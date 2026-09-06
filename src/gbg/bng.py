"""British National Grid (EPSG:27700) <-> WGS84 (EPSG:4326) helpers.

The BGS OGC API stores geometry in CRS84 (lon, lat) and its `bbox` parameter is
lon/lat by default. The rest of this project works in BNG metres, because a
voxel grid in degrees is nonsense. Convert at the boundary, once.
"""

from __future__ import annotations

from functools import lru_cache

from pyproj import Transformer

BBox = tuple[float, float, float, float]


@lru_cache(maxsize=2)
def _transformer(src: int, dst: int) -> Transformer:
    # always_xy=True means (x, y) == (easting, northing) == (lon, lat) ordering throughout.
    return Transformer.from_crs(f"EPSG:{src}", f"EPSG:{dst}", always_xy=True)


def bng_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    lon, lat = _transformer(27700, 4326).transform(easting, northing)
    return lon, lat


def wgs84_to_bng(lon: float, lat: float) -> tuple[float, float]:
    e, n = _transformer(4326, 27700).transform(lon, lat)
    return e, n


def bng_bbox_to_wgs84(bbox: BBox) -> BBox:
    """Convert a BNG bbox to a lon/lat bbox that *contains* it.

    A rectangle in BNG is not a rectangle in lon/lat, so transform all four
    corners and take the envelope. Slightly over-selects; the caller filters
    on easting/northing afterwards (see sobi.SobiClient.iter_boreholes).
    """
    x0, y0, x1, y1 = bbox
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    lonlats = [bng_to_wgs84(e, n) for e, n in corners]
    lons = [p[0] for p in lonlats]
    lats = [p[1] for p in lonlats]
    return (min(lons), min(lats), max(lons), max(lats))


def in_bbox(x: float, y: float, bbox: BBox) -> bool:
    x0, y0, x1, y1 = bbox
    return x0 <= x <= x1 and y0 <= y <= y1
