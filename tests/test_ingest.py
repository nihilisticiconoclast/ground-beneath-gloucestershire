from __future__ import annotations

import pytest

from gbg import db as dbm
from gbg.bng import bng_bbox_to_wgs84, bng_to_wgs84, in_bbox, wgs84_to_bng
from gbg.http import PoliteClient
from gbg.scans import ScanFetcher
from gbg.sobi import CompletenessError, SobiClient, parse_feature

from conftest import PILOT_FEATURES, FakeBGS, feature


# ------------------------------------------------------------------ config


def test_real_config_loads_and_aois_are_sane(real_config):
    assert set(real_config.aois) >= {"pilot", "stroud_valleys", "gloucestershire"}
    pilot = real_config.aoi("pilot").bbox_bng
    x0, y0, x1, y1 = pilot
    assert (x1 - x0, y1 - y0) == (5000, 5000)
    # Pilot sits inside the valleys, which sit inside the county.
    for inner, outer in (("pilot", "stroud_valleys"), ("stroud_valleys", "gloucestershire")):
        a, b = real_config.aoi(inner).bbox_bng, real_config.aoi(outer).bbox_bng
        assert b[0] <= a[0] and b[1] <= a[1] and a[2] <= b[2] and a[3] <= b[3]


def test_unknown_aoi_names_the_known_ones(real_config):
    with pytest.raises(KeyError, match="pilot"):
        real_config.aoi("narnia")


# ------------------------------------------------------------------ bng


def test_stroud_roundtrips_and_lands_in_gloucestershire():
    lon, lat = bng_to_wgs84(385100, 205100)
    assert -2.30 < lon < -2.15 and 51.70 < lat < 51.80  # Stroud is ~(-2.22, 51.745)
    e, n = wgs84_to_bng(lon, lat)
    assert abs(e - 385100) < 0.01 and abs(n - 205100) < 0.01


def test_envelope_contains_all_corners(pilot_bbox):
    lonlat = bng_bbox_to_wgs84(pilot_bbox)
    x0, y0, x1, y1 = pilot_bbox
    for e, n in [(x0, y0), (x1, y0), (x0, y1), (x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2)]:
        lon, lat = bng_to_wgs84(e, n)
        assert lonlat[0] <= lon <= lonlat[2] and lonlat[1] <= lat <= lonlat[3]


# ------------------------------------------------------------------ sobi parsing


def test_parse_feature_handles_the_known_traps():
    rec = parse_feature(PILOT_FEATURES[2])  # length -2.0
    assert rec.bgs_id == 1003 and isinstance(rec.bgs_id, int)
    assert rec.length_m is None and rec.length_raw == -2.0
    assert rec.notification_only is False

    notif = parse_feature(PILOT_FEATURES[3])
    assert notif.notification_only is True
    assert notif.length_m == 8.0

    good = parse_feature(PILOT_FEATURES[0])
    assert good.scan_url.endswith("/scans/items/1001")
    assert good.lon is not None and good.lat is not None


# ------------------------------------------------------------------ sobi client


def test_enumerate_pages_through_next_links_and_trims_to_bng(fake_bgs, client_cfg, pilot_bbox):
    with PoliteClient(client_cfg, transport=fake_bgs.transport()) as http:
        sobi = SobiClient(http)
        n = sobi.count(pilot_bbox)
        recs = list(sobi.iter_boreholes(pilot_bbox, page_size=2))
    # Envelope holds all six; the BNG rectangle holds five (1006 is 100 m outside).
    assert n == 6
    assert sorted(r.bgs_id for r in recs) == [1001, 1002, 1003, 1004, 1005]
    # 6 features at a page cap of 2 = 3 pages, plus the count call.
    item_calls = [u for u in fake_bgs.requests if "/items" in u]
    assert len(item_calls) == 4
    assert "offset=4" in item_calls[-1]


def test_bbox_parameter_changes_the_count(fake_bgs, client_cfg, pilot_bbox):
    # "Before trusting a parameter, confirm it changes the output."
    with PoliteClient(client_cfg, transport=fake_bgs.transport()) as http:
        sobi = SobiClient(http)
        pilot = sobi.count(pilot_bbox)
        tiny = sobi.count((383900, 203900, 384100, 204100))  # just around 1002
    assert pilot == 6 and tiny == 1


def test_enumerate_raises_when_delivered_count_disagrees_with_number_matched(client_cfg, pilot_bbox):
    liar = FakeBGS(PILOT_FEATURES, page_size_cap=2, lie_about_total=True)
    with PoliteClient(client_cfg, transport=liar.transport()) as http:
        sobi = SobiClient(http)
        with pytest.raises(CompletenessError, match="numberMatched=9 but delivered 6"):
            list(sobi.iter_boreholes(pilot_bbox, page_size=2))


def test_non_json_200_is_an_error(client_cfg, pilot_bbox):
    import httpx

    def html_handler(request):
        return httpx.Response(200, content=b"<html>maintenance</html>", headers={"content-type": "text/html"})

    with PoliteClient(client_cfg, transport=httpx.MockTransport(html_handler)) as http:
        with pytest.raises(RuntimeError, match="Expected JSON"):
            SobiClient(http).count(pilot_bbox)


# ------------------------------------------------------------------ db


def test_upsert_is_idempotent(tmp_path, fake_bgs, client_cfg, pilot_bbox):
    con = dbm.connect(tmp_path / "t.duckdb")
    with PoliteClient(client_cfg, transport=fake_bgs.transport()) as http:
        recs = list(SobiClient(http).iter_boreholes(pilot_bbox, page_size=2))
    assert dbm.upsert_boreholes(con, recs, aoi="pilot") == 5
    assert dbm.upsert_boreholes(con, recs, aoi="pilot") == 5
    s = dbm.summary(con)
    assert s["boreholes"] == 5
    assert s["boreholes_with_depth"] == 4  # 1003 has the sentinel
    assert s["notification_only"] == 1


# ------------------------------------------------------------------ scans


def test_scan_fetcher_distinguishes_pdf_from_200_html_and_caps_the_run(tmp_path, fake_bgs, client_cfg):
    with PoliteClient(client_cfg, transport=fake_bgs.transport()) as http:
        fetcher = ScanFetcher(http, tmp_path / "scans", max_per_run=2)
        good = fetcher.fetch(1001)
        bad = fetcher.fetch(1002)
        capped = fetcher.fetch(1003)
        cached = fetcher.fetch(1001)  # cache hit: no request, no cap consumed
    assert good.ok and good.pages == 1 and good.path.exists() and good.sha256
    assert not bad.ok and bad.status == 200 and "not a PDF" in bad.note
    assert not (tmp_path / "scans" / "1002.pdf").exists()
    assert not capped.ok and "run cap" in capped.note
    assert cached.ok and cached.note == "cached"
    scan_calls = [u for u in fake_bgs.requests if "sobi-scans" in u]
    assert len(scan_calls) == 2
