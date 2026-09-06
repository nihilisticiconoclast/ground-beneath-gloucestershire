"""Client for the BGS Single Onshore Borehole Index via the OGC API - Features service.

Verified against the live service on 2026-09-06 (see docs/DATA_SOURCES.md):

* collection: https://ogcapi.bgs.ac.uk/collections/onshoreboreholeindex
* items:      .../items?f=json&bbox=<lon,lat,lon,lat>&limit=N&offset=M
* response:   GeoJSON FeatureCollection with `numberMatched`, `numberReturned`
              and a `links[rel=next]` entry carrying the next `offset`.
* properties: reference, name, grid_ref, easting, northing, precision, length,
              year_known, sitereport, held_at, id, length_scan_cat, bgs_id,
              date_updated, scan_url, ags_log_url, water_well_ref, scan_quality

Known traps (all handled here):
* `id` and `bgs_id` arrive as JSON floats (1.0). Cast to int.
* `length` is -2.0 on records whose depth was never captured. Negative length
  is a sentinel, not a depth: stored as None, raw value kept in `length_raw`.
* Names containing "NOTIFICATION ONLY" are index-only records with no log.
* The lon/lat bbox that contains a BNG rectangle over-selects at the corners,
  so every feature is re-filtered on easting/northing after fetch.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, asdict
from typing import Any

from .bng import BBox, bng_bbox_to_wgs84, in_bbox
from .http import PoliteClient

OGCAPI_BASE = "https://ogcapi.bgs.ac.uk"
SOBI_ITEMS = f"{OGCAPI_BASE}/collections/onshoreboreholeindex/items"
AGS_ITEMS = f"{OGCAPI_BASE}/collections/agsboreholeindex/items"

NOTIFICATION_RE = re.compile(r"NOTIFICATION\s+ONLY", re.IGNORECASE)


class CompletenessError(RuntimeError):
    """Raised when the number of features fetched disagrees with numberMatched."""


@dataclass(frozen=True)
class BoreholeRecord:
    bgs_id: int
    reference: str | None
    name: str | None
    grid_ref: str | None
    easting: float
    northing: float
    precision: str | None
    length_m: float | None  # None when the index holds no usable depth
    length_raw: float | None  # exactly what the API said, sentinel and all
    year_known: str | None
    held_at: str | None
    length_scan_cat: str | None
    scan_url: str | None
    ags_log_url: str | None
    water_well_ref: str | None
    scan_quality: str | None
    date_updated: str | None
    notification_only: bool
    lon: float | None
    lat: float | None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def _to_int(v: Any) -> int:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return int(v)


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def parse_feature(feature: dict[str, Any]) -> BoreholeRecord:
    p = feature.get("properties", {})
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or [None, None]
    length_raw = _to_float(p.get("length"))
    length_m = length_raw if (length_raw is not None and length_raw >= 0) else None
    name = p.get("name")
    return BoreholeRecord(
        bgs_id=_to_int(p.get("bgs_id", feature.get("id"))),
        reference=p.get("reference"),
        name=name,
        grid_ref=p.get("grid_ref"),
        easting=float(p["easting"]),
        northing=float(p["northing"]),
        precision=p.get("precision"),
        length_m=length_m,
        length_raw=length_raw,
        year_known=p.get("year_known"),
        held_at=p.get("held_at"),
        length_scan_cat=p.get("length_scan_cat"),
        scan_url=p.get("scan_url"),
        ags_log_url=p.get("ags_log_url"),
        water_well_ref=p.get("water_well_ref"),
        scan_quality=p.get("scan_quality"),
        date_updated=p.get("date_updated"),
        notification_only=bool(name and NOTIFICATION_RE.search(name)),
        lon=coords[0],
        lat=coords[1],
    )


def _next_link(payload: dict[str, Any]) -> str | None:
    for link in payload.get("links", []):
        if link.get("rel") == "next":
            return link.get("href")
    return None


class SobiClient:
    def __init__(self, http: PoliteClient, items_url: str = SOBI_ITEMS):
        self.http = http
        self.items_url = items_url

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.http.get(url, params=params)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            # A 200 with an HTML error page is the failure mode that hides.
            raise RuntimeError(f"Expected JSON from {resp.url}, got content-type {ctype!r}")
        return resp.json()

    def count(self, bbox_bng: BBox) -> int:
        """Independent count for a bbox: numberMatched with limit=1.

        Use this as the oracle for enumerate(): the two numbers must agree.
        """
        lonlat = bng_bbox_to_wgs84(bbox_bng)
        payload = self._get_json(
            self.items_url,
            params={"f": "json", "bbox": ",".join(f"{v:.6f}" for v in lonlat), "limit": 1},
        )
        return int(payload["numberMatched"])

    def iter_boreholes(
        self, bbox_bng: BBox, page_size: int = 500, strict_bbox: bool = True
    ) -> Iterator[BoreholeRecord]:
        """Yield every borehole whose easting/northing falls inside bbox_bng.

        Pages through the API following its own `next` links. Raises
        CompletenessError if the number of features received from the API
        differs from the `numberMatched` it reported on the first page.
        """
        lonlat = bng_bbox_to_wgs84(bbox_bng)
        params: dict[str, Any] | None = {
            "f": "json",
            "bbox": ",".join(f"{v:.6f}" for v in lonlat),
            "limit": page_size,
        }
        url: str | None = self.items_url
        expected: int | None = None
        received = 0
        while url:
            payload = self._get_json(url, params=params)
            params = None  # the next link carries its own query string
            if expected is None:
                expected = int(payload.get("numberMatched", -1))
            features = payload.get("features", [])
            received += len(features)
            for feature in features:
                rec = parse_feature(feature)
                if strict_bbox and not in_bbox(rec.easting, rec.northing, bbox_bng):
                    continue
                yield rec
            url = _next_link(payload)
        if expected is not None and expected >= 0 and received != expected:
            raise CompletenessError(
                f"API reported numberMatched={expected} but delivered {received} features"
            )

    def iter_ags_boreholes(self, bbox_bng: BBox, page_size: int = 500) -> Iterator[dict[str, Any]]:
        """Yield raw AGS-index features (the gold-label candidates) in a bbox.

        Properties (from the service's OpenAPI document): bgs_loca_id, id,
        proj_name, proj_cont, proj_eng, x, y, loca_fdep, rec_id, loca_id,
        item_index_id, ags_log_url, dad_item_url. Left as dicts on purpose:
        the AGS payload format behind ags_log_url is unverified (Stage 0 task).
        """
        lonlat = bng_bbox_to_wgs84(bbox_bng)
        params: dict[str, Any] | None = {
            "f": "json",
            "bbox": ",".join(f"{v:.6f}" for v in lonlat),
            "limit": page_size,
        }
        url: str | None = AGS_ITEMS
        while url:
            payload = self._get_json(url, params=params)
            params = None
            for feature in payload.get("features", []):
                props = dict(feature.get("properties", {}))
                geom = feature.get("geometry") or {}
                props["_lonlat"] = geom.get("coordinates")
                yield props
            url = _next_link(payload)
