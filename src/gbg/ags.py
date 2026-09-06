"""Parse AGS files into gold-standard BoreholeLogs.

AGS4 layout (comma-separated, double-quoted):
    "GROUP","GEOL"
    "HEADING","LOCA_ID","GEOL_TOP","GEOL_BASE","GEOL_DESC","GEOL_LEG",...
    "UNIT","","m","m","",...
    "TYPE","ID","2DP","2DP","X",...
    "DATA","BH01","0.00","0.60","Brown sandy TOPSOIL","101",...

AGS3 layout (older files, still common in the BGS store):
    "**GEOL"
    "*HOLE_ID","*GEOL_TOP","*GEOL_BASE","*GEOL_DESC","*GEOL_LEG"
    "<UNITS>","m","m","",""
    "BH01","0.00","0.60","Brown sandy TOPSOIL","101"

Both give: hole id, top, base, description. The description is normalised to a
controlled class with `lithology.normalise`, and the normaliser's confidence is
carried through — a gold *interval boundary* is trustworthy (it was measured),
a gold *class* is only as good as the description-to-class mapping.

GEOL is read for intervals; LOCA is read for LOCA_NATE / LOCA_NATN (BNG
position) and LOCA_GL (ground level, m AOD) so gold boreholes can be placed in
the model without a BGS id mapping. Everything else in the file is ignored.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict

from .lithology import normalise
from .schema import BoreholeLog, LithInterval

GOLD_SOURCE = "gold:ags"


def _rows(text: str) -> list[list[str]]:
    return [r for r in csv.reader(io.StringIO(text)) if r]


def parse_group(text: str, group: str) -> dict[str, list[dict[str, str]]]:
    """Return rows of one AGS group (e.g. GEOL, LOCA) keyed by hole id."""
    rows = _rows(text)
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    group = group.upper()
    i = 0
    n = len(rows)
    while i < n:
        row = rows[i]
        # ---- AGS4 ----
        if len(row) >= 2 and row[0] == "GROUP" and row[1].upper() == group:
            headings: list[str] | None = None
            i += 1
            while i < n and not (rows[i][0] == "GROUP"):
                r = rows[i]
                if r[0] == "HEADING":
                    headings = [h.upper() for h in r[1:]]
                elif r[0] == "DATA" and headings:
                    rec = dict(zip(headings, r[1:]))
                    hole = rec.get("LOCA_ID") or rec.get("HOLE_ID") or ""
                    out[hole].append(rec)
                i += 1
            continue
        # ---- AGS3 ----
        if row[0].startswith("**") and row[0][2:].upper() == group:
            headings = None
            i += 1
            while i < n and not rows[i][0].startswith("**"):
                r = rows[i]
                if r[0].startswith("*"):
                    headings = [h.lstrip("*").upper() for h in r]
                elif r[0].startswith("<") or not headings:
                    pass  # <UNITS>, <CONT> etc.
                else:
                    rec = dict(zip(headings, r))
                    hole = rec.get("HOLE_ID") or rec.get("LOCA_ID") or ""
                    out[hole].append(rec)
                i += 1
            continue
        i += 1
    return dict(out)


def parse_geol(text: str) -> dict[str, list[dict[str, str]]]:
    """GEOL rows grouped by hole id."""
    return parse_group(text, "GEOL")


def parse_loca(text: str) -> dict[str, dict[str, float | None]]:
    """Per hole: easting, northing, ground_level_m from LOCA (None when absent)."""
    out: dict[str, dict[str, float | None]] = {}
    for hole, recs in parse_group(text, "LOCA").items():
        rec = recs[0]
        out[hole] = {
            "easting": _num(rec.get("LOCA_NATE")),
            "northing": _num(rec.get("LOCA_NATN")),
            "ground_level_m": _num(rec.get("LOCA_GL")),
        }
    return out


def _num(v: str | None) -> float | None:
    if v is None or v.strip() == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def logs_from_ags(text: str, bgs_ids: dict[str, int] | None = None) -> list[BoreholeLog]:
    """Build one BoreholeLog per hole in the file.

    `bgs_ids` maps AGS hole id -> BGS id. Holes without a mapping get a
    negative placeholder id derived from the hole name's hash, so they are
    obviously not real BGS ids downstream.
    """
    logs: list[BoreholeLog] = []
    loca = parse_loca(text)
    for hole, recs in parse_geol(text).items():
        intervals: list[LithInterval] = []
        for rec in recs:
            top = _num(rec.get("GEOL_TOP"))
            base = _num(rec.get("GEOL_BASE"))
            if top is None or base is None or base <= top:
                continue
            desc = rec.get("GEOL_DESC", "") or ""
            norm = normalise(desc)
            intervals.append(
                LithInterval(top_m=top, base_m=base, raw_description=desc,
                             lith_class=norm.lith_class, confidence=norm.confidence)
            )
        if not intervals:
            continue
        bgs_id = (bgs_ids or {}).get(hole)
        if bgs_id is None:
            bgs_id = -(abs(hash(hole)) % 10_000_000 + 1)
        site = loca.get(hole, {})
        logs.append(BoreholeLog(bgs_id=bgs_id, intervals=intervals, source=GOLD_SOURCE,
                                ground_level_m=site.get("ground_level_m"),
                                easting=site.get("easting"), northing=site.get("northing"),
                                notes=f"ags hole {hole}"))
    return logs
