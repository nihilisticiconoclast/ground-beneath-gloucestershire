# Decisions

Short records: problem, what was measured, what was chosen, what the
acceptance test is. Add to the top. Abandoned approaches go here too, with
what they cost.

## 2026-09-06 — Repository unpacked; viewer published from CI

**Problem.** The repository held the project as an uploaded zip plus three
loose copies of files from inside it, so nothing ran from a checkout, and the
GitHub Pages site (already switched on, source "deploy from a branch") served
a Jekyll rendering of a one-line README.

**Measured.** From a checkout of the unpacked layout on Python 3.11:
`pytest` 49 passed (the README said 47; corrected). The CI smoke step
reproduced the Stage 2 numbers exactly (40 boreholes, 2,276 points, 97.7% vs
84.7%, +13.0 pp, 31,045 below-ground voxels), and the eval gate fired on the
known-bad fixture. `scripts/make_sample_voxels.py` regenerates
`web/data/sample_voxels.json` byte for byte. The viewer rendered in headless
Chromium (software WebGL2) against that file: a legend of 7 classes summing
to 33,212 voxels, the peel and certainty sliders and the hover probe all
responded, and the only console error was a missing favicon. The same check
showed the peel slider inverted against its ruler: at "surface" the thumb sat
beside the 40 m tick. Rotating the input anticlockwise instead of clockwise
fixed it; at 80% the readout says 100 m AOD and the thumb sits on the 100 m
tick. Three.js had to be served from local copies for that check because the
sandbox proxy reset Chromium's CDN connections; the unpkg URLs themselves
answered 200 to curl.

**Chosen.**

- Bundle contents moved to the repository root; the zip, the loose duplicates
  and the empty `{src/...}` directory tree (a shell brace-expansion accident
  inside the zip) removed.
- Pages stays a CI deployment of `web/` after the tests pass, as designed.
  Changes to `ci.yml`: `workflow_dispatch`, so a human can publish
  deliberately; `cache-dependency-path: pyproject.toml`, since setup-python's
  pip cache looks for `**/requirements.txt` by default and there is none;
  Pages permissions scoped to the `pages` job; a concurrency group so deploys
  never overlap; the post-deploy check retries for up to three minutes and
  downloads to files rather than piping into `head`/`grep -q`, so a closed
  pipe cannot make `curl` report failure.
- The Pages source must be "GitHub Actions". `configure-pages` is asked to
  create the site if none exists, and the README records the one-time
  settings step.

**Acceptance test.** The `pages` job's last step: the live URL serves a page
containing "The ground beneath Gloucestershire", and `data/sample_voxels.json`
starts with `"synthetic":true`. Not yet observed: the job runs on `main`, and
this change was made on a branch.

**Cost of anything abandoned.** Nothing abandoned. Not verified here: the
viewer loading three.js from unpkg end to end in a browser (only each half
separately, see above), and the Pages source switching on the first
workflow deployment rather than needing the settings change by hand.

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
