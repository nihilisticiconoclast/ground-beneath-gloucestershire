# The Ground Beneath Gloucestershire

A 3D lithology model of the ground beneath the Stroud valleys, the Cotswold
escarpment and the Severn Vale, reconstructed from tens of thousands of
British Geological Survey borehole logs that today exist only as scanned PDFs —
with the model's uncertainty shown in the viewer rather than hidden in a
report.

The idea is the same as a connectome built from serial-section images: a very
large number of individual 2D records (each borehole log is a one-dimensional
column of "what the ground was, at what depth"), registered in space and
assembled into one whole that nobody has seen, because nobody could read
enough of them.

## What the pipeline does

```
SOBI index ──enumerate──▶ boreholes ──fetch-scans──▶ scan PDFs ──extract──▶ lithology intervals
                                                                                   │
AGS gold logs ──gold──▶ intervals(gold:ags) ◀────────── eval ──────────────────────┘
                                                            │
                                            gates pass? ──▶ Stage 2: 3D model with uncertainty
                                                                            │
                                                                       web/ viewer (GitHub Pages)
```

| stage | command | what it proves |
|---|---|---|
| probe | `gbg probe` | the API answers and the `bbox` parameter changes the answer |
| enumerate | `gbg enumerate pilot` | every borehole in the tile is indexed; fetched count == API's `numberMatched` |
| fetch | `gbg fetch-scans pilot` | PDFs cached; HTTP 200 that isn't a PDF is recorded as a failure, not a scan |
| gold | `gbg gold data/gold/*.ags --id-map data/gold/ids.csv` | measured logs to score against |
| extract | `gbg extract pilot --provider ollama` | model output validated against the data contract |
| eval | `gbg eval` | boundary recall and lithology F1 vs gold; exit 1 if the gate fails |
| model | `gbg model pilot --tune` | leave-one-borehole-out accuracy beats the nearest-borehole baseline; calibrated; bakes `web/data/model.json` |
| serve | `gbg-mcp` | an MCP server so an agent can ask the corpus and the model questions |

`web/` renders `web/data/sample_voxels.json` (a synthetic preview, flagged on
screen) by default and `web/data/model.json` when the model stage has baked
one: open `index.html?model=data/model.json`. A model baked with failing
gates or in the depth frame is flagged in the same place.

## Seeing it on GitHub Pages

The viewer is published at
<https://nihilisticiconoclast.github.io/ground-beneath-gloucestershire/>.
The `pages` job in `.github/workflows/ci.yml` runs on every push to `main`
(and on a manual run from the Actions tab): once the tests pass it regenerates
the synthetic preview, uploads `web/` as the site, and then fetches the live
URL to check that what is served is the viewer. Until a real model is committed
as `web/data/model.json`, the page shows the synthetic preview and says so on
screen.

One-time setup: in the repository's **Settings → Pages**, set *Source* to
**GitHub Actions**. With the older "Deploy from a branch" source, Pages
publishes a Jekyll rendering of this README instead of the viewer.

## The numbers that decide whether to proceed

From `config/gbg.toml`, scored by `gbg eval` against AGS boreholes (see
[docs/GATES.md](docs/GATES.md)):

- boundary recall ≥ **0.85** at ±0.25 m
- lithology macro-F1 ≥ **0.80**
- on at least **20** gold boreholes

If extraction can't clear these after two or three prompt/model iterations,
the answer is a different approach (or the AGS-only subset), not a fourth
iteration. Write down what was tried and what it scored.

For the model (Stage 2), scored leave-one-borehole-out:

- accuracy beats the nearest-borehole baseline by ≥ **5 pp**
- among voxels called ≥ 80% certain, ≥ **75%** are right
- on at least **30** boreholes with positions

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -q                                     # 49 tests, no network

