"""Stage 2 model tests.

A synthetic world with known stratigraphy lets the mechanics be checked
exactly: on flat layers the model must be near-perfect away from boundaries,
unconstrained far from any borehole, and the baked JSON must match the
viewer's contract (the same shape scripts/make_sample_voxels.py writes).
"""

from __future__ import annotations

import json
import random

import numpy as np
import pytest

from gbg.model import (AIR, MODEL_CLASSES, UNCONSTRAINED, BoreholeSite, KernelLithologyModel,
                       discretise, estimate_ground_levels, kernel_surface, write_model_json)
from gbg.schema import BoreholeLog, LithInterval

BBOX = (382500.0, 202500.0, 387500.0, 207500.0)


def flat_world_class(z: float) -> str:
    """Flat layers in elevation: limestone above 85 m AOD, marl 80–85, mudstone below."""
    if z > 85:
        return "LIMESTONE"
    if z > 80:
        return "MARL"
    return "MUDSTONE"


def make_log(bgs_id: int, e: float, n: float, gl: float, depth: float, step: float = 1.0) -> BoreholeLog:
    """A log of the flat world at (e, n), with intervals merged by class."""
    intervals = []
    d = 0.0
    cur, start = None, 0.0
    while d < depth:
        c = flat_world_class(gl - d - step / 2)
        if c != cur:
            if cur is not None:
                intervals.append(LithInterval(top_m=start, base_m=d, lith_class=cur))
            cur, start = c, d
        d += step
    intervals.append(LithInterval(top_m=start, base_m=depth, lith_class=cur))
    return BoreholeLog(bgs_id=bgs_id, intervals=intervals, source="test", ground_level_m=gl,
                       easting=e, northing=n)


def flat_world(n_boreholes: int = 40, seed: int = 3):
    rng = random.Random(seed)
    logs, sites = {}, {}
    for i in range(n_boreholes):
        e = rng.uniform(BBOX[0] + 200, BBOX[2] - 200)
        n = rng.uniform(BBOX[1] + 200, BBOX[3] - 200)
        gl = 100.0 - 0.002 * (e - BBOX[0])  # ground falls 10 m across the tile
        depth = rng.uniform(15, 40)
        logs[1000 + i] = make_log(1000 + i, e, n, gl, depth)
        sites[1000 + i] = BoreholeSite(1000 + i, e, n, gl)
    return logs, sites


def test_discretise_elevation_and_depth_frames():
    logs, sites = flat_world(5)
    s = discretise(logs.values(), sites, step_m=0.5)
    assert s.frame == "elevation" and len(s) > 0
    assert set(np.unique(s.bgs_id)) == set(logs)
    # Points sit between ground level and ground level minus depth.
    for bid, log in logs.items():
        z = s.xyz[s.bgs_id == bid, 2]
        gl = sites[bid].ground_level_m
        assert z.max() <= gl and z.min() >= gl - log.base_depth_m
    # Depth frame when no site has a ground level.
    sites_nogl = {k: BoreholeSite(k, v.easting, v.northing, None) for k, v in sites.items()}
    d = discretise(logs.values(), sites_nogl, step_m=0.5)
    assert d.frame == "depth" and d.xyz[:, 2].max() <= 0
    # Mixed frames are refused rather than silently merged.
    mixed = dict(sites); mixed[1000] = sites_nogl[1000]
    with pytest.raises(ValueError, match="mixed frames"):
        discretise(logs.values(), mixed)


def test_estimate_ground_levels_fills_from_neighbours_and_flags():
    _, sites = flat_world(30)
    target = 1000
    sites[target] = BoreholeSite(target, sites[target].easting, sites[target].northing, None)
    est = estimate_ground_levels(sites, length_h=600.0)
    assert est[target].gl_estimated is True
    truth = 100.0 - 0.002 * (sites[target].easting - BBOX[0])
    assert abs(est[target].ground_level_m - truth) < 3.0  # within the tile's total relief / 3
    lonely = BoreholeSite(1, BBOX[0] - 50_000, BBOX[1] - 50_000, None)
    assert estimate_ground_levels({1: lonely, **sites})[1].ground_level_m is None


def test_loo_on_flat_world_is_accurate_and_calibrated():
    logs, sites = flat_world(40)
    samples = discretise(logs.values(), sites, step_m=0.5)
    km = KernelLithologyModel(length_h=600.0, length_v=2.5)
    res = km.loo_validate(samples)
    assert res["n_boreholes"] == 40
    # Measured 2026-09-06 on this fixture: acc 0.977, baseline 0.847 (the baseline misses
    # points deeper than its nearest neighbour reaches), calibration 1.0 at >= 0.8.
    assert res["accuracy"] > 0.95
    assert 0.75 < res["baseline_accuracy"] < res["accuracy"]
    assert res["gain_pp"] > 5.0
    assert res["calibration_at_threshold"] > 0.9
    assert 0 < res["confident_fraction"] <= 1
    assert res["constrained_fraction"] > 0.95
    assert len(res["per_borehole"]) == 40 and all(0 <= r["accuracy"] <= 1 for r in res["per_borehole"])


