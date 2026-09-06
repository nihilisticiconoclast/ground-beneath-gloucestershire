"""Command-line entry points. One verb per pipeline stage.

    gbg probe                 sanity-check the API and the bbox parameter (no writes)
    gbg enumerate pilot       index every borehole in an AOI into DuckDB
    gbg fetch-scans pilot     download scan PDFs (throttled, capped, cached)
    gbg gold <ags files...>   load AGS files as gold-standard logs
    gbg extract pilot         run the configured extractor over cached scans
    gbg eval                  score extractions against gold and apply the gates
    gbg model pilot           fit + validate the 3D model, bake voxels for the viewer
    gbg stats                 what the database currently holds
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import db as dbm
from .ags import GOLD_SOURCE, logs_from_ags
from .config import Config, load_config
from .evals import evaluate, write_report
from .extract import make_extractor, render_pdf_pages
from .http import PoliteClient
from .scans import ScanFetcher
from .sobi import CompletenessError, SobiClient

app = typer.Typer(add_completion=False, help="The Ground Beneath Gloucestershire pipeline.")
console = Console()

CONFIG_OPT = typer.Option("config/gbg.toml", "--config", "-c", help="Path to gbg.toml")


def _cfg(path: str) -> Config:
    cfg = load_config(path)
    if "YOUR-EMAIL-HERE" in cfg.client.user_agent:
        console.print("[yellow]config/gbg.toml: set a real contact in client.user_agent before talking to BGS.[/]")
    return cfg


@app.command()
def probe(config: str = CONFIG_OPT) -> None:
    """Confirm the API answers, and that `bbox` changes the answer (no database writes)."""
    cfg = _cfg(config)
    with PoliteClient(cfg.client) as http:
        sobi = SobiClient(http)
        counts = {name: sobi.count(aoi.bbox_bng) for name, aoi in cfg.aois.items()}
    table = Table(title="numberMatched by AOI")
    table.add_column("aoi")
    table.add_column("bbox (BNG)")
    table.add_column("boreholes", justify="right")
    for name, n in counts.items():
        table.add_row(name, str(list(cfg.aois[name].bbox_bng)), f"{n:,}")
    console.print(table)
    ordered = sorted(counts.items(), key=lambda kv: kv[1])
    if len({v for _, v in counts.items()}) < len(counts):
        console.print("[red]Two AOIs returned the same count — the bbox parameter may be ignored.[/]")
        raise typer.Exit(code=2)
    console.print(f"bbox parameter is honoured (counts differ: {ordered[0][1]:,} … {ordered[-1][1]:,}).")


@app.command()
def enumerate(aoi: str, config: str = CONFIG_OPT, page_size: int = 500) -> None:
    """Index every borehole in an AOI into DuckDB, and check the count against the API's own."""
    cfg = _cfg(config)
    area = cfg.aoi(aoi)
    con = dbm.connect(cfg.paths.db)
    with PoliteClient(cfg.client) as http:
        sobi = SobiClient(http)
        expected = sobi.count(area.bbox_bng)
        console.print(f"{area.name}: API reports {expected:,} boreholes in the lon/lat envelope")
        try:
            records = list(sobi.iter_boreholes(area.bbox_bng, page_size=page_size))
        except CompletenessError as exc:
            console.print(f"[red]{exc}[/] — nothing written")
            raise typer.Exit(code=2)
    n = dbm.upsert_boreholes(con, records, aoi=area.name)
    trimmed = expected - n
    console.print(
        f"stored {n:,} boreholes inside the BNG rectangle "
        f"({trimmed:,} envelope-only records outside it were dropped) in {http.request_count} requests"
    )
    with_depth = sum(1 for r in records if r.length_m is not None)
    notif = sum(1 for r in records if r.notification_only)
    console.print(f"with a recorded depth: {with_depth:,} · notification-only: {notif:,}")