# 1. Put a real contact address in config/gbg.toml (client.user_agent).
# 2. Sanity-check the API and the bbox parameter:
gbg probe
# 3. Index the Stroud pilot tile (5 x 5 km):
gbg enumerate pilot
gbg stats
# 4. Fetch a first batch of scans (capped at 50 per run by config):
gbg fetch-scans pilot
# 5. Look at a few PDFs by eye before spending any model time on them.
```

Local extraction needs [Ollama](https://ollama.com) and a vision model
(`ollama pull qwen2.5vl:7b`, or whichever you set in `extraction.ollama_model`).
Then `gbg extract pilot --provider ollama` and `gbg eval`.

### The model and the MCP server

```bash
gbg model pilot --tune                  # LOO validation, gates, bakes web/data/model.json
gbg model pilot --source "extract:ollama/qwen2.5vl:7b"   # model the extracted logs instead of gold
python -m http.server -d web 8000       # then open http://localhost:8000/?model=data/model.json

pip install -e ".[mcp]"
claude mcp add --transport stdio --scope project gbg -- gbg-mcp   # or use the committed .mcp.json
```

The MCP server (`src/gbg/mcp_server.py`) exposes `pipeline_status`,
`count_boreholes`, `nearest_boreholes`, `borehole_log` and `subsurface_at`.
It reads only the local DuckDB and the baked model; it never calls BGS, so an
agent using it cannot become a bulk downloader by accident.

The model itself (`src/gbg/model.py`) is a kernel-weighted class vote with a
Dirichlet prior and strong horizontal/vertical anisotropy — simple enough to
explain in a sentence, honest enough to say "unconstrained" where no borehole
is near. Its interface (fit / predict_proba / loo_validate / bake) is what a
PyMC or NumPyro indicator-kriging model would slot into later.

## Stage 0 checklist — things verified, things not

Verified against the live service on 2026-09-06 (details and captured
responses in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)):

- [x] SOBI OGC API items endpoint, field names, `numberMatched` / `next` paging
- [x] `bgs_id` and `id` arrive as floats; `length = -2.0` is a "not recorded" sentinel
- [x] `limit` accepts up to 1,000,000; `offset` paging; bbox is lon/lat (CRS84)
- [x] the scans endpoint pattern `api.bgs.ac.uk/sobi-scans/v1/borehole/scans/items/{id}`
- [x] an `agsboreholeindex` collection exists on the same API (gold-label candidates)

Not yet verified — do these on the first live run and tick them off:

- [x] `gbg probe` shows different counts for pilot / valleys / county (bbox honoured) —
      177 / 737 / 19,335 on 2026-09-07
- [x] `gbg enumerate pilot` finishes without `CompletenessError` — 176 stored, 1
      envelope-only record dropped, 2 requests, on 2026-09-07
- [ ] what the scans API returns for a record with no scan (404? 200+HTML?) — `gbg fetch-scans` records it either way
- [ ] whether `length_scan_cat` ending `_Y` means "scan available" (compare against fetch outcomes)
- [ ] what `ags_log_url` on the AGS index actually serves (an .ags file? a zip? a page?) — `gbg gold` expects AGS text.
      Partly answered 2026-09-07: it is *null* on all 38 AGS records in the pilot
      tile, and populated on 346 of a 500-record county sample, so there is
      nothing to fetch in the pilot tile. The payload format is still unverified.
- [ ] the BGS scanned-record licence line to use in the viewer credit (currently "Contains British Geological Survey materials © UKRI 2026")
- [ ] email enquiries@bgs.ac.uk describing the project and asking whether a county-scale bulk cut is available, before scaling past the pilot

## Data and licences

Borehole index and scans are BGS data released under the Open Government
Licence; the API terms forbid bulk download of the *entire* dataset and
reserve the right to rate-limit or block clients that create excessive load.
This project scopes to one county, throttles to one request every two seconds,
caches everything, and caps scans per run. See docs/DATA_SOURCES.md.

Code is MIT. The model outputs will be published under OGL-compatible terms
with BGS attribution.

## Layout

```
config/gbg.toml         areas of interest, rate limits, gates — edit this, not the code
src/gbg/                pipeline package (see module docstrings); model.py is Stage 2, mcp_server.py the MCP server
.mcp.json               project-scope MCP registration for Claude Code
tests/                  fixtures reproduce the live API's shape and its traps; no network
scripts/                make_sample_voxels.py — synthetic preview for the viewer
web/                    GitHub Pages viewer (three.js), loads web/data/*.json
docs/                   DATA_SOURCES, GATES, DECISIONS
data/                   local only (git-ignored except README): db, raw scans, gold, evals
```
