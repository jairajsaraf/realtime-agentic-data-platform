"""Boundary test for the Stage D agent package (runs on the DEFAULT install, no extra needed).

Enforces the architecture rule `agent -> HTTP API` only, mirroring the MCP boundary test in
tests/test_mcp_cli.py: every import in src/rtdp/agent/*.py is inspected via ast, so a direct
query/catalog/table/ingest access path can never creep into the agent unnoticed.
"""

from __future__ import annotations

import ast
from pathlib import Path

_AGENT_PACKAGE = "rtdp.agent"
_AGENT_DIR = Path(__file__).resolve().parents[1] / "src" / "rtdp" / "agent"

# rtdp modules outside the agent package the agent may import: config + the telemetry boundary.
_ALLOWED_RTDP_IMPORTS = {"rtdp.config", "rtdp.telemetry"}
# Data-plane packages that must never appear (query/catalog/table/ingest access paths).
_FORBIDDEN_TOP_LEVEL = {"duckdb", "pyiceberg", "boto3", "pandas", "pandera", "pyarrow"}


def _imports_of(path: Path) -> set[str]:
    """Absolute module names imported by a file, resolving relative imports in rtdp.agent."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                found.add(node.module or "")
            else:
                parts = _AGENT_PACKAGE.split(".")
                base = parts[: len(parts) - (node.level - 1)]
                if node.module:
                    found.add(".".join([*base, node.module]))
                else:
                    # `from . import dq` / `from .. import query`: the names ARE the modules.
                    found.update(".".join([*base, alias.name]) for alias in node.names)
    return found


def test_agent_package_respects_api_boundary():
    """The agent reaches data ONLY via HTTP: no query/catalog/table/ingest imports."""
    py_files = sorted(_AGENT_DIR.glob("*.py"))
    assert py_files, f"no sources found under {_AGENT_DIR}"
    for py in py_files:
        for module in _imports_of(py):
            top = module.partition(".")[0]
            assert top not in _FORBIDDEN_TOP_LEVEL, f"{py.name} imports {module}"
            if top == "rtdp" and not module.startswith(f"{_AGENT_PACKAGE}."):
                assert module in _ALLOWED_RTDP_IMPORTS, (
                    f"{py.name} imports {module}; only {_ALLOWED_RTDP_IMPORTS} and "
                    f"{_AGENT_PACKAGE}.* are allowed"
                )


def test_boundary_check_catches_a_forbidden_import(tmp_path):
    """Guard against a vacuous pass: the resolver must see relative and absolute imports."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from .. import query\nfrom ..catalog import build_catalog\nimport duckdb\n",
        encoding="utf-8",
    )
    assert _imports_of(probe) == {"rtdp.query", "rtdp.catalog", "duckdb"}
