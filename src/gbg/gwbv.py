"""Read the borehole log PDF that `ags_log_url` actually serves.

The AGS index's `ags_log_url` does not deliver AGS text (verified 2026-09-07,
docs/DATA_SOURCES.md §3). It redirects to BGS's GWBV viewer, which returns a
log sheet auto-generated from the depositor's AGS file. That sheet is *vector*:
it carries a text layer, so depths and descriptions can be read exactly, with
no OCR and no vision model. The depths in it were measured and typed by the
site-investigation contractor, which is what made AGS gold in the first place.

Layout of a sheet — column positions are derived from each page's own header
row rather than hard-coded, so a change in the generator's spacing cannot
silently shift a column into its neighbour:

    Well │ Water │ Samples │ Result │ Depth (m) │ Level (m) │ Legend │ Stratum Description │ Depth m
                                      ^ base of   ^ m AOD              ^ free text          ^ ruler
                                        stratum

Three traps, all observed on real sheets:

* The `Depth (m)` value is the **base** of a stratum, printed at the boundary
  line; the description sits inside the band *above* it, positioned to fit the
  space rather than at the boundary. Pairing the two by y-position is wrong.
  Pairing them by order is right — and the count of descriptions must equal
  the count of boundaries, or the parse is rejected rather than guessed at.
* A stratum spanning a page break has its description **repeated** verbatim at
  the top of the next page. Left in, it invents an interval.
* The depth ruler down the right-hand edge is text too, and sits close enough
  to the description column to be swept into it.

The parse is checked against the sheet's own arithmetic before it is returned:
every `Depth (m)` + `Level (m)` pair must sum to the stated ground level. That
is an independent number the generator printed, so a column read one place out
cannot pass quietly.
"""

from __future__ import annotations

import re
import statistics
import zlib
from dataclasses import dataclass
from pathlib import Path

from .bng import bng_bbox_to_wgs84, in_bbox
from .config import BBox
from .http import PoliteClient
from .lithology import normalise
from .schema import BoreholeLog, LithInterval

GWBV_BASE = "https://webservices.bgs.ac.uk/GWBV/viewborehole"
AGS_ITEMS = "https://ogcapi.bgs.ac.uk/collections/agsboreholeindex/items"

# The sheet's own arithmetic must close to this. Depth and level are printed to
# 2 dp and ground level to 1 dp, so anything under half a printed unit is
# rounding; anything above it means a column was read wrongly.
GL_TOLERANCE_M = 0.06

_COORDS = re.compile(r"Co-?ords\s*\(British National Grid\)\s*:\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)")
_LEVEL = re.compile(r"\bLevel:\s*(-?\d+(?:\.\d+)?)")
_BOREHOLE_ID = re.compile(r"Borehole ID:\s*(\S+)")
_PROJECT = re.compile(r"Project Name:\s*(.+?)\s+Project No:")
_REFERENCE = re.compile(r"BGS Reference\s+(\S+)")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
# Two wordings observed for "we hold this record but it has no strata", the
# second with the service's own typo. Matched loosely for that reason.
_NO_LOG_DATA = re.compile(r"No log data available|No strata inf\w+ is available", re.I)

_HEADER_BAND = (140.0, 160.0)  # the column-header row
_BODY_TOP = 160.0              # below the header row
_BODY_BOTTOM = 760.0           # above the NGDC footer


class GwbvParseError(ValueError):
    """The sheet did not parse into a log we are willing to call gold."""


class NoLogData(GwbvParseError):
    """The service returned a valid PDF that says it holds no log for this record.

    Observed 2026-09-07 on a dynamic-probe record: HTTP 200, a well-formed
    single-page PDF, and the only text on it is "No log data available for this
    record". This is an outcome to count, not a parse to fix, so it is raised
    separately from a genuine parse failure.
    """


@dataclass(frozen=True)
class GwbvHeader:
    borehole_id: str | None = None
    project: str | None = None
    reference: str | None = None
    easting: float | None = None
    northing: float | None = None
    ground_level_m: float | None = None


def log_url(loca_id: str) -> str:
    """The URL `ags_log_url` resolves to, for a given AGS `bgs_loca_id`."""
    return f"{GWBV_BASE}?loca_id={loca_id}"


