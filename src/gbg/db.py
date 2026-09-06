"""DuckDB storage.

Tables:
  boreholes    — one row per SOBI record enumerated (raw index, normalised)
  scans        — one row per scan fetch attempt (including failures)
  intervals    — extracted or gold lithology intervals, tagged by `source`
  log_meta     — per (bgs_id, source): position / ground level stated on the log itself
  eval_runs    — one row per `gbg eval`, with the metrics and gate verdicts
  model_runs   — one row per `gbg model`, with LOO metrics and gate verdicts
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import duckdb

from .schema import BoreholeLog
from .scans import ScanResult
from .sobi import BoreholeRecord

DDL = """
CREATE TABLE IF NOT EXISTS boreholes (
    bgs_id            INTEGER PRIMARY KEY,
    reference         VARCHAR,
    name              VARCHAR,
    grid_ref          VARCHAR,
    easting           DOUBLE NOT NULL,
    northing          DOUBLE NOT NULL,
    precision         VARCHAR,
    length_m          DOUBLE,
    length_raw        DOUBLE,
    year_known        VARCHAR,
    held_at           VARCHAR,
    length_scan_cat   VARCHAR,
    scan_url          VARCHAR,
    ags_log_url       VARCHAR,
    water_well_ref    VARCHAR,
    scan_quality      VARCHAR,
    date_updated      VARCHAR,
    notification_only BOOLEAN,
    lon               DOUBLE,
    lat               DOUBLE,
    aoi               VARCHAR,
    enumerated_at     TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS scans (
    bgs_id        INTEGER,
    status        INTEGER,
    content_type  VARCHAR,
    bytes         BIGINT,
    sha256        VARCHAR,
    pages         INTEGER,
    path          VARCHAR,
    ok            BOOLEAN,
    note          VARCHAR,
    fetched_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS intervals (
    bgs_id          INTEGER,
    source          VARCHAR,     -- 'gold:ags' | 'extract:<provider>/<model>'
    seq             INTEGER,
    top_m           DOUBLE,
    base_m          DOUBLE,
    raw_description VARCHAR,
    lith_class      VARCHAR,
    confidence      DOUBLE,
    extracted_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS log_meta (
    bgs_id          INTEGER,
    source          VARCHAR,
    easting         DOUBLE,
    northing        DOUBLE,
    ground_level_m  DOUBLE,
    total_depth_m   DOUBLE,
    PRIMARY KEY (bgs_id, source)
);

CREATE TABLE IF NOT EXISTS model_runs (
    run_id               VARCHAR,
    source               VARCHAR,
    frame                VARCHAR,
    n_boreholes          INTEGER,
    n_points             INTEGER,
    accuracy             DOUBLE,
    baseline_accuracy    DOUBLE,
    gain_pp              DOUBLE,
    calibration_at_80    DOUBLE,
    gates_passed         BOOLEAN,
    model_path           VARCHAR,
    ran_at               TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id            VARCHAR,
    source            VARCHAR,
    n_gold            INTEGER,
    boundary_recall   DOUBLE,
    boundary_precision DOUBLE,
    lithology_f1      DOUBLE,
    depth_accuracy    DOUBLE,
    gates_passed      BOOLEAN,
    report_path       VARCHAR,
    ran_at            TIMESTAMP DEFAULT current_timestamp
);
"""


def connect(path: Path | str) -> duckdb.DuckDBPyConnection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(DDL)
    return con


def upsert_boreholes(con, records: Iterable[BoreholeRecord], aoi: str) -> int:
    rows = [(*r.as_row().values(), aoi) for r in records]
    if not rows:
        return 0
    placeholders = ",".join("?" * 21)
    con.executemany(
        f"INSERT OR REPLACE INTO boreholes VALUES ({placeholders}, current_timestamp)", rows
    )
    return len(rows)


def record_scan(con, r: ScanResult) -> None:
    con.execute(
        "INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?, current_timestamp)",
        [r.bgs_id, r.status, r.content_type, r.bytes, r.sha256, r.pages,
         str(r.path) if r.path else None, r.ok, r.note],
    )


def replace_intervals(con, log: BoreholeLog, source: str) -> int:
    con.execute("DELETE FROM intervals WHERE bgs_id = ? AND source = ?", [log.bgs_id, source])
    rows = [
        (log.bgs_id, source, i, iv.top_m, iv.base_m, iv.raw_description, iv.lith_class,
         iv.confidence)
        for i, iv in enumerate(log.intervals)
    ]
    con.executemany(
        "INSERT INTO intervals VALUES (?,?,?,?,?,?,?,?, current_timestamp)", rows
    )
    con.execute(
        "INSERT OR REPLACE INTO log_meta VALUES (?,?,?,?,?,?)",
        [log.bgs_id, source, log.easting, log.northing, log.ground_level_m, log.total_depth_m],
    )
    return len(rows)


def load_logs(con, source: str) -> dict[int, BoreholeLog]:
    """Reassemble BoreholeLog objects for one source, keyed by bgs_id."""
    from .schema import LithInterval

    rows = con.execute(
        "SELECT bgs_id, seq, top_m, base_m, raw_description, lith_class, confidence "
        "FROM intervals WHERE source = ? ORDER BY bgs_id, seq",
        [source],
    ).fetchall()
    logs: dict[int, list[LithInterval]] = {}
    for bgs_id, _seq, top, base, desc, cls, conf in rows:
        logs.setdefault(bgs_id, []).append(
            LithInterval(top_m=top, base_m=base, raw_description=desc or "", lith_class=cls,
                         confidence=conf)
        )
    meta = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT bgs_id, easting, northing, ground_level_m, total_depth_m "
            "FROM log_meta WHERE source = ?", [source]
        ).fetchall()
    }
    out = {}
    for k, v in logs.items():
        e, n, gl, td = meta.get(k, (None, None, None, None))
        out[k] = BoreholeLog(bgs_id=k, intervals=v, source=source, easting=e, northing=n,
                             ground_level_m=gl, total_depth_m=td)
    return out


def load_sites(con, source: str):
    """BoreholeSite per bgs_id for a source: log-stated position first, SOBI index second."""
    from .model import BoreholeSite

    rows = con.execute(
        "SELECT m.bgs_id, coalesce(m.easting, b.easting), coalesce(m.northing, b.northing), "
        "m.ground_level_m FROM log_meta m LEFT JOIN boreholes b USING (bgs_id) WHERE m.source = ?",
        [source],
    ).fetchall()
    sites = {}
    for bgs_id, e, n, gl in rows:
        if e is None or n is None:
            continue  # no position from either the log or the index: cannot be modelled
        sites[bgs_id] = BoreholeSite(bgs_id=bgs_id, easting=float(e), northing=float(n),
                                     ground_level_m=(float(gl) if gl is not None else None))
    return sites


def summary(con) -> dict[str, int]:
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "boreholes": q("SELECT count(*) FROM boreholes"),
        "boreholes_with_depth": q("SELECT count(*) FROM boreholes WHERE length_m IS NOT NULL"),
        "notification_only": q("SELECT count(*) FROM boreholes WHERE notification_only"),
        "scans_ok": q("SELECT count(DISTINCT bgs_id) FROM scans WHERE ok"),
        "scans_failed": q("SELECT count(*) FROM scans WHERE NOT ok"),
        "logs_gold": q("SELECT count(DISTINCT bgs_id) FROM intervals WHERE source LIKE 'gold:%'"),
        "logs_extracted": q(
            "SELECT count(DISTINCT bgs_id) FROM intervals WHERE source LIKE 'extract:%'"
        ),
        "eval_runs": q("SELECT count(*) FROM eval_runs"),
        "model_runs": q("SELECT count(*) FROM model_runs"),
    }
