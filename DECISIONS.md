# Decisions

Short records: problem, what was measured, what was chosen, what the
acceptance test is. Add to the top. Abandoned approaches go here too, with
what they cost.

## 2026-09-06 — Stage 2 model and MCP server

**Problem.** Turn interval logs into a 3D model whose uncertainty is real,
and expose corpus + model to an agent without letting the agent fetch from BGS.

**Measured.** On a synthetic flat-layer world (40 boreholes, 2,276 points,
leave-one-borehole-out): kernel model 97.7% vs nearest-borehole baseline
84.7% (+13.0 pp), calibration 100% at ≥ 80%, 99.3% of points constrained at
`length_h = 600 m`; at `length_h = 300 m` the same world gave 83.1% (−1.6 pp)
because 15% of points fell below the evidence threshold — the accuracy metric
deliberately counts "unconstrained" as wrong.

**Chosen.**

- Model v0 = kernel-weighted class vote with a Dirichlet prior
  (`gbg/model.py`), anisotropic Gaussian kernel, entropy as the uncertainty
  channel, `min_evidence` marks unconstrained voxels (entropy 1.0). Chosen
  over indicator kriging for now because every number it produces can be
  explained in one sentence; the interface is the same one a PyMC/NumPyro
  model will implement later.
- Validation is leave-one-*borehole*-out, not leave-one-point-out, which
  would let a column vouch for itself.
- Elevation frame when ground levels exist (AGS `LOCA_GL`, or stated on a
  scan and extracted as `ground_level_m`); missing ground levels are
  estimated from GL-bearing neighbours and flagged `gl_estimated`; with no
  ground levels at all the model runs in depth and the baked JSON says so.
- MCP server (`gbg-mcp`, SDK 2.x `MCPServer`) is read-only over the local
  DuckDB and the baked JSON. It never imports `gbg.http`. Verified over real
  stdio with the SDK's client: initialize, list_tools, call_tool.
- SDK fact worth recording: `mcp` 2.x renamed `FastMCP` → `MCPServer` and
  result fields are snake_case (`input_schema`, `structured_content`,
  `is_error`). Code written from memory of 1.x would not import.

**Acceptance test.** 47 tests green; `gbg model pilot --tune` on the
synthetic corpus prints PROCEED and bakes 31,045 below-ground voxels; a
stdio MCP client gets LIMESTONE at (385000, 205000, 87 m AOD) and MUDSTONE at
70 m AOD from that bake.

**Cost of anything abandoned.** One `%`-format bug in the CLI table (a
literal `80%` inside a `%`-formatted string) reached the CLI run because unit
tests covered the model but not the command; fixed in twenty minutes, and it
is why `gbg model` now has a CLI smoke test in CI.

## 2026-09-06 — Scaffold

**Problem.** Start the project on verified ground rather than assumed field names.

**Measured.** Live SOBI OGC API responses (collection metadata, first items
page, queryables, OpenAPI). Whole-collection `numberMatched` = 1,358,783.
Field names and types recorded in DATA_SOURCES.md. Two traps found by reading
records, not documentation: ids are JSON floats; `length = -2.0` is a sentinel.

**Chosen.**

- Convert BNG → WGS84 client-side for the API's `bbox`, then re-filter on
  `easting`/`northing`. Reason: the collection only advertises CRS84; the
  lon/lat envelope of a BNG rectangle over-selects at the corners (~15 m at
  Stroud's grid convergence). Test: `test_enumerate_pages_through_next_links_and_trims_to_bng`.
- Completeness oracle in `SobiClient.iter_boreholes`: fetched count must
  equal `numberMatched` or the run raises. Reason: a truncated response is
  indistinguishable from a complete one without an independent count.
- Scan fetcher treats HTTP 200 without a `%PDF-` prefix as a failure and
  records it. Reason: a 200 HTML "no scan" page would otherwise be cached as
  a scan.
- Gold labels from the `agsboreholeindex` collection rather than the AGS
  utilities API (50-borehole cap). Boundaries trusted; classes derived via
  `lithology.normalise` with a stored confidence.
- Gates (0.85 boundary recall at ±0.25 m; 0.80 macro-F1; ≥20 gold) chosen
  before any extraction was run, so they cannot drift to fit a result.
- Viewer built against a synthetic model with a visible `synthetic` flag, so
  interface decisions are made before Stage 2 and nobody mistakes the
  preview for data.

**Acceptance test for the scaffold.** `pytest` green (37 tests, no network);
mock end-to-end run produces a `DO NOT PROCEED` verdict on the known-bad
fixture. Both observed on 2026-09-06.

**Not verified (sandbox could not reach BGS).** Live paging over a real bbox,
scans API behaviour for missing scans, the AGS `ags_log_url` payload format,
viewer rendering in a browser. Listed in README's Stage 0 checklist.

## Template

```
## YYYY-MM-DD — <title>
**Problem.**
**Measured.**
**Chosen.**
**Acceptance test.**
**Cost of anything abandoned.**
```
