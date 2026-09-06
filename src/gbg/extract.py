"""Turn a scanned PDF into a BoreholeLog via a vision-capable model.

Three providers share one interface so the eval harness doesn't care which
one produced a log:

  MockExtractor      replays fixture JSON — used by tests and CI, costs nothing
  OllamaExtractor    local vision model via the Ollama HTTP API (free)
  AnthropicExtractor Claude via the Messages API (optional, needs a key)

Output is validated through `schema.BoreholeLog`, so a provider that returns
overlapping intervals or an unknown class fails loudly here rather than
quietly downstream.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol

from .schema import BoreholeLog, LithInterval

PROMPT = resources.files("gbg.prompts").joinpath("extract_log.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- rendering


def render_pdf_pages(pdf_path: Path, out_dir: Path, dpi: int = 150, max_pages: int = 8) -> list[Path]:
    """Rasterise up to `max_pages` pages to PNG; returns the image paths in order."""
    import pymupdf

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            target = out_dir / f"{pdf_path.stem}_p{i + 1:02d}.png"
            if not target.exists():
                page.get_pixmap(dpi=dpi).save(target)
            paths.append(target)
    return paths


# ----------------------------------------------------------------------------- parsing


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_model_json(text: str) -> dict:
    """Extract the JSON object from a model reply, tolerating code fences and preamble."""
    m = _FENCE.search(text)
    candidate = m.group(1) if m else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(candidate[start : end + 1])


def to_log(payload: dict, bgs_id: int, source: str) -> BoreholeLog:
    intervals = [
        LithInterval(
            top_m=float(iv["top_m"]),
            base_m=float(iv["base_m"]),
            raw_description=str(iv.get("raw_description", "")),
            lith_class=str(iv.get("lith_class", "UNKNOWN")),
            confidence=iv.get("confidence"),
        )
        for iv in payload.get("intervals", [])
    ]
    td = payload.get("total_depth_m")
    gl = payload.get("ground_level_m")
    return BoreholeLog(
        bgs_id=bgs_id,
        intervals=intervals,
        source=source,
        total_depth_m=float(td) if td is not None else None,
        ground_level_m=float(gl) if gl is not None else None,
        notes=str(payload.get("notes", "")),
    )


# ----------------------------------------------------------------------------- providers


class Extractor(Protocol):
    source: str

    def extract(self, bgs_id: int, page_images: list[Path]) -> BoreholeLog: ...


@dataclass
class MockExtractor:
    """Replays JSON fixtures keyed by bgs_id from a directory ({bgs_id}.json)."""

    fixture_dir: Path
    source: str = "extract:mock/fixture"

    def extract(self, bgs_id: int, page_images: list[Path]) -> BoreholeLog:
        path = self.fixture_dir / f"{bgs_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return to_log(payload, bgs_id, self.source)


@dataclass
class OllamaExtractor:
    model: str
    base_url: str = "http://localhost:11434"
    timeout: float = 600.0

    @property
    def source(self) -> str:
        return f"extract:ollama/{self.model}"

    def extract(self, bgs_id: int, page_images: list[Path]) -> BoreholeLog:
        import httpx

        images = [base64.b64encode(p.read_bytes()).decode("ascii") for p in page_images]
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "user", "content": PROMPT, "images": images}],
            "options": {"temperature": 0},
        }
        resp = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.json()["message"]["content"]
        return to_log(parse_model_json(text), bgs_id, self.source)


@dataclass
class AnthropicExtractor:
    model: str

    @property
    def source(self) -> str:
        return f"extract:anthropic/{self.model}"

    def extract(self, bgs_id: int, page_images: list[Path]) -> BoreholeLog:
        import anthropic  # optional dependency: pip install "gbg[llm]"

        client = anthropic.Anthropic()
        content: list[dict] = []
        for p in page_images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(p.read_bytes()).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": PROMPT})
        msg = client.messages.create(
            model=self.model, max_tokens=4000, temperature=0,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
        return to_log(parse_model_json(text), bgs_id, self.source)


def make_extractor(provider: str, cfg, fixture_dir: Path | None = None) -> Extractor:
    if provider == "mock":
        return MockExtractor(fixture_dir=fixture_dir or Path("tests/fixtures/extractions"))
    if provider == "ollama":
        return OllamaExtractor(model=cfg.ollama_model, base_url=cfg.ollama_url)
    if provider == "anthropic":
        return AnthropicExtractor(model=cfg.anthropic_model)
    raise ValueError(f"unknown extraction provider {provider!r}")
