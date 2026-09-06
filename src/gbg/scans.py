"""Fetch scanned borehole logs from the BGS scans API.

Endpoint (verified in the SOBI index `scan_url` field):
    https://api.bgs.ac.uk/sobi-scans/v1/borehole/scans/items/{bgs_id}

BGS delivers multi-page PDFs. Everything fetched is cached under
config.paths.raw_scans as {bgs_id}.pdf and never re-requested. Each attempt is
recorded (status, content-type, bytes, sha256, pages) so that "we have 3,000
scans" is a query, not a belief.

Unverified until the first live run (Stage 0 checklist in README):
* what the API returns for a bgs_id with no scan (404? 200 with an HTML page?)
* whether `length_scan_cat` ending in `_Y` really means "scan available"
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .http import PoliteClient

SCANS_BASE = "https://api.bgs.ac.uk/sobi-scans/v1/borehole/scans/items"
PDF_MAGIC = b"%PDF-"


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

    def fetch(self, bgs_id: int) -> ScanResult:
        path = self.cached_path(bgs_id)
        if self.is_cached(bgs_id):
            data = path.read_bytes()
            return ScanResult(
                bgs_id=bgs_id, status=200, content_type="application/pdf", bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(), pages=count_pdf_pages(path),
                path=path, ok=True, note="cached",
            )
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
        return ScanResult(
            bgs_id=bgs_id, status=200, content_type=ctype, bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(), pages=count_pdf_pages(path),
            path=path, ok=True, note="fetched",
        )
