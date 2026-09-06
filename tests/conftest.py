"""Test fixtures.

The fake BGS server below reproduces the *shape* of the live responses
captured on 2026-09-06 (see docs/DATA_SOURCES.md), including the traps:
float ids, the -2.0 length sentinel, NOTIFICATION ONLY names, and the
`links[rel=next]` pagination. Nothing here talks to the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from gbg.bng import bng_bbox_to_wgs84, bng_to_wgs84, in_bbox, wgs84_to_bng
from gbg.config import ClientConfig, Gates, load_config

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

# A minimal, valid single-page PDF.
TINY_PDF = (
    b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n"
    b"0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n160\n%%EOF\n"
)


def feature(bgs_id: int, easting: float, northing: float, length: float, name: str = "TEST BH") -> dict:
    lon, lat = bng_to_wgs84(easting, northing)
    return {
        "type": "Feature",
        "id": float(bgs_id),
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "reference": f"SO80SW{bgs_id}",
            "name": name,
            "grid_ref": "SO 85000 05000",
            "easting": float(easting),
            "northing": float(northing),
            "precision": "\u00b1 10 METRES",
            "length": float(length),
            "year_known": "1971",
            "sitereport": None,
            "held_at": "KW",
            "id": float(bgs_id),
            "length_scan_cat": ("-2_Y" if length < 0 else "2_Y"),
            "bgs_id": float(bgs_id),
            "date_updated": "2022-10-25T16:33:25",
            "scan_url": f"https://api.bgs.ac.uk/sobi-scans/v1/borehole/scans/items/{bgs_id}",
            "ags_log_url": None,
            "water_well_ref": "N/A",
            "scan_quality": "not Entered",
        },
    }


PILOT_BBOX_BNG = load_config(REPO_ROOT / "config" / "gbg.toml").aoi("pilot").bbox_bng


def _envelope_corner_outside_bng() -> tuple[float, float]:
    """A point inside the lon/lat envelope of the pilot bbox but outside the BNG rectangle.

    The envelope is the lon/lat bounding box of the four transformed corners, so
    its south-west corner lies beyond the BNG south-west corner on at least one
    axis (grid convergence at Stroud is worth ~15 m over 5 km). Step a hair
    inside the envelope so the point is unambiguously within it.
    """
    lon0, lat0, lon1, lat1 = bng_bbox_to_wgs84(PILOT_BBOX_BNG)
    lon, lat = lon0 + 1e-7, lat0 + 1e-7
    e, n = wgs84_to_bng(lon, lat)
    assert lon0 <= lon <= lon1 and lat0 <= lat <= lat1
    assert not in_bbox(e, n, PILOT_BBOX_BNG), (e, n, PILOT_BBOX_BNG)
    return e, n


_OUT_E, _OUT_N = _envelope_corner_outside_bng()

# Six boreholes: five inside the pilot rectangle, one outside it in BNG but
# inside the lon/lat envelope (the corner over-selection case), plus one
# notification-only and one with the -2.0 length sentinel.
PILOT_FEATURES = [
    feature(1001, 383000, 203000, 12.5),
    feature(1002, 384000, 204000, 30.0),
    feature(1003, 385000, 205000, -2.0),  # sentinel depth
    feature(1004, 386000, 206000, 8.0, name="SP12 - NOTIFICATION ONLY"),
    feature(1005, 387000, 207000, 45.2),
    feature(1006, _OUT_E, _OUT_N, 20.0),  # envelope-only record
]


class FakeBGS:
    """Serves paginated SOBI items and scan PDFs; counts requests; can be told to misbehave."""

    def __init__(self, features: list[dict], page_size_cap: int = 2, lie_about_total: bool = False):
        self.features = features
        self.page_size_cap = page_size_cap
        self.lie_about_total = lie_about_total
        self.requests: list[str] = []
        self.scan_bodies: dict[int, tuple[int, str, bytes]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        parsed = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path.endswith("/collections/onshoreboreholeindex/items"):
            limit = min(int(q.get("limit", 10)), self.page_size_cap)
            offset = int(q.get("offset", 0))
            bbox = [float(v) for v in q["bbox"].split(",")] if "bbox" in q else None
            feats = self.features
            if bbox:
                x0, y0, x1, y1 = bbox
                feats = [
                    f for f in feats
                    if x0 <= f["geometry"]["coordinates"][0] <= x1 and y0 <= f["geometry"]["coordinates"][1] <= y1
                ]
            total = len(feats) + (3 if self.lie_about_total else 0)
            page = feats[offset : offset + limit]
            links = [{"rel": "self", "href": url}]
            if offset + limit < len(feats):
                links.append({
                    "rel": "next", "type": "application/geo+json",
                    "href": f"https://ogcapi.bgs.ac.uk{parsed.path}?f=json&bbox={q.get('bbox', '')}&limit={limit}&offset={offset + limit}",
                })
            body = {"type": "FeatureCollection", "features": page, "numberMatched": total,
                    "numberReturned": len(page), "links": links}
            return httpx.Response(200, json=body, headers={"content-type": "application/geo+json"})
        if "/sobi-scans/v1/borehole/scans/items/" in parsed.path:
            bgs_id = int(parsed.path.rsplit("/", 1)[-1])
            status, ctype, body = self.scan_bodies.get(bgs_id, (404, "text/html", b"<html>not found</html>"))
            return httpx.Response(status, content=body, headers={"content-type": ctype})
        return httpx.Response(404, content=b"unknown route")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_bgs() -> FakeBGS:
    server = FakeBGS(PILOT_FEATURES)
    server.scan_bodies[1001] = (200, "application/pdf", TINY_PDF)
    server.scan_bodies[1002] = (200, "text/html", b"<html>Sorry, no scan</html>")  # 200 but not a PDF
    return server


@pytest.fixture
def client_cfg() -> ClientConfig:
    # Fast limiter for tests; the real config is 0.5 req/s.
    return ClientConfig(user_agent="gbg-tests", requests_per_second=10_000, max_scans_per_run=3,
                        timeout_seconds=5, max_retries=1)


@pytest.fixture
def gates() -> Gates:
    return Gates(boundary_tolerance_m=0.25, min_boundary_recall=0.85, min_lithology_f1=0.80,
                 min_gold_boreholes=2)


@pytest.fixture
def real_config():
    return load_config(REPO_ROOT / "config" / "gbg.toml")


@pytest.fixture
def pilot_bbox(real_config):
    return real_config.aoi("pilot").bbox_bng


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