def synthetic_bgs_id(bgs_loca_id: str) -> int:
    """A stable negative id for an AGS hole that has no SOBI `bgs_id`.

    `bgs_loca_id` is a 19-digit string, far outside the INTEGER the corpus keys
    on, and the AGS index carries no SOBI id at all. The repository's existing
    convention is that gold holes with no BGS id mapping take negative ids, so
    these do too: same input, same id, and never a collision with a real
    positive `bgs_id`. Callers must still check for collisions *between* AGS
    holes — `AgsRecord.check_unique_ids` does.
    """
    return -(zlib.crc32(bgs_loca_id.encode()) % 2_000_000_000 + 1)


@dataclass(frozen=True)
class AgsRecord:
    """One row of the AGS borehole index, reduced to what the gold path needs."""

    bgs_loca_id: str
    loca_id: str | None
    project: str | None
    easting: float
    northing: float
    final_depth_m: float | None
    log_url: str

    @property
    def bgs_id(self) -> int:
        return synthetic_bgs_id(self.bgs_loca_id)

    @staticmethod
    def check_unique_ids(records: list[AgsRecord]) -> None:
        seen: dict[int, str] = {}
        for r in records:
            prior = seen.get(r.bgs_id)
            if prior is not None and prior != r.bgs_loca_id:
                raise ValueError(
                    f"synthetic id collision: {prior} and {r.bgs_loca_id} both map to {r.bgs_id}"
                )
            seen[r.bgs_id] = r.bgs_loca_id


def iter_ags_records(http: PoliteClient, bbox_bng: BBox, page_size: int = 2000) -> list[AgsRecord]:
    """AGS index records inside a BNG rectangle that carry a fetchable log.

    The collection advertises CRS84 only, so the query goes out as the lon/lat
    envelope of the rectangle and the results are re-filtered on the BNG `x`/`y`
    the records themselves carry — the same over-selection correction
    `SobiClient` makes, for the same reason.
    """
    resp = http.get(AGS_ITEMS, params={
        "f": "json", "limit": page_size,
        "bbox": ",".join(f"{v:.6f}" for v in bng_bbox_to_wgs84(bbox_bng)),
    })
    resp.raise_for_status()
    body = resp.json()
    out: list[AgsRecord] = []
    for feature in body.get("features", []):
        p = feature.get("properties", {})
        x, y, url = p.get("x"), p.get("y"), p.get("ags_log_url")
        if not url or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        if not in_bbox(float(x), float(y), bbox_bng):
            continue
        out.append(AgsRecord(
            bgs_loca_id=str(p.get("bgs_loca_id") or p.get("id")),
            loca_id=p.get("loca_id"),
            project=p.get("proj_name"),
            easting=float(x), northing=float(y),
            final_depth_m=p.get("loca_fdep") if isinstance(p.get("loca_fdep"), (int, float)) else None,
            log_url=url,
        ))
    AgsRecord.check_unique_ids(out)
    return out


@dataclass
class LogFetch:
    """What one attempt at a log sheet produced. Failures are recorded, not raised."""

    record: AgsRecord
    ok: bool
    note: str
    path: Path | None = None
    log: BoreholeLog | None = None


class AgsLogFetcher:
    """Fetch and cache GWBV log sheets. Cached bytes are never re-requested."""

    def __init__(self, http: PoliteClient, cache_dir: Path, max_per_run: int):
        self.http = http
        self.cache_dir = cache_dir
        self.max_per_run = max_per_run
        self.fetched_this_run = 0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def cached_path(self, record: AgsRecord) -> Path:
        return self.cache_dir / f"{record.bgs_loca_id}.pdf"

    def fetch(self, record: AgsRecord, source: str) -> LogFetch:
        path = self.cached_path(record)
        if not (path.exists() and path.stat().st_size > 0):
            if self.fetched_this_run >= self.max_per_run:
                return LogFetch(record, False, f"run cap reached ({self.max_per_run})")
            self.fetched_this_run += 1
            resp = self.http.get(record.log_url, headers={"Accept": "application/pdf"})
            if resp.status_code != 200:
                return LogFetch(record, False, f"http {resp.status_code}")
            if not resp.content.startswith(b"%PDF-"):
                return LogFetch(record, False,
                                f"200 but not a PDF ({resp.headers.get('content-type')})")
            path.write_bytes(resp.content)
        try:
            log = parse_log(path, bgs_id=record.bgs_id, source=source,
                            index_depth_m=record.final_depth_m)
        except NoLogData as exc:
            return LogFetch(record, False, f"no log: {exc}", path=path)
        except GwbvParseError as exc:
            return LogFetch(record, False, f"parse: {exc}", path=path)
        return LogFetch(record, True, "parsed", path=path, log=log)


