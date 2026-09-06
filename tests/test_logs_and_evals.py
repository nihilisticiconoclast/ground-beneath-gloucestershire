from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gbg.ags import GOLD_SOURCE, logs_from_ags, parse_geol
from gbg.evals import evaluate, macro_f1, score_borehole
from gbg.extract import MockExtractor, parse_model_json, to_log
from gbg.lithology import LITH_CLASSES, normalise
from gbg.schema import BoreholeLog, LithInterval

from conftest import FIXTURES


# ------------------------------------------------------------------ lithology


@pytest.mark.parametrize(
    "text, expected, min_conf",
    [
        ("Firm brown mottled grey silty CLAY", "CLAY", 0.9),
        ("Loose brown fine SAND", "SAND", 0.9),
        ("Pale yellow-brown oolitic LIMESTONE", "LIMESTONE", 0.9),
        ("Weak thinly bedded blue-grey MUDSTONE (Lias)", "MUDSTONE", 0.9),
        ("MADE GROUND: brick rubble and ash", "MADE_GROUND", 0.9),
        ("Dark brown sandy TOPSOIL", "TOPSOIL", 0.9),
        ("Reddish brown MARL (Mercia Mudstone)", "MARL", 0.9),
        ("brown sandy clay", "CLAY", 0.5),            # lower case: keyword scoring
        ("oolite, shelly, well cemented", "LIMESTONE", 0.7),
        ("no recovery", "NO_RECOVERY", 0.7),
        ("", "UNKNOWN", 0.0),
        ("?? illegible ??", "UNKNOWN", 0.0),
    ],
)
def test_normalise(text, expected, min_conf):
    n = normalise(text)
    assert n.lith_class == expected
    assert n.confidence >= min_conf
    assert n.lith_class in LITH_CLASSES


def test_gravel_modifier_does_not_override_capitalised_principal():
    assert normalise("Grey slightly sandy gravelly CLAY with occasional limestone fragments").lith_class == "CLAY"


# ------------------------------------------------------------------ schema


def test_interval_rejects_upside_down_depths_and_unknown_classes():
    with pytest.raises(ValidationError):
        LithInterval(top_m=2.0, base_m=1.0, lith_class="CLAY")
    with pytest.raises(ValidationError):
        LithInterval(top_m=0.0, base_m=1.0, lith_class="OOLITE")


def test_log_sorts_intervals_and_rejects_overlaps():
    log = BoreholeLog(
        bgs_id=1,
        source="test",
        intervals=[
            LithInterval(top_m=1.0, base_m=2.0, lith_class="SAND"),
            LithInterval(top_m=0.0, base_m=1.0, lith_class="TOPSOIL"),
        ],
    )
    assert [i.lith_class for i in log.intervals] == ["TOPSOIL", "SAND"]
    assert log.boundaries() == [0.0, 1.0, 2.0]
    assert log.class_at(0.5) == "TOPSOIL" and log.class_at(1.0) == "SAND" and log.class_at(2.0) is None
    with pytest.raises(ValidationError, match="overlap"):
        BoreholeLog(
            bgs_id=1, source="test",
            intervals=[LithInterval(top_m=0, base_m=2, lith_class="CLAY"),
                       LithInterval(top_m=1, base_m=3, lith_class="SAND")],
        )


def test_plausibility_warnings_flag_index_mismatch_and_gaps():
    log = BoreholeLog(
        bgs_id=1, source="test",
        intervals=[LithInterval(top_m=0, base_m=2, lith_class="CLAY"),
                   LithInterval(top_m=3, base_m=40, lith_class="LIMESTONE")],
    )
    w = log.plausibility_warnings(index_length_m=12.0)
    assert any("exceeds index length" in s for s in w)
    assert any("unlogged gaps" in s for s in w)


# ------------------------------------------------------------------ ags


def test_parse_ags4_and_ags3_geol():
    ags4 = (FIXTURES / "sample_ags4.ags").read_text()
    groups = parse_geol(ags4)
    assert set(groups) == {"BH01", "BH02"}
    assert len(groups["BH01"]) == 4
    assert groups["BH01"][0]["GEOL_DESC"] == "Dark brown sandy TOPSOIL"

    ags3 = (FIXTURES / "sample_ags3.ags").read_text()
    groups3 = parse_geol(ags3)
    assert set(groups3) == {"TP1"} and len(groups3["TP1"]) == 3


