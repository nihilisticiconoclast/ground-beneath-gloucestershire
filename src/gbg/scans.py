"""Fetch scanned borehole logs from the BGS scans API.

Endpoint (verified in the SOBI index `scan_url` field):
    https://api.bgs.ac.uk/sobi-scans/v1/borehole/scans/items/{bgs_id}

BGS delivers multi-page PDFs. Everything fetched is cached under
config.paths.raw_scans as {bgs_id}.pdf and never re-requested. Each attempt is
recorded (status, content-type, bytes, sha256, pages) so that "we have 3,000
scans" is a query, not a belief.

Verified on the first live run, 2026-09-26 (see docs/DATA_SOURCES.md §2):
* a bgs_id with no scan returns HTTP 404 with a JSON body, not a 200 HTML page
* `length_scan_cat` ending `_N` did mean "no scan" on every record tried
* the pages are raster images of the original log, with one small block of real
  text stamped on each — the identity block `read_stamp` reads below

That stamp is the only machine-readable thing on a scan, and it is what lets a
cached file prove it is the borehole it is filed under. Without it a scan is
trusted purely because of its filename, and a mis-served or mis-filed PDF would
be indistinguishable from a good one. It is *not* an independent position: the
stamp agreed with the SOBI index to 0.0 m on all 25 scans first checked, which
says the two come from one database, so it verifies identity and nothing more.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .http import PoliteClient

SCANS_BASE = "https://api.bgs.ac.uk/sobi-scans/v1/borehole/scans/items"
PDF_MAGIC = b"%PDF-"

# The stamp, verbatim from scan 270686 page 1:
#     BGS ID: 270686 : BGS Reference: SO80SW28
#     British National Grid (27700) : 382930,204720
#     Contact BGS: ngdc@bgs.ac.uk
_STAMP_ID = re.compile(r"BGS ID:\s*(\d+)\s*:\s*BGS Reference:\s*(\S+)")
_STAMP_BNG = re.compile(r"British National Grid\s*\(27700\)\s*:\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)")


@dataclass(frozen=True)
class ScanStamp:
    """The identity block BGS prints on every page of a scan."""

    bgs_id: int
    reference: str
    easting: float | None
    northing: float | None


def read_stamp(path: Path) -> ScanStamp | None:
    """Read the identity stamp off a scan's first page, or None if it carries none."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - pymupdf is a hard dependency in practice
        return None
    try:
        with pymupdf.open(path) as doc:
            if doc.page_count == 0:
                return None
            text = doc[0].get_text()
    except Exception:  # noqa: BLE001 - an unreadable PDF is "no stamp", not a crash
        return None
    ident = _STAMP_ID.search(text)
    if not ident:
        return None
    bng = _STAMP_BNG.search(text)
    return ScanStamp(
        bgs_id=int(ident.group(1)),
        reference=ident.group(2),
        easting=float(bng.group(1)) if bng else None,
        northing=float(bng.group(2)) if bng else None,
    )


@dataclass(frozen=True)
class ScanResult:
    bgs_id: int
    status: int
    content_type: str
    bytes: int
    sha256: str | None
    pages: int | None
    path: Path | None
    ok: bool
    note: str
    # None means the scan carries no stamp: unverified, which is not the same as
    # wrong, and is counted separately by `gbg verify-scans`.
    stamp: ScanStamp | None = None


def scan_url(bgs_id: int) -> str:
    return f"{SCANS_BASE}/{bgs_id}"


def count_pdf_pages(path: Path) -> int | None:
    try:
        import pymupdf  # imported lazily: heavy, and not needed by tests of the HTTP path
    except ImportError:  # pragma: no cover
        return None
    with pymupdf.open(path) as doc:
        return doc.page_count


class ScanFetcher:
    def __init__(self, http: PoliteClient, raw_dir: Path, max_per_run: int):
        self.http = http
        self.raw_dir = raw_dir
        self.max_per_run = max_per_run
        self.fetched_this_run = 0
        raw_dir.mkdir(parents=True, exist_ok=True)

    def cached_path(self, bgs_id: int) -> Path:
        return self.raw_dir / f"{bgs_id}.pdf"

    def is_cached(self, bgs_id: int) -> bool:
        p = self.cached_path(bgs_id)
        return p.exists() and p.stat().st_size > 0

    @staticmethod
    def _verified(result: ScanResult) -> ScanResult:
        """Refuse a PDF that says it is a different borehole from the one asked for."""
        if result.path is None:
            return result
        stamp = read_stamp(result.path)
        if stamp is not None and stamp.bgs_id != result.bgs_id:
            return replace(
                result, ok=False, stamp=stamp,
                note=f"stamp says bgs_id {stamp.bgs_id} ({stamp.reference}), asked for "
                     f"{result.bgs_id}",
            )
        return replace(result, stamp=stamp)

    def fetch(self, bgs_id: int) -> ScanResult:
        path = self.cached_path(bgs_id)
        if self.is_cached(bgs_id):
            data = path.read_bytes()
            return self._verified(ScanResult(
                bgs_id=bgs_id, status=200, content_type="application/pdf", bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(), pages=count_pdf_pages(path),
                path=path, ok=True, note="cached",
            ))
        if self.fetched_this_run >= self.max_per_run:
            return ScanResult(
                bgs_id=bgs_id, status=0, content_type="", bytes=0, sha256=None, pages=None,
                path=None, ok=False, note=f"run cap reached ({self.max_per_run})",
            )
        self.fetched_this_run += 1
        resp = self.http.get(scan_url(bgs_id), headers={"Accept": "application/pdf"})
        ctype = resp.headers.get("content-type", "")
        body = resp.content
        if resp.status_code != 200:
            return ScanResult(
                bgs_id=bgs_id, status=resp.status_code, content_type=ctype, bytes=len(body),
                sha256=None, pages=None, path=None, ok=False, note=f"http {resp.status_code}",
            )
        if not body.startswith(PDF_MAGIC):
            # HTTP 200 + not-a-PDF is the silent failure to worry about.
            return ScanResult(
                bgs_id=bgs_id, status=200, content_type=ctype, bytes=len(body), sha256=None,
                pages=None, path=None, ok=False, note="200 but body is not a PDF",
            )
        path.write_bytes(body)
        result = self._verified(ScanResult(
            bgs_id=bgs_id, status=200, content_type=ctype, bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(), pages=count_pdf_pages(path),
            path=path, ok=True, note="fetched",
        ))
        if not result.ok:
            # Do not keep a PDF filed under the wrong borehole; a later run would
            # read it straight out of the cache and never ask again.
            path.unlink(missing_ok=True)
            return replace(result, path=None)
        return result
