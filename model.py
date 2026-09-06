"""Stage 2 — a probabilistic 3D lithology model with honest uncertainty.

Version 0 is deliberately simple and fully explainable: every borehole log is
discretised into labelled points in (easting, northing, elevation); a voxel's
class distribution is a kernel-weighted vote of nearby points, smoothed by a
Dirichlet prior so that a voxel with no evidence returns a flat distribution
rather than a confident guess.

    p(class c | voxel) = (alpha / K + sum_{points i of class c} w_i)
                         / (alpha + sum_i w_i)

    w_i = exp(-( (dx/l_h)^2 + (dy/l_h)^2 + (dz/l_v)^2 ))

The anisotropy (l_h >> l_v) encodes the one thing everybody agrees about
layered geology: things change far faster downwards than sideways. Entropy of
p, normalised to [0, 1], is the uncertainty shown in the viewer. A voxel whose
total evidence weight falls below `min_evidence` is marked unconstrained and
gets entropy 1.0: the honest answer to "what is here?" is "nobody has drilled
near enough to say".

Validation is leave-one-*borehole*-out (not leave-one-point-out, which would
leak a borehole's own column to itself) against the nearest-borehole baseline
that any geologist would use by eye. The gates are in docs/GATES.md.

Frames: if boreholes carry ground levels the model works in elevation (m AOD).
If none do, it works in depth below ground (z = -depth) and says so — that is a
weaker model, and the JSON it bakes carries `frame: "depth"`.

The upgrade path to a properly Bayesian model (Gaussian-process indicator
kriging in PyMC/NumPyro) keeps this module's interface: fit / predict_proba /
loo_validate / bake.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
from scipy.spatial import cKDTree

from .lithology import LITH_CLASSES
from .schema import BoreholeLog

# Classes that carry no lithological information are never training evidence.
NON_EVIDENCE = {"NO_RECOVERY", "UNKNOWN"}
MODEL_CLASSES: tuple[str, ...] = tuple(c for c in LITH_CLASSES if c not in NON_EVIDENCE)
CLASS_INDEX = {c: i for i, c in enumerate(MODEL_CLASSES)}
AIR = 255


@dataclass(frozen=True)
class BoreholeSite:
    """Where a log sits in space. ground_level_m may be None (depth frame)."""

    bgs_id: int
    easting: float
    northing: float
    ground_level_m: float | None = None
    gl_estimated: bool = False


@dataclass
class Samples:
    xyz: np.ndarray  # (n, 3) easting, northing, z
    cls: np.ndarray  # (n,) int class index into MODEL_CLASSES
    bgs_id: np.ndarray  # (n,) int
    frame: str  # "elevation" | "depth"

    def __len__(self) -> int:
        return int(self.xyz.shape[0])


def discretise(logs: Iterable[BoreholeLog], sites: dict[int, BoreholeSite], step_m: float = 0.5) -> Samples:
    """Turn interval logs into labelled points every `step_m` down each column."""
    xs, ys, zs, cs, ids = [], [], [], [], []
    frames = set()
    for log in logs:
        site = sites.get(log.bgs_id)
        if site is None:
            continue
        gl = site.ground_level_m
        frame = "elevation" if gl is not None else "depth"
        frames.add(frame)
        for iv in log.intervals:
            if iv.lith_class in NON_EVIDENCE:
                continue
            d = iv.top_m + step_m / 2
            while d < iv.base_m:
                xs.append(site.easting)
                ys.append(site.northing)
                zs.append((gl - d) if gl is not None else -d)
                cs.append(CLASS_INDEX[iv.lith_class])
                ids.append(log.bgs_id)
                d += step_m
    if len(frames) > 1:
        raise ValueError("mixed frames: some sites have ground levels and some do not; "
                         "estimate the missing ones first (estimate_ground_levels)")
    frame = frames.pop() if frames else "elevation"
    return Samples(
        xyz=np.column_stack([xs, ys, zs]).astype(float) if xs else np.zeros((0, 3)),
        cls=np.asarray(cs, dtype=int),
        bgs_id=np.asarray(ids, dtype=int),
        frame=frame,
    )


def estimate_ground_levels(sites: dict[int, BoreholeSite], length_h: float = 300.0, min_weight: float = 0.05) -> dict[int, BoreholeSite]:
    """Fill missing ground levels from neighbours that have one (2D kernel regression).

    Sites that cannot be estimated (no GL-bearing neighbour within reach) are
    left as None. If *no* site has a GL, returns the input unchanged and the
    model will run in the depth frame.
    """
    known = [s for s in sites.values() if s.ground_level_m is not None]
    if not known:
        return dict(sites)
    kxy = np.array([[s.easting, s.northing] for s in known]) / length_h
    kgl = np.array([s.ground_level_m for s in known])
    tree = cKDTree(kxy)
    out: dict[int, BoreholeSite] = {}
    for bid, s in sites.items():
        if s.ground_level_m is not None:
            out[bid] = s
            continue
        q = np.array([s.easting, s.northing]) / length_h
        idx = tree.query_ball_point(q, r=3.0)
        if not idx:
            out[bid] = s
            continue
        d2 = ((kxy[idx] - q) ** 2).sum(axis=1)
        w = np.exp(-d2)
        if w.sum() < min_weight:
            out[bid] = s
            continue
        gl = float((w * kgl[idx]).sum() / w.sum())
        out[bid] = BoreholeSite(bid, s.easting, s.northing, gl, gl_estimated=True)
    return out


@dataclass
class KernelLithologyModel:
    length_h: float = 300.0
    length_v: float = 2.5
    prior_strength: float = 1.0
    search_radius: float = 3.0  # in scaled units; exp(-9) ~ 1e-4 is negligible beyond
    min_evidence: float = 0.05  # below this total weight a voxel is "unconstrained"
    _tree: cKDTree | None = field(default=None, repr=False)
    _samples: Samples | None = field(default=None, repr=False)

    @property
    def n_classes(self) -> int:
        return len(MODEL_CLASSES)

    def _scale(self, xyz: np.ndarray) -> np.ndarray:
        return xyz / np.array([self.length_h, self.length_h, self.length_v])

    def fit(self, samples: Samples) -> KernelLithologyModel:
        if len(samples) == 0:
            raise ValueError("no samples to fit")
        self._samples = samples
        self._tree = cKDTree(self._scale(samples.xyz))
        return self

    def predict_proba(self, xyz: np.ndarray, exclude_bgs_id: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Return (proba (n, K), evidence (n,)) for query points."""
        assert self._tree is not None and self._samples is not None, "call fit() first"
        q = self._scale(np.atleast_2d(xyz).astype(float))
        K = self.n_classes
        alpha = self.prior_strength
        proba = np.full((q.shape[0], K), 1.0 / K)
        evidence = np.zeros(q.shape[0])
        neighbours = self._tree.query_ball_point(q, r=self.search_radius)
        s_xyz = self._tree.data
        s_cls, s_ids = self._samples.cls, self._samples.bgs_id
        for i, idx in enumerate(neighbours):
            if not idx:
                continue
            idx = np.asarray(idx)
            if exclude_bgs_id is not None:
                idx = idx[s_ids[idx] != exclude_bgs_id]
                if idx.size == 0:
                    continue
            d2 = ((s_xyz[idx] - q[i]) ** 2).sum(axis=1)
            w = np.exp(-d2)
            counts = np.bincount(s_cls[idx], weights=w, minlength=K)
            total = w.sum()
            evidence[i] = total
            proba[i] = (alpha / K + counts) / (alpha + total)
        return proba, evidence

    @staticmethod
    def entropy(proba: np.ndarray) -> np.ndarray:
        K = proba.shape[1]
        p = np.clip(proba, 1e-12, 1.0)
        return -(p * np.log(p)).sum(axis=1) / math.log(K)

    # ------------------------------------------------------------ validation

    def loo_validate(self, samples: Samples, calibration_threshold: float = 0.8) -> dict:
        """Leave-one-borehole-out. Returns accuracy, baseline accuracy, calibration, per-borehole rows."""
        self.fit(samples)
        ids = np.unique(samples.bgs_id)
        correct = total = 0
        base_correct = base_total = 0
        conf_correct = conf_total = 0
        constrained_total = 0
        rows = []
        for bid in ids:
            mask = samples.bgs_id == bid
            xyz, truth = samples.xyz[mask], samples.cls[mask]
            proba, evidence = self.predict_proba(xyz, exclude_bgs_id=int(bid))
            pred = proba.argmax(axis=1)
            constrained = evidence >= self.min_evidence
            hit = (pred == truth) & constrained
            correct += int(hit.sum())
            total += int(mask.sum())
            constrained_total += int(constrained.sum())
            conf = proba.max(axis=1) >= calibration_threshold
            conf_total += int((conf & constrained).sum())
            conf_correct += int((conf & hit).sum())
            b_pred = self._nearest_borehole_baseline(xyz, exclude_bgs_id=int(bid))
            valid = b_pred >= 0
            base_correct += int(((b_pred == truth) & valid).sum())
            base_total += int(mask.sum())
            rows.append({
                "bgs_id": int(bid), "n": int(mask.sum()),
                "accuracy": float(hit.sum() / mask.sum()),
                "baseline_accuracy": float(((b_pred == truth) & valid).sum() / mask.sum()),
                "constrained_fraction": float(constrained.mean()),
            })
        acc = correct / total if total else None
        base = base_correct / base_total if base_total else None
        return {
            "n_boreholes": int(ids.size), "n_points": int(total),
            "accuracy": acc, "baseline_accuracy": base,
            "gain_pp": (100 * (acc - base)) if (acc is not None and base is not None) else None,
            "calibration_at_threshold": (conf_correct / conf_total) if conf_total else None,
            "calibration_threshold": calibration_threshold,
            "confident_fraction": (conf_total / total) if total else None,
            "constrained_fraction": (constrained_total / total) if total else None,
            "params": {"length_h": self.length_h, "length_v": self.length_v,
                       "prior_strength": self.prior_strength},
            "per_borehole": rows,
        }

    def _nearest_borehole_baseline(self, xyz: np.ndarray, exclude_bgs_id: int, max_dz: float = 3.0) -> np.ndarray:
        """Class at the same z in the horizontally nearest *other* borehole; -1 if none."""
        assert self._samples is not None
        s = self._samples
        out = np.full(xyz.shape[0], -1, dtype=int)
        other = s.bgs_id != exclude_bgs_id
        if not other.any():
            return out
        # One representative (x, y) per other borehole.
        ids, first = np.unique(s.bgs_id[other], return_index=True)
        oxy = s.xyz[other][first, :2]
        tree = cKDTree(oxy)
        _, nn = tree.query(xyz[:, :2])
        for i, k in enumerate(nn):
            col = (s.bgs_id == ids[k])
            dz = np.abs(s.xyz[col, 2] - xyz[i, 2])
            j = dz.argmin()
            if dz[j] <= max_dz:
                out[i] = s.cls[col][j]
        return out

    def tune(self, samples: Samples, grid_h=(150.0, 300.0, 600.0), grid_v=(1.5, 2.5, 5.0)) -> dict:
        """Small grid search on LOO accuracy. Returns the best result (params inside)."""
        best = None
        for lh in grid_h:
            for lv in grid_v:
                self.length_h, self.length_v = lh, lv
                res = self.loo_validate(samples)
                if best is None or (res["accuracy"] or 0) > (best["accuracy"] or 0):
                    best = res
        assert best is not None
        self.length_h = best["params"]["length_h"]
        self.length_v = best["params"]["length_v"]
        self.fit(samples)
        return best

    # ------------------------------------------------------------ baking

    def bake(
        self,
        bbox_bng: tuple[float, float, float, float],
        cell_xy: float,
        cell_z: float,
        z0: float,
        z1: float,
        surface_fn,
        sites: dict[int, BoreholeSite] | None = None,
        name: str = "model",
    ) -> dict:
        """Evaluate the model on a voxel grid and return the viewer's JSON shape.

        `surface_fn(easting_array, northing_array) -> elevation_array` gives the
        ground surface per column; voxels whose top is above it are AIR (255).
        In the depth frame pass a function returning zeros and z0 <= -max_depth.
        """
        x0, y0, x1, y1 = bbox_bng
        nx, ny = int(math.ceil((x1 - x0) / cell_xy)), int(math.ceil((y1 - y0) / cell_xy))
        nz = int(math.ceil((z1 - z0) / cell_z))
        xi = x0 + (np.arange(nx) + 0.5) * cell_xy
        yi = y0 + (np.arange(ny) + 0.5) * cell_xy
        gx, gy = np.meshgrid(xi, yi)  # (ny, nx)
        surface = np.asarray(surface_fn(gx.ravel(), gy.ravel()), dtype=float)  # (ny*nx,)
        class_idx = np.full(nx * ny * nz, AIR, dtype=int)
        entropy = np.ones(nx * ny * nz)
        constrained = np.zeros(nx * ny * nz, dtype=bool)
        for k in range(nz):
            zc = z0 + (k + 0.5) * cell_z
            ztop = z0 + (k + 1) * cell_z
            below = ztop <= surface + 1e-9
            if not below.any():
                continue
            pts = np.column_stack([gx.ravel()[below], gy.ravel()[below], np.full(below.sum(), zc)])
            proba, evidence = self.predict_proba(pts)
            sl = slice(k * nx * ny, (k + 1) * nx * ny)
            idx = np.arange(nx * ny)[below] + k * nx * ny
            class_idx[idx] = proba.argmax(axis=1)
            ok = evidence >= self.min_evidence
            ent = self.entropy(proba)
            ent[~ok] = 1.0
            entropy[idx] = ent
            constrained[idx] = ok
        return {
            "synthetic": False,
            "name": name,
            "frame": self._samples.frame if self._samples else "elevation",
            "origin_bng": [x0, y0],
            "cell_xy": cell_xy, "cell_z": cell_z,
            "nx": nx, "ny": ny, "nz": nz,
            "z0": z0,
            "classes": list(MODEL_CLASSES),
            "class_idx": class_idx.tolist(),
            "entropy": [round(float(e), 2) for e in entropy],
            "surface": [round(float(s), 1) for s in surface],
            "constrained_voxels": int(constrained.sum()),
            "below_ground_voxels": int((class_idx != AIR).sum()),
            "boreholes": [
                {"x": s.easting, "y": s.northing,
                 "depth": float(max((self._depth_of(bid) for bid in [s.bgs_id]), default=0.0)),
                 "gl": s.ground_level_m}
                for s in (sites or {}).values()
            ],
            "params": {"length_h": self.length_h, "length_v": self.length_v,
                       "prior_strength": self.prior_strength, "min_evidence": self.min_evidence},
        }

    def _depth_of(self, bgs_id: int) -> float:
        assert self._samples is not None
        s = self._samples
        m = s.bgs_id == bgs_id
        if not m.any():
            return 0.0
        z = s.xyz[m, 2]
        return float(z.max() - z.min() + 0.5)


def kernel_surface(sites: dict[int, BoreholeSite], length_h: float = 300.0, fallback: float | None = None):
    """A surface_fn for bake(): kernel regression of ground level over the site set."""
    known = [s for s in sites.values() if s.ground_level_m is not None]
    if not known:
        if fallback is None:
            raise ValueError("no ground levels available; pass fallback= or use the depth frame")
        return lambda x, y: np.full(np.asarray(x).shape, fallback, dtype=float)
    kxy = np.array([[s.easting, s.northing] for s in known])
    kgl = np.array([s.ground_level_m for s in known])
    mean_gl = float(kgl.mean())
    tree = cKDTree(kxy / length_h)

    def fn(x, y):
        q = np.column_stack([np.asarray(x, float), np.asarray(y, float)]) / length_h
        out = np.full(q.shape[0], mean_gl)
        for i, idx in enumerate(tree.query_ball_point(q, r=3.0)):
            if not idx:
                continue
            d2 = ((tree.data[idx] - q[i]) ** 2).sum(axis=1)
            w = np.exp(-d2)
            if w.sum() > 1e-6:
                out[i] = (w * kgl[idx]).sum() / w.sum()
        return out

    return fn


def write_model_json(payload: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return path
