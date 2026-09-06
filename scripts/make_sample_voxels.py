"""Write web/data/sample_voxels.json — a SYNTHETIC preview model for the viewer.

This is not data. It is a plausible-looking stack (Cotswold-style layers with a
gentle ESE dip, a valley cut with alluvium, and uncertainty that grows away from
pretend boreholes) so that the viewer, controls and colour scheme can be built
and reviewed before Stage 2 produces a real model. The real model writer must
emit the same JSON shape; the viewer does not care which it is loading, but the
`synthetic: true` flag is shown on screen when set.

Pure Python on purpose (no numpy) so it runs anywhere the repo is checked out.

Shape:
{
  "synthetic": true,
  "name": "...",
  "origin_bng": [easting, northing],       # SW corner of the grid, metres
  "cell_xy": 125, "cell_z": 2.5,           # voxel size, metres
  "nx": 40, "ny": 40, "nz": 24,
  "z0": -20.0,                             # elevation (m AOD) of the bottom of layer 0
  "classes": ["TOPSOIL", ...],             # index -> class name
  "class_idx": [...],                      # length nx*ny*nz, order: x fastest, then y, then z
  "entropy": [...],                        # same order, 0..1, 2 dp
  "surface": [...],                        # length nx*ny, ground elevation m AOD
  "boreholes": [{"x": .., "y": .., "depth": ..}]
}
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "web" / "data" / "sample_voxels.json"

CLASSES = ["TOPSOIL", "MADE_GROUND", "PEAT", "CLAY", "SILT", "SAND", "GRAVEL", "MARL",
           "MUDSTONE", "SILTSTONE", "SANDSTONE", "LIMESTONE", "IRONSTONE", "COAL", "CHALK",
           "NO_RECOVERY", "UNKNOWN"]
IDX = {c: i for i, c in enumerate(CLASSES)}

NX, NY, NZ = 40, 40, 30
CELL_XY, CELL_Z = 125.0, 2.5  # 5 km x 5 km footprint, 75 m of column
ORIGIN = (382500.0, 202500.0)  # pilot tile SW corner (matches config/gbg.toml)
Z0 = 40.0  # bottom of the model, m AOD


def surface_elevation(x: float, y: float) -> float:
    """A plateau at ~110 m AOD falling to a meandering valley floor at ~55 m."""
    # Valley runs roughly W-E across the tile with a meander.
    valley_centre = 0.5 + 0.12 * math.sin(2 * math.pi * x * 1.3 + 0.8)
    d = abs(y - valley_centre)  # distance from the valley axis, in tile units
    valley = 55.0 + 55.0 * min(1.0, (d / 0.22) ** 1.4)
    plateau_dip = -8.0 * x  # gentle fall towards the east
    return valley + plateau_dip


def layer_class(z: float, x: float, y: float, surface: float) -> str:
    """Stratigraphy at elevation z, given the local ground surface."""
    depth = surface - z
    if depth < 0:
        return "AIR"
    # Bedrock boundaries fall gently to the ESE (about a third of a degree here,
    # exaggerated relative to the 5 km tile so the dip is visible in the viewer).
    dip = -0.006 * (x * NX * CELL_XY) - 0.003 * ((1 - y) * NY * CELL_XY)
    base_oolite = 86.0 + dip    # base of the Inferior Oolite limestone, m AOD
    marl_top = 74.0 + dip       # a marl band within the Lias mudstone
    valley_floor = 55.0 + 0.6 * abs(math.sin(2 * math.pi * x * 1.3 + 0.8))
    in_valley = surface < 68.0
    if depth < 0.5:
        return "TOPSOIL"
    if in_valley and depth < 2.5:
        return "CLAY"          # alluvial clay
    if in_valley and z > valley_floor - 6.0:
        return "GRAVEL" if depth < 6.0 else "SAND"   # valley gravels over sand
    if depth < 3.0 and not in_valley:
        return "CLAY"          # head / weathered clay on the slopes
    if z > base_oolite:
        return "LIMESTONE"
    if marl_top - 2.5 < z <= marl_top:
        return "MARL"
    return "MUDSTONE"


def main(seed: int = 7) -> None:
    rng = random.Random(seed)
    boreholes = []
    for _ in range(38):
        x, y = rng.random(), rng.random()
        boreholes.append({"x": ORIGIN[0] + x * NX * CELL_XY, "y": ORIGIN[1] + y * NY * CELL_XY,
                          "depth": round(rng.uniform(6, 45), 1)})
    surface = []
    for j in range(NY):
        for i in range(NX):
            surface.append(round(surface_elevation((i + 0.5) / NX, (j + 0.5) / NY), 1))

    class_idx: list[int] = []
    entropy: list[float] = []
    for k in range(NZ):
        z = Z0 + (k + 0.5) * CELL_Z
        for j in range(NY):
            for i in range(NX):
                x, y = (i + 0.5) / NX, (j + 0.5) / NY
                s = surface[j * NX + i]
                cls = layer_class(z, x, y, s)
                if cls == "AIR":
                    class_idx.append(255)
                    entropy.append(0.0)
                    continue
                class_idx.append(IDX[cls])
                # Uncertainty: grows with distance to the nearest pretend borehole and
                # with depth below the deepest nearby one; a little noise so it isn't smooth.
                ex, ey = ORIGIN[0] + x * NX * CELL_XY, ORIGIN[1] + y * NY * CELL_XY
                best = 1e9
                for b in boreholes:
                    d = math.hypot(b["x"] - ex, b["y"] - ey)
                    below = max(0.0, (s - z) - b["depth"])
                    best = min(best, d + 25.0 * below)
                e = 1 - math.exp(-best / 900.0)
                e = min(1.0, max(0.02, e + rng.uniform(-0.05, 0.05)))
                entropy.append(round(e, 2))

    payload = {
        "synthetic": True,
        "name": "Stroud pilot tile — synthetic preview (not data)",
        "origin_bng": list(ORIGIN),
        "cell_xy": CELL_XY,
        "cell_z": CELL_Z,
        "nx": NX, "ny": NY, "nz": NZ,
        "z0": Z0,
        "classes": CLASSES,
        "class_idx": class_idx,
        "entropy": entropy,
        "surface": surface,
        "boreholes": boreholes,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    n_air = sum(1 for c in class_idx if c == 255)
    print(f"wrote {OUT} — {NX*NY*NZ:,} voxels, {NX*NY*NZ - n_air:,} below ground, "
          f"{len(boreholes)} synthetic boreholes, {OUT.stat().st_size/1024:.0f} kB")


if __name__ == "__main__":
    main()
