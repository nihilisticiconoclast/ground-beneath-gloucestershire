# Decisions

Short records: problem, what was measured, what was chosen, what the
acceptance test is. Add to the top. Abandoned approaches go here too, with
what they cost.

## 2026-09-07 — `ags_log_url` serves a PDF, and that turns out to be good news

**Problem.** The last unverified item in the gold chain: what
`ags_log_url` actually returns. `gbg.ags` was written expecting AGS3/AGS4
text, on an explicitly recorded assumption.

**Measured.** Fetched the deepest AGS record in the Gloucester/M5 J11
candidate tile (`BH03`, `loca_fdep` 13.0 m, project "Area 2 - Golden Valley
Bridge M5 J11"). The URL redirects to
`webservices.bgs.ac.uk/GWBV/viewborehole?loca_id=…` and returns
`application/pdf`, 13,668 bytes. `logs_from_ags` fails on it, as it must.
But `pymupdf` finds a full text layer — no raster, no OCR — carrying the
log's `Depth (m)` boundaries (0.50, 0.95, 4.85, 4.95), `Level (m)` in m AOD
(30.60, 30.15, 26.25, 26.15 — so ground level is 31.10 m AOD for this hole,
derivable per borehole), and complete stratum descriptions such as "Hard
Extremely weak fissured dark grey silty CLAY MUDSTONE" and "Strong grey
fossiliferous LIMESTONE with frequent … fossil fragments". Extracted text is
not in reading order, so columns must be aligned by coordinate.

**Chosen.** Nothing yet — this is recorded before any code changes because it
invalidates a documented assumption rather than confirming one. The
consequence to weigh: gold labels are obtainable from these PDFs by plain
text extraction, with no vision model, no Ollama and no API key, which is a
far shorter path to a real model than scan extraction. It is also *narrower*:
these are shallow site-investigation holes (13.0 m at the deepest in that
tile, 25.0 m county-wide) against SOBI scans reaching 131 m in the Stroud
tile. Depth still needs the scans.

**Acceptance test.** None yet; nothing was changed. When a parser exists, the
test is that it reproduces the four boundary depths and levels above from
this exact PDF, and that `gbg.lithology.normalise` maps its descriptions to
CLAY / MUDSTONE / LIMESTONE.

**Cost of anything abandoned.** The AGS-text assumption in `gbg.ags` and the
`gbg gold` command built on it are not wrong for AGS files a user supplies by
hand, but they cannot be fed from this API. Two requests to find out.

## 2026-09-07 — First live BGS run; the pilot tile has no usable gold

**Problem.** The viewer was showing the synthetic preview because the corpus
was empty, and nobody had checked whether the Stroud pilot tile can produce a
real model at all. The scaffold entry below records why: the sandbox that built
it could not reach BGS, so Stage 0 was never run. This environment can.

**Measured.** All against the live API on 2026-09-07, through `PoliteClient`
at the configured 0.5 req/s.

- `gbg probe`: pilot 177, stroud_valleys 737, gloucestershire 19,335. The bbox
  parameter is honoured. Whole-collection `numberMatched` is still 1,358,783,
  unchanged from 2026-09-06.
- `gbg enumerate pilot`: 176 boreholes stored inside the BNG rectangle, 1
  envelope-only record dropped, 2 requests, no `CompletenessError`. The dropped
  record is the corner over-selection case `tests/conftest.py` was built around,
  observed for the first time against real data.
- Depth in the tile: 106 of 176 carry a recorded length, 12 are the `-2.0`
  sentinel. Median 3.81 m, max 131.06 m; 36 are ≥ 5 m, 20 ≥ 10 m, 8 ≥ 30 m.
  Over 25 km² that is about 1.4 boreholes ≥ 5 m per km², against the Stage 1→2
  gate's ≥ 3 per km².
- **Gold in the tile: none usable.** `agsboreholeindex` returns 38 records for
  the pilot bbox. Every one belongs to the "Cotswold Canals" project;
  `loca_fdep` has median 1.03 m and max 4.0 m; and 0 of 38 carry an
  `ags_log_url` or a `dad_item_url`. There is nothing to download, and nothing
  deep enough to constrain bedrock that starts ~20 m down. The Stage 0 gate
  needs ≥ 20 gold boreholes, so it cannot be scored in this tile at all.
- County-wide the gold does exist: 1,426 AGS records in the county envelope,
  and 346 of a 500-record sample carry an `ags_log_url`. Depths are still
  shallow (median 2.0 m, max 25.0 m, 114 of 500 ≥ 5 m). Largest projects:
  M4/M5 Improvements (119), Cotswold Canals (72), M5 J13–14 (40).
- **A trap worth recording:** `ags_log_url` is populated on 19 of the 176 SOBI
  records in the tile while being null on all 38 records the *AGS* index
  returns for the same ground. The two collections disagree, and for this tile
  SOBI is the better source of AGS links. `gbg gold` currently expects the AGS
  index; that assumption is wrong here.
- No extraction provider in this environment: nothing on `localhost:11434`,
  `ANTHROPIC_API_KEY` unset. The scan-extraction route cannot run here.

**Chosen.** `client.user_agent` now identifies the client with the
repository's issue tracker as the contact, replacing the placeholder, so no
personal address is sent to BGS. Nothing else is chosen yet: the AOI question
this raises is recorded here rather than answered quietly, because moving the
AOI changes what every later stage is scored on.

**Acceptance test.** Two Stage 0 checkboxes tick in README: probe shows
different counts per AOI, and enumerate satisfied the completeness oracle.
Both observed 2026-09-07.

**Cost of anything abandoned.** Nothing abandoned. Establishing that the pilot
tile cannot clear the Stage 0 gate cost two requests, which is cheaper than
discovering it after building an extraction pipeline against it.

## 2026-09-07 — The viewer is actually live

**Problem.** The previous entry's acceptance test was unobserved: the workflow
existed only on a branch, so it had never run. GitHub also refuses a
`workflow_dispatch` for a workflow absent from the default branch (404), so
the deploy could not be started from the branch either.

**Measured.** With the Pages source set to GitHub Actions and the branch
fast-forwarded into `main`, run
[34095043122](https://github.com/nihilisticiconoclast/ground-beneath-gloucestershire/actions/runs/34095043122)
finished green: `test` in 53 s (49 tests, the model smoke run, the viewer
wiring check and the known-bad eval fixture), then `pages` in 18 s, its own
verify step included. Checked independently of the run afterwards:
`/` returns 200 with `<title>The Ground Beneath Gloucestershire</title>`;
`viewer.js` (11,119 B), `styles.css` (6,068 B) and `data/sample_voxels.json`
(376,491 B) all return 200; the served JSON is byte-identical to the committed
file and satisfies the viewer's contract (48,000 = nx·ny·nz voxels, `class_idx`
and `entropy` both 48,000 long, `surface` 1,600, 33,212 below ground, 38
boreholes); and `styles.css` line 91 carries `rotate(-90deg)`, so the deployed
CSS is the fixed slider and not a stale artefact.

**Chosen.** Nothing new. This entry exists only to close the previous one's
open acceptance test with the numbers.

**Acceptance test.** All of the above, observed 2026-09-07.

**Cost of anything abandoned.** Nothing abandoned.

**Not verified.** A browser completing the page's `unpkg.com` importmap fetch
end to end. This sandbox's Chromium cannot egress — every HTTPS connection it
opens is reset by the agent proxy (`net::ERR_CONNECTION_RESET`), including one
to the live site itself — so the render check was run against a local mirror of
the bytes Pages serves, with the two three.js files fetched by `curl` from the
exact URLs the live importmap names (both 200). Legend, both sliders and the
hover probe worked on that mirror. Opening the URL in a real browser is the
one-second check nobody in this sandbox can make.

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
