"""MCP server tests — in-process, no transport, no network.

Builds a small corpus in a temporary DuckDB, bakes a model from the flat
synthetic world, points the server at both via environment variables, and
calls every tool the way a client would (list_tools / call_tool).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gbg import db as dbm
from gbg.ags import GOLD_SOURCE, logs_from_ags
from gbg.model import KernelLithologyModel, discretise, kernel_surface, write_model_json
from gbg.bng import in_bbox
from gbg.sobi import parse_feature

from conftest import FIXTURES, PILOT_FEATURES
from test_model import BBOX, flat_world

pytest.importorskip("mcp")


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    # Config: copy the real one but point paths and model output into tmp.
    cfg_text = (Path(__file__).resolve().parents[1] / "config" / "gbg.toml").read_text()
    cfg_text = cfg_text.replace('db = "data/gbg.duckdb"', f'db = "{tmp_path / "gbg.duckdb"}"')
    cfg_text = cfg_text.replace('output = "web/data/model.json"', f'output = "{tmp_path / "model.json"}"')
    cfg_path = tmp_path / "gbg.toml"
    cfg_path.write_text(cfg_text)
    monkeypatch.setenv("GBG_CONFIG", str(cfg_path))
    monkeypatch.setenv("GBG_MODEL", str(tmp_path / "model.json"))

    con = dbm.connect(tmp_path / "gbg.duckdb")
    recs = [parse_feature(f) for f in PILOT_FEATURES]
    dbm.upsert_boreholes(con, [r for r in recs if in_bbox(r.easting, r.northing, BBOX)], aoi="pilot")
    for log in logs_from_ags((FIXTURES / "sample_ags4.ags").read_text(), {"BH01": 1001, "BH02": 1002}):
        dbm.replace_intervals(con, log, GOLD_SOURCE)

    logs, sites = flat_world(30)
    samples = discretise(logs.values(), sites)
    km = KernelLithologyModel(length_h=600.0, length_v=2.5).fit(samples)
    payload = km.bake(BBOX, 250.0, 5.0, 55.0, 105.0, kernel_surface(sites, 600.0), sites=sites, name="flat")
    payload["gates_passed"] = True
    write_model_json(payload, tmp_path / "model.json")
    con.close()

    # The server caches config/model per process; reset between tests.
    import gbg.mcp_server as srv
    srv._cfg.cache_clear()
    srv._model.cache_clear()
    return srv.build_server()


def call(server, name, **args):
    res = asyncio.run(server.call_tool(name, args))
    assert not res.is_error, res.content
    return json.loads(res.content[0].text)


def test_tools_are_listed_with_schemas(corpus):
    tools = asyncio.run(corpus.list_tools())
    names = {t.name for t in tools}
    assert names == {"pipeline_status", "count_boreholes", "nearest_boreholes", "borehole_log", "subsurface_at"}
    by_name = {t.name: t for t in tools}
    assert set(by_name["subsurface_at"].input_schema["required"]) == {"easting", "northing", "z_m_aod"}
    assert all(t.description for t in tools)


def test_status_and_counts(corpus):
    status = call(corpus, "pipeline_status")
    assert status["counts"]["boreholes"] == 5 and status["counts"]["logs_gold"] == 2
    assert status["baked_model"]["grid"] == [20, 20, 10] and status["baked_model"]["synthetic"] is False
    counts = call(corpus, "count_boreholes", min_easting=382500, min_northing=202500,
                  max_easting=387500, max_northing=207500)
    assert counts["boreholes"] == 5 and counts["with_recorded_depth"] == 4 and counts["notification_only"] == 1


def test_nearest_and_log(corpus):
    near = call(corpus, "nearest_boreholes", easting=383010, northing=203010, k=2)["boreholes"]
    assert [n["bgs_id"] for n in near] == [1001, 1002]
    assert near[0]["distance_m"] == pytest.approx(14.1, abs=0.1)
    assert near[0]["log_sources"] == [GOLD_SOURCE]
    log = call(corpus, "borehole_log", bgs_id=1001)
    assert log["source"] == GOLD_SOURCE and len(log["intervals"]) == 4
    assert log["site"]["ground_level_m"] == 78.2
    assert call(corpus, "borehole_log", bgs_id=9999)["intervals"] == []


def test_subsurface_queries(corpus):
    # Inside the flat world's limestone (ground ~95 m AOD here, limestone down to 85 m).
    hit = call(corpus, "subsurface_at", easting=385000, northing=205000, z_m_aod=87.0)
    assert hit["class"] == "LIMESTONE" and hit["constrained"] and hit["certainty"] > 0.5
    # Above ground.
    air = call(corpus, "subsurface_at", easting=385000, northing=205000, z_m_aod=104.9)
    assert air["class"] == "AIR"
    # Outside the grid gives the extent back rather than a wrong voxel.
    out = call(corpus, "subsurface_at", easting=100, northing=100, z_m_aod=90)
    assert "error" in out and out["grid_origin_bng"] == [382500.0, 202500.0]