# ----------------------------------------------------------------- geometry


def _lines(words, xlo: float, xhi: float, ylo: float, yhi: float, tol: float = 3.0
           ) -> list[tuple[float, str]]:
    """Words whose left edge falls in an x band, grouped into lines by y."""
    sel = [w for w in words if xlo <= w[0] < xhi and ylo <= w[1] < yhi]
    out: list[tuple[float, str]] = []
    cur: list[str] = []
    y: float | None = None
    for w in sorted(sel, key=lambda w: (round(w[1], 1), w[0])):
        if y is None or abs(w[1] - y) > tol:
            if cur:
                out.append((y, " ".join(cur)))
            cur, y = [], w[1]
        cur.append(w[4])
    if cur and y is not None:
        out.append((y, " ".join(cur)))
    return out


def _bands(words) -> dict[str, tuple[float, float]] | None:
    """Column x-bands for one page, taken from that page's own header row.

    Returns None for a page with no log columns on it (the sheets carry a
    trailing key page), which the caller treats as "nothing here", not an error.
    """
    head = [w for w in words if _HEADER_BAND[0] <= w[1] <= _HEADER_BAND[1]]
    # Three words read "Depth" on a sheet: the samples column (~x 82), the
    # strata column (~x 273) and the ruler heading (~x 549, a row higher).
    ruler = [w for w in words if w[4] == "Depth" and w[0] > 500.0 and w[1] < _HEADER_BAND[0]]

    def find(word: str, lo: float, hi: float) -> tuple[float, float] | None:
        for w in head:
            if w[4] == word and lo <= w[0] < hi:
                return w[0], w[2]
        return None

    depth = find("Depth", 200.0, 400.0)
    level = find("Level", 200.0, 400.0)
    legend = find("Legend", 200.0, 420.0)
    if not (depth and level and legend and ruler):
        return None
    right_edge = min(r[0] for r in ruler)
    return {
        "depth": (depth[0] - 6.0, level[0] - 6.0),
        "level": (level[0] - 6.0, legend[0] - 6.0),
        "description": (legend[1] + 4.0, right_edge - 4.0),
    }


def _numeric(lines: list[tuple[float, str]]) -> list[tuple[float, float]]:
    """Keep only lines that are a bare number; return (y, value)."""
    out = []
    for y, text in lines:
        t = text.strip()
        if _NUMBER.fullmatch(t):
            out.append((y, float(t)))
    return out


def _blocks(lines: list[tuple[float, str]], max_gap: float = 12.0) -> list[str]:
    """Consecutive lines closer together than `max_gap` are one description.

    Bare numbers are dropped first: the depth ruler is right-aligned, so a wide
    value ("120.5") starts further left than a narrow one ("10.5") and reaches
    into the description column on deep logs. No real description is a number
    and nothing else.
    """
    lines = [(y, t) for y, t in lines if not _NUMBER.fullmatch(t.strip())]
    blocks: list[str] = []
    cur: list[str] = []
    last_y: float | None = None
    for y, text in lines:
        if last_y is not None and y - last_y > max_gap and cur:
            blocks.append(" ".join(cur))
            cur = []
        cur.append(text)
        last_y = y
    if cur:
        blocks.append(" ".join(cur))
    return blocks


# ------------------------------------------------------------------- header


def parse_header(page_words) -> GwbvHeader:
    """Read the identity block at the top of page 1."""
    text = " ".join(w[4] for w in sorted(
        (w for w in page_words if w[1] < _HEADER_BAND[0]), key=lambda w: (round(w[1]), w[0])))
    coords = _COORDS.search(text)
    level = _LEVEL.search(text)
    bid = _BOREHOLE_ID.search(text)
    proj = _PROJECT.search(text)
    return GwbvHeader(
        borehole_id=bid.group(1) if bid else None,
        project=proj.group(1).strip() if proj else None,
        reference=None,
        easting=float(coords.group(1)) if coords else None,
        northing=float(coords.group(2)) if coords else None,
        ground_level_m=float(level.group(1)) if level else None,
    )


# -------------------------------------------------------------------- parse