def test_logs_from_ags_map_ids_and_carry_normaliser_confidence():
    ags4 = (FIXTURES / "sample_ags4.ags").read_text()
    logs = logs_from_ags(ags4, {"BH01": 1001})
    by_id = {log.bgs_id: log for log in logs}
    assert 1001 in by_id and by_id[1001].source == GOLD_SOURCE
    assert by_id[1001].base_depth_m == 12.5
    assert [i.lith_class for i in by_id[1001].intervals] == ["TOPSOIL", "CLAY", "CLAY", "MUDSTONE"]
    unmapped = [log for log in logs if log.bgs_id < 0]
    assert len(unmapped) == 1 and "BH02" in unmapped[0].notes


# ------------------------------------------------------------------ extraction parsing


def test_parse_model_json_tolerates_fences_and_preamble():
    text = 'Sure, here is the log:\n```json\n{"intervals": [], "notes": "cover sheet"}\n```\nDone.'
    assert parse_model_json(text)["notes"] == "cover sheet"
    with pytest.raises(ValueError):
        parse_model_json("no json here")


def test_to_log_validates_provider_output():
    bad = {"intervals": [{"top_m": 5, "base_m": 1, "lith_class": "CLAY"}]}
    with pytest.raises(ValidationError):
        to_log(bad, 1, "extract:test")


def test_mock_extractor_replays_fixture(tmp_path):
    ex = MockExtractor(fixture_dir=FIXTURES / "extractions")
    log = ex.extract(1001, [])
    assert log.bgs_id == 1001 and len(log.intervals) == 4 and log.total_depth_m == 12.5


# ------------------------------------------------------------------ evals


def _gold_and_preds():
    ags4 = (FIXTURES / "sample_ags4.ags").read_text()
    gold = {log.bgs_id: log for log in logs_from_ags(ags4, {"BH01": 1001, "BH02": 1002})}
    ex = MockExtractor(fixture_dir=FIXTURES / "extractions")
    preds = {bid: ex.extract(bid, []) for bid in gold}
    return gold, preds


def test_score_borehole_boundary_tolerance_and_depth_accuracy():
    gold, preds = _gold_and_preds()
    s = score_borehole(gold[1001], preds[1001], tol_m=0.25)
    # Gold boundaries 0, 0.4, 3.2, 7.8, 12.5; predicted 0, 0.4, 3.3, 7.8, 12.5 -> all within 0.25 m.
    assert s.gold_boundaries == 5 and s.boundary_hits == 5 and s.pred_hits == 5
    # Classes agree everywhere except the 3.2–3.3 m sliver (both CLAY, so even that agrees).
    assert s.depth_accuracy == pytest.approx(1.0)

    tight = score_borehole(gold[1001], preds[1001], tol_m=0.05)
    assert tight.boundary_hits == 4  # 3.2 vs 3.3 now misses


def test_missing_prediction_scores_as_all_wrong(gates):
    gold, _ = _gold_and_preds()
    s = score_borehole(gold[1001], None, tol_m=0.25)
    assert s.boundary_hits == 0 and s.correct == 0 and s.samples > 0


def test_macro_f1_handles_missing_and_unknown():
    conf = {"CLAY": {"CLAY": 8, "__MISSING__": 2}, "SAND": {"SAND": 5}}
    assert macro_f1(conf) == pytest.approx((2 * 1.0 * 0.8 / 1.8 + 1.0) / 2)
    assert macro_f1({}) is None


def test_evaluate_reports_gates_and_warnings(gates):
    gold, preds = _gold_and_preds()
    report = evaluate(gold, preds, gates, source="extract:mock/fixture")
    assert report.n_gold == 2 and report.n_pred == 2
    # 1001 is perfect (5/5). 1002 is the poor read: gold 0, 0.3, 2.1, 6.0 vs pred 0, 1.0, 6.0
    # recovers only the ends (2/4). Pooled: 7/9.
    assert report.boundary_recall == pytest.approx(7 / 9)
    assert report.gates["enough_gold"] is True
    assert report.gates["boundary_recall"] is False
    assert report.gates_passed is False
    assert "DO NOT PROCEED" in report.to_markdown()
    assert report.confidence_bands["<0.5"] == 2 and report.confidence_bands[">=0.95"] == 1
    assert json.loads(report.to_json())["n_gold"] == 2


def test_evaluate_passes_when_only_the_good_borehole_is_in_gold(gates):
    gold, preds = _gold_and_preds()
    gold = {1001: gold[1001]}
    gates = gates.__class__(boundary_tolerance_m=0.25, min_boundary_recall=0.85,
                            min_lithology_f1=0.80, min_gold_boreholes=1)
    report = evaluate(gold, {1001: preds[1001]}, gates, source="extract:mock/fixture")
    assert report.gates_passed is True
    assert report.lithology_f1 == pytest.approx(1.0)