@app.command("fetch-scans")
def fetch_scans(
    aoi: str,
    config: str = CONFIG_OPT,
    limit: int | None = typer.Option(None, help="Override client.max_scans_per_run"),
    skip_notification_only: bool = True,
) -> None:
    """Download scan PDFs for boreholes in an AOI. Throttled, capped per run, cached forever."""
    cfg = _cfg(config)
    area = cfg.aoi(aoi)
    con = dbm.connect(cfg.paths.db)
    where = "aoi = ?" + (" AND NOT notification_only" if skip_notification_only else "")
    ids = [
        r[0]
        for r in con.execute(
            f"SELECT bgs_id FROM boreholes WHERE {where} "
            "AND bgs_id NOT IN (SELECT bgs_id FROM scans WHERE ok) "
            "ORDER BY length_m DESC NULLS LAST, bgs_id",
            [area.name],
        ).fetchall()
    ]
    cap = limit or cfg.client.max_scans_per_run
    console.print(f"{len(ids):,} boreholes without a good scan; fetching up to {cap} this run")
    ok = failed = 0
    with PoliteClient(cfg.client) as http:
        fetcher = ScanFetcher(http, cfg.paths.raw_scans, max_per_run=cap)
        for bgs_id in ids[:cap]:
            res = fetcher.fetch(bgs_id)
            dbm.record_scan(con, res)
            ok += int(res.ok)
            failed += int(not res.ok)
            if not res.ok:
                console.print(f"  {bgs_id}: [yellow]{res.note}[/] (status {res.status}, {res.content_type})")
    console.print(f"fetched ok: {ok} · failed: {failed} · requests made: {http.request_count}")


@app.command()
def gold(
    files: list[Path],
    config: str = CONFIG_OPT,
    id_map: Path | None = typer.Option(None, help="CSV of ags_hole_id,bgs_id"),
) -> None:
    """Load AGS files as gold-standard logs (source 'gold:ags')."""
    cfg = _cfg(config)
    con = dbm.connect(cfg.paths.db)
    mapping: dict[str, int] = {}
    if id_map:
        for line in id_map.read_text(encoding="utf-8").splitlines():
            if "," in line and not line.lower().startswith("ags_hole_id"):
                hole, bid = line.split(",", 1)
                mapping[hole.strip()] = int(bid)
    total = 0
    for f in files:
        logs = logs_from_ags(f.read_text(encoding="utf-8", errors="replace"), mapping)
        for log in logs:
            dbm.replace_intervals(con, log, GOLD_SOURCE)
        total += len(logs)
        console.print(f"{f}: {len(logs)} logs")
    unmapped = con.execute(
        "SELECT count(DISTINCT bgs_id) FROM intervals WHERE source = ? AND bgs_id < 0", [GOLD_SOURCE]
    ).fetchone()[0]
    console.print(f"loaded {total} gold logs; {unmapped} have no BGS id mapping yet (negative ids)")


@app.command()
def extract(
    aoi: str,
    config: str = CONFIG_OPT,
    provider: str | None = typer.Option(None, help="mock | ollama | anthropic (default from config)"),
    limit: int = 50,
    fixture_dir: Path | None = None,
) -> None:
    """Run the extractor over cached scans that don't yet have a log from this provider."""
    cfg = _cfg(config)
    area = cfg.aoi(aoi)
    con = dbm.connect(cfg.paths.db)
    extractor = make_extractor(provider or cfg.extraction.provider, cfg.extraction, fixture_dir)
    rows = con.execute(
        "SELECT s.bgs_id, s.path, b.length_m FROM scans s JOIN boreholes b USING (bgs_id) "
        "WHERE s.ok AND b.aoi = ? AND s.bgs_id NOT IN "
        "(SELECT bgs_id FROM intervals WHERE source = ?) ORDER BY s.bgs_id LIMIT ?",
        [area.name, extractor.source, limit],
    ).fetchall()
    console.print(f"{len(rows)} scans to extract with {extractor.source}")
    done = errors = 0
    for bgs_id, pdf_path, index_len in rows:
        try:
            pages = render_pdf_pages(Path(pdf_path), cfg.paths.page_images, dpi=cfg.extraction.render_dpi)
            log = extractor.extract(bgs_id, pages)
            dbm.replace_intervals(con, log, extractor.source)
            done += 1
            for w in log.plausibility_warnings(index_len):
                console.print(f"  {bgs_id}: [yellow]{w}[/]")
        except Exception as exc:  # noqa: BLE001 — one bad scan must not stop the run
            errors += 1
            console.print(f"  {bgs_id}: [red]{type(exc).__name__}: {exc}[/]")
    console.print(f"extracted: {done} · errors: {errors}")


