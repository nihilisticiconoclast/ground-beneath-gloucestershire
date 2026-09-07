"""Typed access to config/gbg.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/gbg.toml")

BBox = tuple[float, float, float, float]  # min_x, min_y, max_x, max_y


@dataclass(frozen=True)
class ClientConfig:
    user_agent: str
    requests_per_second: float
    max_scans_per_run: int
    timeout_seconds: float
    max_retries: int


@dataclass(frozen=True)
class PathsConfig:
    db: Path
    raw_scans: Path
    page_images: Path
    extractions: Path
    gold: Path
    # Cache for the AGS log sheets `ags_log_url` serves (see gbg.gwbv). Defaulted
    # so a config written before the gold-from-PDF path existed still loads.
    ags_logs: Path = Path("data/raw/ags_logs")


@dataclass(frozen=True)
class Aoi:
    name: str
    description: str
    bbox_bng: BBox

    def validate(self) -> None:
        x0, y0, x1, y1 = self.bbox_bng
        if not (x0 < x1 and y0 < y1):
            raise ValueError(f"AOI {self.name!r}: bbox must be [min_e, min_n, max_e, max_n]")
        # Great Britain fits comfortably inside these bounds; anything else is a typo.
        if not (0 <= x0 and x1 <= 700_000 and 0 <= y0 and y1 <= 1_300_000):
            raise ValueError(f"AOI {self.name!r}: bbox {self.bbox_bng} is outside BNG range")


@dataclass(frozen=True)
class ExtractionConfig:
    provider: str
    ollama_model: str
    ollama_url: str
    anthropic_model: str
    render_dpi: int


@dataclass(frozen=True)
class Gates:
    boundary_tolerance_m: float
    min_boundary_recall: float
    min_lithology_f1: float
    min_gold_boreholes: int
    min_model_gain_pp: float = 5.0
    min_calibration_at_80: float = 0.75
    min_model_boreholes: int = 30


@dataclass(frozen=True)
class ModelConfig:
    length_h: float = 300.0
    length_v: float = 2.5
    prior_strength: float = 1.0
    min_evidence: float = 0.05
    cell_xy: float = 125.0
    cell_z: float = 2.5
    sample_step_m: float = 0.5
    output: Path = Path("web/data/model.json")


@dataclass(frozen=True)
class Config:
    client: ClientConfig
    paths: PathsConfig
    aois: dict[str, Aoi]
    extraction: ExtractionConfig
    gates: Gates
    model: ModelConfig = field(default_factory=ModelConfig)
    source_path: Path = field(default=DEFAULT_CONFIG_PATH)

    def aoi(self, name: str) -> Aoi:
        try:
            return self.aois[name]
        except KeyError:
            known = ", ".join(sorted(self.aois))
            raise KeyError(f"Unknown AOI {name!r}. Known: {known}") from None


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    client = ClientConfig(**raw["client"])
    paths = PathsConfig(**{k: Path(v) for k, v in raw["paths"].items()})
    aois = {
        name: Aoi(name=name, description=body["description"], bbox_bng=tuple(body["bbox_bng"]))
        for name, body in raw["aoi"].items()
    }
    for aoi in aois.values():
        aoi.validate()
    extraction = ExtractionConfig(**raw["extraction"])
    gates = Gates(**raw["gates"])
    model_raw = dict(raw.get("model", {}))
    if "output" in model_raw:
        model_raw["output"] = Path(model_raw["output"])
    model = ModelConfig(**model_raw)
    return Config(
        client=client, paths=paths, aois=aois, extraction=extraction, gates=gates, model=model,
        source_path=path,
    )
