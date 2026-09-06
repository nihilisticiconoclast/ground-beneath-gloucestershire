"""Score extracted logs against gold logs, and decide the gate.

Two families of metric, because a log can be right about one and wrong about
the other:

  Boundaries — did we find the depths where the ground changes?
      recall    = gold boundaries with a predicted boundary within tolerance / gold boundaries
      precision = predicted boundaries with a gold boundary within tolerance / predicted boundaries

  Lithology — along the column, sampled every `step_m`, is the class right?
      depth_accuracy = fraction of sampled depths where classes agree
      lithology_f1   = macro-F1 over classes present in gold (depth-weighted)

Pooled across all gold boreholes (every metre counts once) and also reported
per borehole so a single bad scan can't hide in an average — or dominate it.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import Gates
from .schema import BoreholeLog

MISSING = "__MISSING__"


@dataclass
class BoreholeScore:
    bgs_id: int
    gold_boundaries: int
    pred_boundaries: int
    boundary_hits: int
    pred_hits: int
    samples: int
    correct: int
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def boundary_recall(self) -> float | None:
        return self.boundary_hits / self.gold_boundaries if self.gold_boundaries else None

    @property
    def boundary_precision(self) -> float | None:
        return self.pred_hits / self.pred_boundaries if self.pred_boundaries else None

    @property
    def depth_accuracy(self) -> float | None:
        return self.correct / self.samples if self.samples else None


def _match_count(a: list[float], b: list[float], tol: float) -> int:
    """How many points of `a` have some point of `b` within tol (each a counted once)."""
    hits = 0
    j = 0
    b = sorted(b)
    for x in sorted(a):
        while j < len(b) and b[j] < x - tol:
            j += 1
        if j < len(b) and abs(b[j] - x) <= tol:
            hits += 1
    return hits


def score_borehole(gold: BoreholeLog, pred: BoreholeLog | None, tol_m: float, step_m: float = 0.1) -> BoreholeScore:
    gb = gold.boundaries()
    pb = pred.boundaries() if pred else []
    conf: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    samples = correct = 0
    depth = 0.0
    base = gold.base_depth_m
    while depth < base - 1e-9:
        g = gold.class_at(depth)
        if g is not None:
            p = (pred.class_at(depth) if pred else None) or MISSING
            conf[g][p] += 1
            samples += 1
            correct += int(p == g)
        depth = round(depth + step_m, 6)
    return BoreholeScore(
        bgs_id=gold.bgs_id,
        gold_boundaries=len(gb),
        pred_boundaries=len(pb),
        boundary_hits=_match_count(gb, pb, tol_m),
        pred_hits=_match_count(pb, gb, tol_m),
        samples=samples,
        correct=correct,
        confusion={k: dict(v) for k, v in conf.items()},
    )


def macro_f1(confusion: dict[str, dict[str, int]]) -> float | None:
    """Macro-F1 over gold classes, from a gold->pred confusion of depth samples."""
    classes = set(confusion)
    if not classes:
        return None
    pred_totals: Counter[str] = Counter()
    for g, row in confusion.items():
        for p, n in row.items():
            pred_totals[p] += n
    f1s = []
    for c in classes:
        tp = confusion[c].get(c, 0)
        fn = sum(confusion[c].values()) - tp
        fp = pred_totals[c] - tp
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if (prec + rec) else 0.0)
    return sum(f1s) / len(f1s)


@dataclass
class EvalReport:
    source: str
    n_gold: int
    n_pred: int
    boundary_recall: float | None
    boundary_precision: float | None
    depth_accuracy: float | None
    lithology_f1: float | None
    confidence_bands: dict[str, int]
    gates: dict[str, bool]
    gates_passed: bool
    warnings: list[str]
    per_borehole: list[dict]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def to_markdown(self) -> str:
        def pct(v: float | None) -> str:
            return "n/a" if v is None else f"{100 * v:.1f}%"

        lines = [
            f"# Extraction eval — `{self.source}`",
            "",
            f"Gold boreholes: **{self.n_gold}** · predicted logs present: **{self.n_pred}**",
            "",
            "| metric | value |",
            "|---|---|",
            f"| boundary recall (gate) | {pct(self.boundary_recall)} |",
            f"| boundary precision | {pct(self.boundary_precision)} |",
            f"| depth-sampled accuracy | {pct(self.depth_accuracy)} |",
            f"| lithology macro-F1 (gate) | {pct(self.lithology_f1)} |",
            "",
            "Gates: " + ", ".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in self.gates.items()),
            "",
            f"**Verdict: {'PROCEED' if self.gates_passed else 'DO NOT PROCEED'}**",
            "",
            "Confidence bands of predicted intervals: "
            + ", ".join(f"{k}: {v}" for k, v in self.confidence_bands.items()),
        ]
        if self.warnings:
            lines += ["", "Warnings:"] + [f"- {w}" for w in self.warnings]
        lines += ["", "| bgs_id | bnd recall | bnd precision | depth acc | samples |", "|---|---|---|---|---|"]
        for r in self.per_borehole:
            lines.append(
                f"| {r['bgs_id']} | {pct(r['boundary_recall'])} | {pct(r['boundary_precision'])} | "
                f"{pct(r['depth_accuracy'])} | {r['samples']} |"
            )
        return "\n".join(lines) + "\n"


def confidence_bands(preds: dict[int, BoreholeLog]) -> dict[str, int]:
    bands = {"none": 0, "<0.5": 0, "0.5-0.8": 0, "0.8-0.95": 0, ">=0.95": 0}
    for log in preds.values():
        for iv in log.intervals:
            c = iv.confidence
            if c is None:
                bands["none"] += 1
            elif c < 0.5:
                bands["<0.5"] += 1
            elif c < 0.8:
                bands["0.5-0.8"] += 1
            elif c < 0.95:
                bands["0.8-0.95"] += 1
            else:
                bands[">=0.95"] += 1
    return bands


def evaluate(gold: dict[int, BoreholeLog], preds: dict[int, BoreholeLog], gates: Gates, source: str) -> EvalReport:
    scores = [score_borehole(g, preds.get(bid), gates.boundary_tolerance_m) for bid, g in sorted(gold.items())]
    gb = sum(s.gold_boundaries for s in scores)
    pb = sum(s.pred_boundaries for s in scores)
    samples = sum(s.samples for s in scores)
    pooled: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in scores:
        for g, row in s.confusion.items():
            for p, n in row.items():
                pooled[g][p] += n

    recall = sum(s.boundary_hits for s in scores) / gb if gb else None
    precision = sum(s.pred_hits for s in scores) / pb if pb else None
    acc = sum(s.correct for s in scores) / samples if samples else None
    f1 = macro_f1({k: dict(v) for k, v in pooled.items()})

    warnings: list[str] = []
    n_gold = len(gold)
    n_pred = sum(1 for bid in gold if bid in preds)
    if n_pred < n_gold:
        warnings.append(f"{n_gold - n_pred} gold boreholes have no predicted log (scored as all-missing)")
    bands = confidence_bands(preds)
    used = [k for k, v in bands.items() if v and k != "none"]
    if bands["none"] and bands["none"] == sum(bands.values()):
        warnings.append("extractor reported no confidences at all")
    elif len(used) <= 1 and sum(bands.values()) >= 10:
        warnings.append(f"confidence collapsed to a single band ({used}); the scale is not being used")

    gate_results = {
        "enough_gold": n_gold >= gates.min_gold_boreholes,
        "boundary_recall": recall is not None and recall >= gates.min_boundary_recall,
        "lithology_f1": f1 is not None and f1 >= gates.min_lithology_f1,
    }
    if not gate_results["enough_gold"]:
        warnings.append(
            f"only {n_gold} gold boreholes; gate needs {gates.min_gold_boreholes} to mean anything"
        )
    return EvalReport(
        source=source, n_gold=n_gold, n_pred=n_pred,
        boundary_recall=recall, boundary_precision=precision, depth_accuracy=acc, lithology_f1=f1,
        confidence_bands=bands, gates=gate_results, gates_passed=all(gate_results.values()),
        warnings=warnings,
        per_borehole=[
            {
                "bgs_id": s.bgs_id, "boundary_recall": s.boundary_recall,
                "boundary_precision": s.boundary_precision, "depth_accuracy": s.depth_accuracy,
                "samples": s.samples,
            }
            for s in scores
        ],
    )


def write_report(report: EvalReport, out_dir: Path, run_id: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"eval_{run_id}.md"
    js = out_dir / f"eval_{run_id}.json"
    md.write_text(report.to_markdown(), encoding="utf-8")
    js.write_text(report.to_json(), encoding="utf-8")
    return md, js
