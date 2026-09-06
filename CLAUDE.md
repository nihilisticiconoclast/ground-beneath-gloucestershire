# CLAUDE.md — working rules for this repository

## What this is

A pipeline that reconstructs a 3D lithology model of Gloucestershire from BGS
scanned borehole logs. Read `README.md` (stage plan), `docs/GATES.md` (the
numbers that decide progress) and `docs/DATA_SOURCES.md` (verified API shapes)
before changing anything. `config/gbg.toml` holds every tunable; do not
hard-code AOIs, rate limits or thresholds in Python.

## Non-negotiables

1. **Never bulk-download BGS.** All HTTP goes through `gbg.http.PoliteClient`
   (throttled, retrying, identifying User-Agent). Do not add a second client,
   do not raise `requests_per_second` above 1.0, do not remove the per-run scan
   cap. BGS's terms forbid bulk download of the entire dataset and they will
   block IPs. Scope is one county.
2. **Nothing is true because a process finished.** Every ingest step reports a
   count and compares it with an independent one (`numberMatched`, bytes on
   disk, rows in DuckDB). If you cannot name the observation that would show a
   step failed, the step is not done.
3. **Gates are set before results exist.** Do not lower a threshold in
   `config/gbg.toml` to make a run pass. If extraction fails the gate three
   times, the next move is a different approach, recorded in
   `docs/DECISIONS.md` with the numbers.
4. **Uncertainty is data.** Extractors must return per-interval confidence;
   the model must emit per-voxel entropy; the viewer shows both. A confident
   wrong value is worse than a visible gap. Never fill a gap with a guess.
5. **Prose and data are separately trustworthy.** Any identifier, count or
   percentage you write in a doc or message must come from a query you ran,
   not from memory of one.

## Conventions

- Python 3.11+, `src/` layout, package `gbg`. Types on public functions.
  Pydantic models in `gbg.schema` are the data contract — change them only
  with a migration note in `docs/DECISIONS.md`.
- Coordinates: BNG (EPSG:27700) everywhere internally; WGS84 only at the API
  boundary (`gbg.bng`).
- Tests use `httpx.MockTransport` fakes in `tests/conftest.py` that mirror the
  live API's shape *including its traps*. When you learn a new trap from the
  live service, add it to the fake and to `docs/DATA_SOURCES.md` on the same
  commit.
- `data/` is local and git-ignored. Only `data/README.md` is tracked.
- `web/` is static and deployed by GitHub Pages from `main`. It loads
  `web/data/*.json`; it must never call BGS directly.
- `gbg/mcp_server.py` must never import `gbg.http` or `gbg.sobi`/`gbg.scans`
  fetchers. Agents read; humans fetch.
- Don't add dependencies for things the standard library does (tomllib, csv,
  json). Heavy optional deps go in `pyproject.toml` extras.

## Before saying something works

- [ ] What observation would show this is wrong, and did I make it?
- [ ] Am I quoting what I verified, or what a process reported?
- [ ] Does every identifier in what I wrote exist in the thing I'm describing?
- [ ] Would the reader know what I could not check?

## Useful commands

```
python -m pytest -q                     # no network, ~2 s
gbg probe                               # is the API up; does bbox change the count
gbg enumerate pilot && gbg stats
gbg fetch-scans pilot --limit 20
gbg gold data/gold/*.ags --id-map data/gold/ids.csv
gbg extract pilot --provider ollama
gbg eval                                # exit code 1 on gate failure
gbg model pilot --tune                  # Stage 2: LOO validation + bake web/data/model.json
gbg-mcp                                 # MCP server over stdio (reads local data only)
python scripts/make_sample_voxels.py    # regenerate the synthetic preview
```

## Facts that were verified rather than remembered

- BGS OGC API shape: see docs/DATA_SOURCES.md (2026-09-06).
- `mcp` Python SDK is 2.x: `from mcp.server.mcpserver import MCPServer`;
  tool results expose `content`, `structured_content`, `is_error`;
  `Tool.input_schema` is snake_case. `FastMCP` no longer exists.
- Claude Code registers a stdio server with
  `claude mcp add --transport stdio --scope project gbg -- gbg-mcp`
  (project scope writes `.mcp.json`, which is committed).
