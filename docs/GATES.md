# Gates

Each stage has a number that decides whether the next stage starts. The
numbers live in `config/gbg.toml`; the definitions live here; the verdict is
printed by `gbg eval` and stored in the `eval_runs` table.

## Stage 0 → 1: can we read the scans?

Scored on AGS boreholes inside the pilot tile, which provide measured
interval depths and descriptions independent of any model.

| metric | definition | gate |
|---|---|---|
| boundary recall | gold boundaries with a predicted boundary within ±0.25 m ÷ gold boundaries | ≥ 0.85 |
| boundary precision | predicted boundaries with a gold boundary within ±0.25 m ÷ predicted boundaries | reported |
| depth-sampled accuracy | every 0.1 m down each gold column, is the predicted class equal to the gold class | reported |
| lithology macro-F1 | macro-F1 over the classes present in gold, from the depth samples | ≥ 0.80 |
| gold size | number of gold boreholes scored | ≥ 20 |

Pooled over all gold boreholes (every metre counts once) *and* listed per
borehole. A gold borehole with no prediction scores zero everywhere rather than
being skipped, so the harness cannot pass by silently dropping hard scans.

Also reported, not gated: the distribution of the extractor's self-reported
confidence. A run that emits only one band ("everything 0.9") is flagged —
an extractor that will not use the low end of its scale has hidden its errors
where nobody will review them.

**Attempt budget:** three iterations of prompt/model/preprocessing. If the
gate is still failing, stop tuning and choose between (a) the AGS-only subset,
(b) a different model class, (c) Candidate 2 from the project research
(electricity network entity resolution). Record each attempt's numbers in
DECISIONS.md, including the abandoned ones.

## Stage 1 → 2: enough constrained ground?

- ≥ 3,000 boreholes with QA'd intervals across the target area, and
- every 1 km² cell in the area of interest has ≥ 3 boreholes with depth ≥ 5 m,
  or is explicitly marked "unconstrained" in the model output.

## Stage 2 → 3: is the model better than the trivial one?

Scored by `gbg model`, leave-one-*borehole*-out (a borehole's own column is
never evidence for itself), with config keys in brackets:

- accuracy of the interpolated model beats the nearest-borehole baseline by
  ≥ 5 percentage points [`min_model_gain_pp`]. A point the model calls
  *unconstrained* counts as wrong, so this gain is conservative.
- calibration: among held-out points the model calls ≥ 80% certain, ≥ 75%
  are right [`min_calibration_at_80`].
- at least 30 boreholes with positions [`min_model_boreholes`].

Baseline definition: the class at the same elevation (within 3 m) in the
horizontally nearest *other* borehole — what a geologist does by eye.

Measured on the synthetic flat-layer fixture (tests/test_model.py, 40
boreholes, 2026-09-06): accuracy 97.7%, baseline 84.7%, gain +13.0 pp,
calibration 100% at ≥ 80%, with `length_h = 600 m`. Real ground will be
worse; that is the point of measuring it.

## What "pass" does not mean

A gate passing means the next stage may start, not that the work is finished.
Every gate compares against an oracle — AGS gold, held-out boreholes, an
independent count — because nothing is true because a process finished.
