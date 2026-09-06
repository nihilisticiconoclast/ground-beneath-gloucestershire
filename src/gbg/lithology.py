"""Controlled lithology vocabulary and a free-text normaliser.

The classes are deliberately coarse: they are what a voxel can plausibly carry
and what a vision model can be asked to commit to. Finer taxonomy (formation
names, BGS Lexicon codes) is a later, separate column — never a replacement.

Normalisation follows the BS 5930 convention where the principal constituent
is written in CAPITALS ("Soft grey sandy CLAY" -> CLAY) and falls back to
keyword scoring when a log doesn't follow it. Returns a confidence so that a
guess is never indistinguishable from a read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters only for tie-breaking on equal keyword score (earlier wins).
LITH_CLASSES: tuple[str, ...] = (
    "TOPSOIL",
    "MADE_GROUND",
    "PEAT",
    "CLAY",
    "SILT",
    "SAND",
    "GRAVEL",
    "MARL",
    "MUDSTONE",
    "SILTSTONE",
    "SANDSTONE",
    "LIMESTONE",
    "IRONSTONE",
    "COAL",
    "CHALK",
    "NO_RECOVERY",
    "UNKNOWN",
)

# keyword (regex, case-insensitive) -> class. Longer/more specific patterns first.
_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmade[\s-]?ground\b|\bfill\b|\bhardcore\b|\brubble\b|\bbrick", re.I), "MADE_GROUND"),
    (re.compile(r"\btop[\s-]?soil\b|\bsoil\b(?!\s+report)", re.I), "TOPSOIL"),
    (re.compile(r"\bpeat\w*", re.I), "PEAT"),
    (re.compile(r"\bno[\s-]?recovery\b|\bvoid\b|\bcavity\b|\bnot recovered\b", re.I), "NO_RECOVERY"),
    (re.compile(r"\bmudstone\w*|\bshale\w*|\bclaystone\w*", re.I), "MUDSTONE"),
    (re.compile(r"\bsiltstone\w*", re.I), "SILTSTONE"),
    (re.compile(r"\bsandstone\w*|\bgrit\w*", re.I), "SANDSTONE"),
    (re.compile(r"\blimestone\w*|\boolit\w*|\bragstone\w*|\bfreestone\w*", re.I), "LIMESTONE"),
    (re.compile(r"\bironstone\w*", re.I), "IRONSTONE"),
    (re.compile(r"\bcoal\b|\bcoaly\b|\bseam\b", re.I), "COAL"),
    (re.compile(r"\bchalk\w*", re.I), "CHALK"),
    (re.compile(r"\bmarl\w*", re.I), "MARL"),
    (re.compile(r"\bgravel\w*|\bcobble\w*|\bboulder\w*|\bpebbl\w*", re.I), "GRAVEL"),
    (re.compile(r"\bsand\b|\bsands\b|\bsandy\b", re.I), "SAND"),
    (re.compile(r"\bsilt\b|\bsilts\b|\bsilty\b", re.I), "SILT"),
    (re.compile(r"\bclay\b|\bclays\b|\bclayey\b|\bmarly clay\b", re.I), "CLAY"),
]

# For the BS 5930 principal-constituent rule: a fully upper-case word that maps to a class.
_PRINCIPAL = {
    "CLAY": "CLAY", "SILT": "SILT", "SAND": "SAND", "GRAVEL": "GRAVEL", "PEAT": "PEAT",
    "MUDSTONE": "MUDSTONE", "SILTSTONE": "SILTSTONE", "SANDSTONE": "SANDSTONE",
    "LIMESTONE": "LIMESTONE", "IRONSTONE": "IRONSTONE", "COAL": "COAL", "CHALK": "CHALK",
    "MARL": "MARL", "TOPSOIL": "TOPSOIL", "COBBLES": "GRAVEL", "BOULDERS": "GRAVEL",
    "SHALE": "MUDSTONE", "OOLITE": "LIMESTONE",
}

_MADE_GROUND_UPPER = re.compile(r"\bMADE\s+GROUND\b")


@dataclass(frozen=True)
class Normalised:
    lith_class: str
    confidence: float
    rule: str


def normalise(description: str | None) -> Normalised:
    """Map a free-text lithology description to a controlled class.

    Confidence bands (used downstream, so keep them meaningful):
      0.9  principal constituent written in capitals (BS 5930 style)
      0.7  exactly one class keyword family matched
      0.5  several families matched; the last-mentioned rock/soil noun wins
      0.0  nothing matched -> UNKNOWN
    """
    if not description or not description.strip():
        return Normalised("UNKNOWN", 0.0, "empty")
    text = description.strip()

    if _MADE_GROUND_UPPER.search(text):
        return Normalised("MADE_GROUND", 0.9, "principal-capitals")

    # Rule 1: BS 5930 principal constituent in capitals. Take the LAST capitalised
    # principal — "Sandy CLAY over LIMESTONE" is rare in one interval, but the
    # convention puts the principal noun last ("silty sandy CLAY").
    caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", text) if w in _PRINCIPAL]
    if caps:
        return Normalised(_PRINCIPAL[caps[-1]], 0.9, "principal-capitals")

    # Rule 2: keyword families.
    hits: list[tuple[int, str]] = []  # (position of last match, class)
    for pattern, cls in _KEYWORDS:
        last = None
        for m in pattern.finditer(text):
            last = m.start()
        if last is not None:
            hits.append((last, cls))
    if not hits:
        return Normalised("UNKNOWN", 0.0, "no-match")
    families = {cls for _, cls in hits}
    if len(families) == 1:
        return Normalised(hits[0][1], 0.7, "single-keyword")
    # Several families: the one mentioned last is usually the principal ("clayey SAND"),
    # but modifiers like "sandy" come first, so "last mention" is a reasonable prior.
    hits.sort()
    return Normalised(hits[-1][1], 0.5, "last-of-several")
