# data/ (local only)

Nothing in this directory is committed except this file. The pipeline creates:

```
data/gbg.duckdb              the database (boreholes, scans, intervals, eval_runs)
data/raw/scans/{bgs_id}.pdf  scan PDFs as delivered by BGS — never modified, never re-fetched
data/derived/pages/          PNG page renders used for extraction
data/derived/evals/          eval_<run_id>.md / .json reports
data/gold/                   AGS files you download for gold labels, plus ids.csv (ags_hole_id,bgs_id)
```

Scans are BGS material under the Open Government Licence:
"Contains British Geological Survey materials © UKRI 2026".
