# Data sources

Everything below was checked against the live service on **2026-09-06** unless
marked *unverified*. When something here disagrees with the API, the API wins;
update this file with the date you found out.

## 1. BGS Single Onshore Borehole Index (SOBI) — the index

- Service: BGS OGC API Service BETA (pygeoapi-style), `https://ogcapi.bgs.ac.uk`
- Collection: `https://ogcapi.bgs.ac.uk/collections/onshoreboreholeindex`
- Items: `https://ogcapi.bgs.ac.uk/collections/onshoreboreholeindex/items?f=json`
- Queryables: `.../onshoreboreholeindex/queryables?f=json`
- OpenAPI: `https://ogcapi.bgs.ac.uk/openapi`
- Licence: Open Government Licence v3 (collection metadata says OGL; the API
  landing page says "mostly OGL, please see individual collections").
- Attribution: "Contains British Geological Survey materials © UKRI [year]".

Observed on 2026-09-06: `numberMatched` = **1,358,783** for the whole
collection; default page size 10; `limit` maximum 1,000,000; `offset` paging
with a `links[rel=next]` entry; storage CRS is CRS84 so `bbox` is
`min_lon,min_lat,max_lon,max_lat`. A `bbox-crs` parameter exists in the
OpenAPI document but the collection lists only CRS84, so this project converts
BNG → WGS84 client-side (`gbg.bng`).

Properties on every feature (all verified):

| field | type | notes |
|---|---|---|
| `id`, `bgs_id` | number (float in JSON!) | the same value; cast to int |
| `reference` | string | BGS reference e.g. `SO80SW12`, `SD30NE6/A` |
| `name` | string | free text; "NOTIFICATION ONLY" marks index-only records |
| `grid_ref` | string | OS grid ref |
| `easting`, `northing` | number | BNG metres — use these, not the geometry |
| `precision` | string | e.g. `± 10 METRES`, `± METRE` |
| `length` | number | metres; **-2.0 means not recorded** |
| `length_scan_cat` | string | e.g. `3_Y`, `-2_Y` — *meaning of the suffix unverified* |
| `year_known` | string or null | |
| `sitereport` | string or null | |
| `held_at` | string | e.g. `KW`, `WLKW` (Keyworth / Wallingford, presumably) |
| `date_updated` | ISO datetime | |
| `scan_url` | string | see §2 |
| `ags_log_url` | string or null | see §3 |
| `water_well_ref` | string | `N/A` when none |
| `scan_quality` | string | mostly `not Entered` |

Sample record (verbatim, id 1):

```json
{"reference":"SD20NE1","name":"FORMBY 4","grid_ref":"SD 28213 07484","easting":328213.0,
 "northing":407484.0,"precision":"± METRE","length":1182.62,"year_known":"1950",
 "sitereport":null,"held_at":"WLKW","id":1.0,"length_scan_cat":"3_Y","bgs_id":1.0,
 "date_updated":"2025-03-10T14:27:11",
 "scan_url":"https://api.bgs.ac.uk/sobi-scans/v1/borehole/scans/items/1",
 "ags_log_url":null,"water_well_ref":"SD20/3","scan_quality":"not Entered"}
```

## 2. BGS borehole scans — the primary material

- Endpoint pattern (from `scan_url`): `https://api.bgs.ac.uk/sobi-scans/v1/borehole/scans/items/{bgs_id}`
- Delivers multi-page PDFs (BGS, 2023: "generating multi-page PDFs … with no limitation on size").
- BGS states over one million borehole log scans are free under OGL.
- *Unverified*: response for an id with no scan; content-type header; whether
  `Accept: application/pdf` is needed. `gbg.scans` checks the body starts
  with `%PDF-` regardless of headers and records every outcome.

## 3. AGS borehole index — the gold labels

- Collection: `https://ogcapi.bgs.ac.uk/collections/agsboreholeindex`
- Properties (from the OpenAPI document): `bgs_loca_id`, `id`, `proj_name`,
  `proj_cont`, `proj_eng`, `x`, `y`, `loca_fdep`, `rec_id`, `loca_id`,
  `item_index_id`, `ags_log_url`, `dad_item_url`.
- BGS describes it as third-party site-investigation data "delivered as
  received"; no BGS interpretation added.
- *Unverified*: the format served at `ags_log_url` (AGS3/AGS4 text, zip, or
  HTML). `gbg.ags` parses AGS4 `GROUP GEOL` and AGS3 `**GEOL`.
- Also available: the AGS File Utilities API `https://agsapi.bgs.ac.uk/`
  (validation/conversion; spatial export reportedly capped at 50 boreholes).

Why AGS is gold: interval depths in AGS were *measured and typed by the
contractor*, so boundaries are trustworthy. Class labels come from
`gbg.lithology.normalise` over `GEOL_DESC`, so a class is only as good as
that mapping — its confidence is stored alongside.

## 4. Other BGS collections on the same API (for Stage 2 priors / validation)

- `bgsgeology625kbedrock` — 1:625k bedrock polygons (verified to exist; OGL).
- BGS UK3D / GB3D fence diagrams — hand-built national cross-sections,
  downloadable separately; the independent check for the interpolated model.
- Terrain skin: Environment Agency National LiDAR Programme DTM (OGL), not yet wired in.

## 5. Fair use

From the BGS API page: "If an end user creates an excessive load on the API,
to the detriment of the service for others, BGS reserves the right to block
access from identified IP addresses. The APIs are not intended to be used for
bulk download of the entire dataset."

How this project stays inside that: county-scale scope, one request every two
seconds (`client.requests_per_second = 0.5`), a hard cap on scans per run,
permanent local caching so nothing is fetched twice, an identifying
User-Agent with a contact address, and an email to enquiries@bgs.ac.uk before
scaling past the pilot.
