"""MCP server: let an agent ask the ground questions.

Runs over stdio. Exposes what the pipeline has already built — the local
DuckDB corpus and the baked voxel model — and *never* calls BGS itself, so an
agent cannot become a bulk downloader by accident. Fetching stays a deliberate
`gbg fetch-scans` by a human.

Register with Claude Code from the repo root (project scope writes .mcp.json):

    claude mcp add --transport stdio --scope project gbg -- gbg-mcp

or point another client at the `gbg-mcp` executable.

Tools:
  pipeline_status        what exists: counts, latest eval and model verdicts
  count_boreholes        boreholes in a BNG bbox (local index)
  nearest_boreholes      k nearest boreholes to a point, with what we hold for each
  borehole_log           the lithology intervals for one borehole (gold or extracted)
  subsurface_at          class probabilities + uncertainty at (easting, northing, z)
"""

from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path

from . import db as dbm
from .config import load_config


def _config_path() -> Path:
    return Path(os.environ.get("GBG_CONFIG", "config/gbg.toml"))


@lru_cache(maxsize=1)
def _cfg():
    return load_config(_config_path())


def _con():
    # One connection per call keeps the server stateless and safe to restart;
    # DuckDB opens in milliseconds.
    return dbm.connect(_cfg().paths.db)


@lru_cache(maxsize=1)
def _model():
    path = Path(os.environ.get("GBG_MODEL", str(_cfg().model.output)))
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_server():
    from mcp.server.mcpserver import MCPServer  # mcp >= 2

    server = MCPServer(
        "ground-beneath-gloucestershire",
        instructions=(
            "Read-only access to a local corpus of BGS borehole records and a baked 3D "
            "lithology model. Coordinates are British National Grid metres (EPSG:27700); "
            "elevations are metres above Ordnance Datum. Uncertainty is reported, not hidden: "
            "an 'unconstrained' voxel means no borehole is near enough to say."
        ),
    )

    @server.tool()
    def pipeline_status() -> dict:
        """Counts of boreholes, scans, logs and the latest eval / model gate verdicts."""
        con = _con()
        summary = dbm.summary(con)
        latest_eval = con.execute(
            "SELECT run_id, source, n_gold, boundary_recall, lithology_f1, gates_passed "
            "FROM eval_runs ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
        latest_model = con.execute(
            "SELECT run_id, source, frame, n_boreholes, accuracy, baseline_accuracy, gain_pp, "
            "calibration_at_80, gates_passed, model_path FROM model_runs ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
        m = _model()
        return {
            "counts": summary,
            "latest_eval": None if not latest_eval else dict(zip(
                ["run_id", "source", "n_gold", "boundary_recall", "lithology_f1", "gates_passed"],
                latest_eval)),
            "latest_model": None if not latest_model else dict(zip(
                ["run_id", "source", "frame", "n_boreholes", "accuracy", "baseline_accuracy",
                 "gain_pp", "calibration_at_80", "gates_passed", "model_path"], latest_model)),
            "baked_model": None if m is None else {
                "name": m.get("name"), "frame": m.get("frame"), "synthetic": m.get("synthetic"),
                "grid": [m["nx"], m["ny"], m["nz"]], "cell_xy": m["cell_xy"], "cell_z": m["cell_z"],
                "constrained_voxels": m.get("constrained_voxels"),
                "gates_passed": m.get("gates_passed"),
            },
        }

    @server.tool()
    def count_boreholes(min_easting: float, min_northing: float, max_easting: float, max_northing: float) -> dict:
        """Count indexed boreholes inside a BNG bounding box, split by what we hold for them."""
        con = _con()
        row = con.execute(
            "SELECT count(*), count(length_m), sum(CASE WHEN notification_only THEN 1 ELSE 0 END), "
            "count(DISTINCT s.bgs_id) "
            "FROM boreholes b LEFT JOIN (SELECT DISTINCT bgs_id FROM scans WHERE ok) s USING (bgs_id) "
            "WHERE easting BETWEEN ? AND ? AND northing BETWEEN ? AND ?",
            [min_easting, max_easting, min_northing, max_northing],
        ).fetchone()
        return {"boreholes": row[0], "with_recorded_depth": row[1],
                "notification_only": int(row[2] or 0), "with_scan_on_disk": row[3],
                "note": "counts are from the local index, not a live BGS query"}

    @server.tool()
    def nearest_boreholes(easting: float, northing: float, k: int = 5) -> dict:
        """The k nearest indexed boreholes to a BNG point, with distance and what we hold."""
        con = _con()
        k = max(1, min(int(k), 50))
        # Union of the SOBI index and log-stated positions (gold boreholes that have no
        # index mapping yet carry negative ids and live only in log_meta).
        rows = con.execute(
            "WITH sites AS ("
            "  SELECT bgs_id, name, reference, easting, northing, length_m, year_known FROM boreholes "
            "  UNION ALL "
            "  SELECT m.bgs_id, 'log-stated position' AS name, m.source AS reference, m.easting, "
            "         m.northing, m.total_depth_m, NULL FROM log_meta m "
            "  WHERE m.easting IS NOT NULL AND m.bgs_id NOT IN (SELECT bgs_id FROM boreholes)"
            ") "
            "SELECT b.bgs_id, b.name, b.reference, b.easting, b.northing, b.length_m, b.year_known, "
            "sqrt((b.easting-?)*(b.easting-?) + (b.northing-?)*(b.northing-?)) AS dist, "
            "(SELECT count(*) FROM scans s WHERE s.bgs_id = b.bgs_id AND s.ok) > 0 AS has_scan, "
            "(SELECT list(DISTINCT source) FROM intervals i WHERE i.bgs_id = b.bgs_id) AS sources "
            "FROM sites b ORDER BY dist LIMIT ?",
            [easting, easting, northing, northing, k],
        ).fetchall()
        cols = ["bgs_id", "name", "reference", "easting", "northing", "length_m", "year_known",
                "distance_m", "has_scan", "log_sources"]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["distance_m"] = round(float(d["distance_m"]), 1)
            d["log_sources"] = list(d["log_sources"] or [])
            out.append(d)
        return {"query": {"easting": easting, "northing": northing, "k": k}, "boreholes": out}

    @server.tool()
    def borehole_log(bgs_id: int, source: str | None = None) -> dict:
        """Lithology intervals for one borehole. Default source: gold if present, else any extraction."""
        con = _con()
        sources = [r[0] for r in con.execute(
            "SELECT DISTINCT source FROM intervals WHERE bgs_id = ? ORDER BY source", [bgs_id]
        ).fetchall()]
        if not sources:
            return {"bgs_id": bgs_id, "intervals": [], "note": "no log held for this borehole"}
        chosen = source or next((s for s in sources if s.startswith("gold:")), sources[0])
        rows = con.execute(
            "SELECT top_m, base_m, lith_class, raw_description, confidence FROM intervals "
            "WHERE bgs_id = ? AND source = ? ORDER BY seq", [bgs_id, chosen]
        ).fetchall()
        meta = con.execute(
            "SELECT easting, northing, ground_level_m, total_depth_m FROM log_meta "
            "WHERE bgs_id = ? AND source = ?", [bgs_id, chosen]
        ).fetchone()
        return {
            "bgs_id": bgs_id, "source": chosen, "available_sources": sources,
            "site": None if not meta else dict(zip(["easting", "northing", "ground_level_m", "total_depth_m"], meta)),
            "intervals": [dict(zip(["top_m", "base_m", "lith_class", "raw_description", "confidence"], r)) for r in rows],
        }

    @server.tool()
    def subsurface_at(easting: float, northing: float, z_m_aod: float) -> dict:
        """What the baked model says is at a BNG point and elevation: class, certainty, and whether it is constrained."""
        m = _model()
        if m is None:
            return {"error": "no baked model; run `gbg model <aoi>` first"}
        i = int(math.floor((easting - m["origin_bng"][0]) / m["cell_xy"]))
        j = int(math.floor((northing - m["origin_bng"][1]) / m["cell_xy"]))
        k = int(math.floor((z_m_aod - m["z0"]) / m["cell_z"]))
        if not (0 <= i < m["nx"] and 0 <= j < m["ny"] and 0 <= k < m["nz"]):
            return {"error": "point is outside the baked grid", "grid_origin_bng": m["origin_bng"],
                    "grid_extent_m": [m["nx"] * m["cell_xy"], m["ny"] * m["cell_xy"]],
                    "z_range": [m["z0"], m["z0"] + m["nz"] * m["cell_z"]]}
        idx = k * m["nx"] * m["ny"] + j * m["nx"] + i
        cls = m["class_idx"][idx]
        surface = m["surface"][j * m["nx"] + i]
        if cls == 255:
            return {"class": "AIR", "surface_m_aod": surface, "note": "above the ground surface at this column"}
        entropy = m["entropy"][idx]
        return {
            "class": m["classes"][cls],
            "certainty": round(1 - entropy, 2),
            "entropy": entropy,
            "constrained": entropy < 0.999,
            "voxel": {"easting": m["origin_bng"][0] + (i + 0.5) * m["cell_xy"],
                      "northing": m["origin_bng"][1] + (j + 0.5) * m["cell_xy"],
                      "z_top_m_aod": m["z0"] + (k + 1) * m["cell_z"],
                      "z_base_m_aod": m["z0"] + k * m["cell_z"]},
            "surface_m_aod": surface,
            "frame": m.get("frame", "elevation"),
            "model": m.get("name"),
            "synthetic": bool(m.get("synthetic", False)),
        }

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
