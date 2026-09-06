"""Structural rules that must not drift."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "gbg"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in node.names}
    return out


def test_mcp_server_never_touches_the_network_layer():
    imps = _imports(SRC / "mcp_server.py")
    forbidden = {"http", "sobi", "scans", "gbg.http", "gbg.sobi", "gbg.scans", "httpx", "requests"}
    assert not (imps & forbidden), f"mcp_server imports fetchers: {imps & forbidden}"


def test_only_http_module_creates_http_clients():
    offenders = []
    for path in SRC.glob("*.py"):
        if path.name in ("http.py", "extract.py"):  # extract.py posts to a *local* Ollama only
            continue
        if "httpx.Client(" in path.read_text() or "httpx.get(" in path.read_text():
            offenders.append(path.name)
    assert not offenders, f"HTTP clients outside gbg.http: {offenders}"