def parse_log(
    pdf: bytes | Path,
    bgs_id: int,
    source: str,
    index_depth_m: float | None = None,
) -> BoreholeLog:
    """Turn one GWBV log sheet into a `BoreholeLog`, or raise `GwbvParseError`."""
    import pymupdf  # lazy: heavy, and only this module needs it

    data = pdf.read_bytes() if isinstance(pdf, Path) else pdf
    if not data.startswith(b"%PDF-"):
        raise GwbvParseError("not a PDF")

    boundaries: list[tuple[float, float | None]] = []  # (depth, level), in sheet order
    descriptions: list[str] = []
    header = GwbvHeader()
    reference: str | None = None

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.page_count == 0:
            raise GwbvParseError("empty PDF")
        if _NO_LOG_DATA.search(doc[0].get_text()):
            raise NoLogData("service holds no log for this record")
        for pno in range(doc.page_count):
            words = doc[pno].get_text("words")
            if pno == 0:
                header = parse_header(words)
                whole = " ".join(w[4] for w in words)
                ref = _REFERENCE.search(whole)
                reference = ref.group(1) if ref else None
            bands = _bands(words)
            if bands is None:
                continue
            depths = _numeric(_lines(words, *bands["depth"], _BODY_TOP, _BODY_BOTTOM))
            levels = _numeric(_lines(words, *bands["level"], _BODY_TOP, _BODY_BOTTOM))
            # Older records (digitised water wells, mostly) carry no ground
            # level, so the sheet prints depths with the Level column blank.
            # That is the model's depth frame, not a broken sheet.
            by_y = {round(y): v for y, v in levels}
            for y, d in depths:
                boundaries.append((d, by_y.get(round(y))))

            page_blocks = _blocks(_lines(words, *bands["description"], _BODY_TOP, _BODY_BOTTOM))
            # A stratum crossing the page break repeats its description at the
            # top of the new page; that is the same stratum, not a new one.
            if page_blocks and descriptions and page_blocks[0] == descriptions[-1]:
                page_blocks = page_blocks[1:]
            descriptions.extend(page_blocks)

    if not boundaries:
        raise GwbvParseError("no depth boundaries found")
    if len(descriptions) != len(boundaries):
        raise GwbvParseError(
            f"{len(descriptions)} descriptions but {len(boundaries)} depth boundaries"
        )

    depths = [d for d, _ in boundaries]
    if any(b <= a for a, b in zip(depths, depths[1:])):
        raise GwbvParseError(f"depths are not strictly increasing: {depths}")

    # The sheet's own arithmetic: depth + level == ground level, on every row
    # that carries a level. With no levels anywhere there is no ground level to
    # check against, and the log is in the depth frame.
    sums = [d + lv for d, lv in boundaries if lv is not None]
    gl = header.ground_level_m
    if gl is None and sums:
        gl = statistics.median(sums)
    if gl is not None and sums:
        worst = max(abs(s - gl) for s in sums)
        if worst > GL_TOLERANCE_M:
            raise GwbvParseError(
                f"depth + level does not close on ground level {gl}: worst row off by {worst:.2f} m"
            )

    intervals: list[LithInterval] = []
    for (base, _), desc, top in zip(boundaries, descriptions, [0.0] + depths[:-1]):
        n = normalise(desc)
        intervals.append(LithInterval(
            top_m=top, base_m=base, raw_description=desc,
            lith_class=n.lith_class, confidence=n.confidence,
        ))

    # A sheet whose only "description" is an editorial note ("Duplicate entry
    # of BH ST67NW133 …") parses cleanly into one UNKNOWN interval spanning the
    # whole hole. That is not a log, and calling it gold would put a hundred
    # metres of nothing into the corpus.
    if all(iv.lith_class == "UNKNOWN" for iv in intervals):
        raise NoLogData(
            f"no interval could be classified from {len(intervals)} description(s); "
            "the sheet carries a note rather than strata"
        )

    notes = []
    if gl is None:
        notes.append("no ground level on the sheet; depths are below ground, not m AOD")
    elif header.ground_level_m is None:
        notes.append(f"ground level not stated; derived {gl:.2f} m AOD from depth + level")
    if index_depth_m is not None and abs(depths[-1] - index_depth_m) > 0.05:
        notes.append(f"sheet base {depths[-1]} m vs index loca_fdep {index_depth_m} m")
    if reference:
        notes.append(f"BGS Reference {reference}")

    return BoreholeLog(
        bgs_id=bgs_id,
        intervals=intervals,
        source=source,
        total_depth_m=depths[-1],
        ground_level_m=gl,
        easting=header.easting,
        northing=header.northing,
        notes="; ".join(notes),
    )