@app.command("eval")
def eval_cmd(
    config: str = CONFIG_OPT,
    source: str | None = typer.Option(None, help="extraction source to score (default: config provider)"),
    out_dir: Path = Path("data/derived/evals"),
) -> None:
    """Score extracted logs against gold and apply the gates. Exit code 1 if the gates fail."""
    cfg = _cfg(config)
    con = dbm.connect(cfg.paths.db)
    if source is None:
        source = make_extractor(cfg.extraction.provider, cfg.extraction).source
    gold_logs = dbm.load_logs(con, GOLD_SOURCE)
    preds = dbm.load_logs(con, source)
    preds = {k: v for k, v in preds.items() if k in gold_logs}
    report = evaluate(gold_logs, preds, cfg.gates, source)
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    md, _ = write_report(report, out_dir, run_id)
    con.execute(
        "INSERT INTO eval_runs VALUES (?,?,?,?,?,?,?,?,?, current_timestamp)",
        [run_id, source, report.n_gold, report.boundary_recall, report.boundary_precision,
         report.lithology_f1, report.depth_accuracy, report.gates_passed, str(md)],
    )
    console.print(report.to_markdown())
    console.print(f"report: {md}")
    if not report.gates_passed:
        raise typer.Exit(code=1)


@app.command()
def model(
    aoi: str,
    config: str = CONFIG_OPT,
    source: str | None = typer.Option(None, help="interval source to model (default: 'gold:ags')"),
    tune: bool = typer.Option(False, help="grid-search the kernel length scales on LOO accuracy"),
    bake: bool = typer.Option(True, help="write the voxel JSON for the viewer if the gates pass"),
    force: bool = typer.Option(False, help="bake even if the gates fail (the file is flagged)"),
    out: Path | None = typer.Option(None, help="override [model].output"),
) -> None:
    """Stage 2: fit the 3D lithology model, validate it leave-one-borehole-out, bake voxels."""
    from .model import (KernelLithologyModel, discretise, estimate_ground_levels,
                        kernel_surface, write_model_json)

    cfg = _cfg(config)
    area = cfg.aoi(aoi)
    con = dbm.connect(cfg.paths.db)
    source = source or GOLD_SOURCE
    logs = dbm.load_logs(con, source)
    sites = dbm.load_sites(con, source)
    x0, y0, x1, y1 = area.bbox_bng
    sites = {k: s for k, s in sites.items() if x0 <= s.easting <= x1 and y0 <= s.northing <= y1}
    logs = {k: v for k, v in logs.items() if k in sites}
    if not logs:
        console.print(f"[red]no logs from source {source!r} with a position inside {area.name}[/]")
        raise typer.Exit(code=2)
    n_gl = sum(1 for s in sites.values() if s.ground_level_m is not None)
    sites = estimate_ground_levels(sites, length_h=cfg.model.length_h)
    n_est = sum(1 for s in sites.values() if s.gl_estimated)
    still_missing = [k for k, s in sites.items() if s.ground_level_m is None]
    if n_gl and still_missing:
        console.print(f"[yellow]{len(still_missing)} boreholes have no ground level and none nearby to "
                      f"estimate from; they are left out of the elevation-frame model[/]")
        sites = {k: s for k, s in sites.items() if s.ground_level_m is not None}
        logs = {k: v for k, v in logs.items() if k in sites}
    samples = discretise(logs.values(), sites, step_m=cfg.model.sample_step_m)
    console.print(
        f"{len(logs)} boreholes → {len(samples):,} labelled points · frame: {samples.frame} · "
        f"ground levels stated: {n_gl}, estimated: {n_est}"
    )
    km = KernelLithologyModel(length_h=cfg.model.length_h, length_v=cfg.model.length_v,
                              prior_strength=cfg.model.prior_strength,
                              min_evidence=cfg.model.min_evidence)
    res = km.tune(samples) if tune else km.loo_validate(samples)
    gates = {
        "enough_boreholes": res["n_boreholes"] >= cfg.gates.min_model_boreholes,
        "beats_baseline": res["gain_pp"] is not None and res["gain_pp"] >= cfg.gates.min_model_gain_pp,
        "calibrated": res["calibration_at_threshold"] is not None
        and res["calibration_at_threshold"] >= cfg.gates.min_calibration_at_80,
    }
    passed = all(gates.values())
    pct = lambda v: "n/a" if v is None else f"{100 * v:.1f}%"  # noqa: E731
    table = Table(title=f"Leave-one-borehole-out · {source} · {area.name}")
    table.add_column("metric"); table.add_column("value", justify="right")
    table.add_row("boreholes / points", f"{res['n_boreholes']} / {res['n_points']:,}")
    table.add_row("model accuracy", pct(res["accuracy"]))
    table.add_row("nearest-borehole baseline", pct(res["baseline_accuracy"]))
    table.add_row(f"gain (gate ≥ {cfg.gates.min_model_gain_pp:.0f} pp)",
                  "n/a" if res["gain_pp"] is None else f"{res['gain_pp']:+.1f} pp")
    table.add_row(f"calibration at ≥80% (gate ≥ {100 * cfg.gates.min_calibration_at_80:.0f}%)",
                  pct(res["calibration_at_threshold"]))
    table.add_row("fraction called ≥80% sure", pct(res["confident_fraction"]))
    table.add_row("fraction constrained", pct(res["constrained_fraction"]))
    table.add_row("length scales h / v", f"{km.length_h:.0f} m / {km.length_v:.1f} m")
    console.print(table)
    console.print("Gates: " + ", ".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in gates.items()))
    console.print(f"[bold]{'PROCEED' if passed else 'DO NOT PROCEED'}[/]")

    model_path = None
    if bake and (passed or force):
        km.fit(samples)
        if samples.frame == "elevation":
            surface_fn = kernel_surface(sites, length_h=cfg.model.length_h)
            zmin = float(samples.xyz[:, 2].min()) - 2 * cfg.model.cell_z
            zmax = float(max(s.ground_level_m for s in sites.values())) + cfg.model.cell_z
        else:
            surface_fn = kernel_surface({}, fallback=0.0)
            zmin, zmax = float(samples.xyz[:, 2].min()) - 2 * cfg.model.cell_z, 0.0
        z0 = math.floor(zmin / cfg.model.cell_z) * cfg.model.cell_z
        payload = km.bake(area.bbox_bng, cfg.model.cell_xy, cfg.model.cell_z, z0, zmax, surface_fn,
                          sites=sites, name=f"{area.description} — {source}")
        payload["gates_passed"] = passed
        payload["validation"] = {k: v for k, v in res.items() if k != "per_borehole"}
        model_path = write_model_json(payload, out or cfg.model.output)
        console.print(f"baked {payload['below_ground_voxels']:,} below-ground voxels "
                      f"({payload['constrained_voxels']:,} constrained) → {model_path}"
                      + ("" if passed else "  [yellow](gates failed; flagged in file)[/]"))
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    con.execute(
        "INSERT INTO model_runs VALUES (?,?,?,?,?,?,?,?,?,?,?, current_timestamp)",
        [run_id, source, samples.frame, res["n_boreholes"], res["n_points"], res["accuracy"],
         res["baseline_accuracy"], res["gain_pp"], res["calibration_at_threshold"], passed,
         str(model_path) if model_path else None],
    )
    if not passed:
        raise typer.Exit(code=1)


@app.command()
def stats(config: str = CONFIG_OPT) -> None:
    """What the database holds right now."""
    cfg = _cfg(config)
    con = dbm.connect(cfg.paths.db)
    table = Table(title=str(cfg.paths.db))
    table.add_column("measure")
    table.add_column("count", justify="right")
    for k, v in dbm.summary(con).items():
        table.add_row(k, f"{v:,}")
    console.print(table)


if __name__ == "__main__":
    app()
