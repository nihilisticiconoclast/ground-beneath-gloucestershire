"""The data contract for a borehole log.

Whatever produces intervals — an AGS file, a vision model, a human — must
produce this shape, and the validators here are the first line of defence
against plausible-looking nonsense (overlapping intervals, depths that run
upwards, a 400 m log for a 12 m borehole).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .lithology import LITH_CLASSES

MAX_PLAUSIBLE_DEPTH_M = 3000.0


class LithInterval(BaseModel):
    top_m: float = Field(ge=0, le=MAX_PLAUSIBLE_DEPTH_M, description="Depth to top, metres below ground")
    base_m: float = Field(gt=0, le=MAX_PLAUSIBLE_DEPTH_M, description="Depth to base, metres below ground")
    raw_description: str = Field(default="", description="Description exactly as written on the log")
    lith_class: str = Field(description="Controlled vocabulary class, see lithology.LITH_CLASSES")
    confidence: float | None = Field(default=None, ge=0, le=1, description="Extractor self-reported confidence")

    @field_validator("lith_class")
    @classmethod
    def _known_class(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in LITH_CLASSES:
            raise ValueError(f"lith_class {v!r} not in controlled vocabulary")
        return v

    @model_validator(mode="after")
    def _top_below_base(self) -> LithInterval:
        if self.base_m <= self.top_m:
            raise ValueError(f"base_m ({self.base_m}) must be greater than top_m ({self.top_m})")
        return self

    @property
    def thickness_m(self) -> float:
        return self.base_m - self.top_m


class BoreholeLog(BaseModel):
    bgs_id: int
    intervals: list[LithInterval]
    source: str = Field(description="'gold:ags' or 'extract:<provider>/<model>'")
    total_depth_m: float | None = Field(default=None, description="Final depth as read from the log, if stated")
    ground_level_m: float | None = Field(default=None, description="Ground level in m AOD, if stated on the log")
    easting: float | None = Field(default=None, description="BNG easting if the log itself states a position")
    northing: float | None = Field(default=None, description="BNG northing if the log itself states a position")
    notes: str = ""

    @model_validator(mode="after")
    def _ordered_and_non_overlapping(self) -> BoreholeLog:
        ivs = sorted(self.intervals, key=lambda i: i.top_m)
        for a, b in zip(ivs, ivs[1:]):
            if b.top_m < a.base_m - 1e-6:
                raise ValueError(
                    f"intervals overlap: [{a.top_m}, {a.base_m}] and [{b.top_m}, {b.base_m}]"
                )
        self.intervals = ivs
        return self

    @property
    def base_depth_m(self) -> float:
        return max((i.base_m for i in self.intervals), default=0.0)

    @property
    def gaps_m(self) -> float:
        """Total unlogged thickness between consecutive intervals (gaps are allowed, but counted)."""
        ivs = self.intervals
        return sum(max(b.top_m - a.base_m, 0.0) for a, b in zip(ivs, ivs[1:]))

    def boundaries(self) -> list[float]:
        """All interval boundaries, deduplicated and sorted (tops and bases)."""
        pts = {round(i.top_m, 3) for i in self.intervals} | {round(i.base_m, 3) for i in self.intervals}
        return sorted(pts)

    def class_at(self, depth_m: float) -> str | None:
        for iv in self.intervals:
            if iv.top_m <= depth_m < iv.base_m:
                return iv.lith_class
        return None

    def plausibility_warnings(self, index_length_m: float | None) -> list[str]:
        """Soft checks that don't invalidate the log but should be surfaced."""
        warnings: list[str] = []
        if index_length_m is not None and self.base_depth_m > index_length_m * 1.15 + 1.0:
            warnings.append(
                f"log base {self.base_depth_m:.1f} m exceeds index length {index_length_m:.1f} m"
            )
        if self.gaps_m > 0.5:
            warnings.append(f"{self.gaps_m:.1f} m of unlogged gaps between intervals")
        if self.intervals and self.intervals[0].top_m > 0.5:
            warnings.append(f"first interval starts at {self.intervals[0].top_m:.1f} m, not surface")
        return warnings