def test_far_from_any_borehole_is_unconstrained_and_flat():
    logs, sites = flat_world(20)
    km = KernelLithologyModel(length_h=300.0, length_v=2.5).fit(discretise(logs.values(), sites))
    far = np.array([[BBOX[0] - 20_000, BBOX[1] - 20_000, 90.0]])
    proba, evidence = km.predict_proba(far)
    assert evidence[0] < km.min_evidence
    assert np.allclose(proba[0], 1 / len(MODEL_CLASSES))
    assert km.entropy(proba)[0] == pytest.approx(1.0)


def test_exclusion_really_removes_the_borehole_itself():
    logs, sites = flat_world(3, seed=9)
    samples = discretise(logs.values(), sites)
    km = KernelLithologyModel(length_h=300.0, length_v=2.5).fit(samples)
    bid = 1000
    own = samples.xyz[samples.bgs_id == bid][:1]
    _, with_self = km.predict_proba(own)
    _, without_self = km.predict_proba(own, exclude_bgs_id=bid)
    assert with_self[0] > without_self[0] + 0.5  # its own column contributed ≥ 0.5 of weight


def test_bake_matches_viewer_contract(tmp_path):
    logs, sites = flat_world(40)
    samples = discretise(logs.values(), sites)
    km = KernelLithologyModel(length_h=600.0, length_v=2.5).fit(samples)
    payload = km.bake(BBOX, cell_xy=250.0, cell_z=5.0, z0=55.0, z1=105.0,
                      surface_fn=kernel_surface(sites, length_h=600.0), sites=sites, name="test")
    n = payload["nx"] * payload["ny"] * payload["nz"]
    assert (payload["nx"], payload["ny"], payload["nz"]) == (20, 20, 10)
    assert len(payload["class_idx"]) == n == len(payload["entropy"])
    assert len(payload["surface"]) == payload["nx"] * payload["ny"]
    assert payload["synthetic"] is False and payload["frame"] == "elevation"
    assert payload["classes"] == list(MODEL_CLASSES)
    idx = np.array(payload["class_idx"])
    assert (idx == AIR).any() and (idx != AIR).any()
    assert payload["below_ground_voxels"] == int((idx != AIR).sum())
    assert 0 < payload["constrained_voxels"] <= payload["below_ground_voxels"]
    # Every constrained below-ground voxel above 85 m must be limestone (flat world).
    nx, ny = payload["nx"], payload["ny"]
    ent = np.array(payload["entropy"])
    for k in range(payload["nz"]):
        zc = payload["z0"] + (k + 0.5) * payload["cell_z"]
        sl = slice(k * nx * ny, (k + 1) * nx * ny)
        block, e = idx[sl], ent[sl]
        sure = (block != AIR) & (e < 0.5)
        if zc > 87 and sure.any():
            assert (block[sure] == MODEL_CLASSES.index("LIMESTONE")).mean() > 0.95
    path = write_model_json(payload, tmp_path / "model.json")
    assert json.loads(path.read_text())["nx"] == 20


def test_unconstrained_voxels_are_never_given_a_class():
    """A voxel nothing constrains must not inherit whichever class sits at index 0.

    `argmax` of a flat posterior returns 0, so before this was fixed the first
    real bake published 13,894 voxels as confident TOPSOIL, not one of which was
    constrained. Baking well below the boreholes reproduces that situation.
    """
    logs, sites = flat_world(40)
    samples = discretise(logs.values(), sites)
    km = KernelLithologyModel(length_h=600.0, length_v=2.5).fit(samples)
    # The flat world's holes bottom out around 60 m AOD; go 40 m deeper.
    payload = km.bake(BBOX, cell_xy=250.0, cell_z=5.0, z0=20.0, z1=105.0,
                      surface_fn=kernel_surface(sites, length_h=600.0), sites=sites, name="test")
    idx = np.array(payload["class_idx"])
    ent = np.array(payload["entropy"])
    below = idx != AIR
    unconstrained = idx == UNCONSTRAINED
    assert unconstrained.any(), "expected some voxels out of reach of every borehole"

    # The sentinel, not entropy, is what says "no evidence here". Entropy alone
    # cannot: a voxel with just enough evidence to be constrained can still have
    # a near-flat posterior that rounds to 1.0, so the two sets are not equal.
    assert (ent[unconstrained] == 1.0).all()
    # Everything else below ground carries a real class index.
    assert (idx[below & ~unconstrained] < len(MODEL_CLASSES)).all()
    # "Constrained" counts evidence, and matches the sentinel exactly.
    assert payload["constrained_voxels"] == int((below & ~unconstrained).sum())
