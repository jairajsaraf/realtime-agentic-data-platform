"""CLI wiring + boundary tests for `rtdp mcp` that run on the DEFAULT install (no extra).

The import guard is precise by design: the install hint appears only when the missing module
is the `mcp` SDK itself; any other ImportError (a bug in rtdp.mcp.server, a broken transitive
install) must re-raise as a real failure. The boundary test enforces the architecture rule
(`MCP server -> HTTP API` only) by inspecting the package's imports via ast — no extra needed.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from rtdp.cli import main

_MCP_PACKAGE = "rtdp.mcp"
_MCP_DIR = Path(__file__).resolve().parents[1] / "src" / "rtdp" / "mcp"

# The only rtdp modules the MCP server may import: config + the pure-Pydantic API models.
_ALLOWED_RTDP_IMPORTS = {"rtdp.config", "rtdp.api.models"}
# Data-plane packages that must never appear (query/catalog/table/ingest access paths).
_FORBIDDEN_TOP_LEVEL = {"duckdb", "pyiceberg", "boto3", "pandas", "pandera", "pyarrow"}


def _block_mcp_sdk(monkeypatch):
    """Simulate the missing [mcp] extra even when it is installed (cf. test_telemetry.py).

    Purging matters and must be complete: a cached ``mcp.server.fastmcp`` would satisfy the
    SDK import without ever touching the blocked top-level ``mcp``, and a cached
    ``rtdp.mcp.server`` would let the CLI's lazy import skip importing entirely. The parent
    ``rtdp`` package attribute is monkeypatched too so the re-import triggered here can't
    leave a stale ``rtdp.mcp`` binding behind for later tests.
    """
    for name in [m for m in sys.modules if m == "mcp" or m.startswith("mcp.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.delitem(sys.modules, "rtdp.mcp.server", raising=False)
    monkeypatch.delitem(sys.modules, "rtdp.mcp", raising=False)
    import rtdp

    if hasattr(rtdp, "mcp"):
        monkeypatch.delattr(rtdp, "mcp")


# ------------------------------------------------------------------ import guard
def test_cli_mcp_missing_extra_exits_2_with_hint(monkeypatch, capsys):
    _block_mcp_sdk(monkeypatch)
    rc = main(["mcp"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--extra mcp" in err
    assert "rtdp[mcp]" in err


def test_cli_mcp_reraises_non_sdk_import_errors(monkeypatch):
    # A failure inside our own module (name != "mcp") must surface, never show the hint.
    monkeypatch.setitem(sys.modules, "rtdp.mcp.server", None)
    with pytest.raises(ImportError):
        main(["mcp"])


def test_core_cli_works_with_sdk_blocked(monkeypatch, capsys):
    _block_mcp_sdk(monkeypatch)
    assert main(["info"]) == 0
    assert "Resolved RTDP configuration" in capsys.readouterr().out


def test_cli_mcp_subcommand_registered(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["mcp", "--help"])
    assert excinfo.value.code == 0
    assert "--api-url" in capsys.readouterr().out


# ------------------------------------------------------------------ CLI -> server wiring
def test_cli_mcp_api_url_override(monkeypatch):
    pytest.importorskip("mcp", reason="requires the optional [mcp] extra")
    # Patch the sys.modules-resolved module: the CLI's `from .mcp.server import serve` reads
    # that exact object, while a plain `import rtdp.mcp.server as m` can bind a stale one via
    # the parent package attribute.
    mcp_server = importlib.import_module("rtdp.mcp.server")

    captured = {}

    def fake_serve(settings):
        captured["base_url"] = settings.agent_api_base_url
        return 0

    monkeypatch.setattr(mcp_server, "serve", fake_serve)
    assert main(["mcp", "--api-url", "http://example.invalid:9"]) == 0
    assert captured["base_url"] == "http://example.invalid:9"


# ------------------------------------------------------------------ boundary
def _imports_of(path: Path) -> set[str]:
    """Absolute module names imported by a file, resolving relative imports in rtdp.mcp."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                found.add(node.module or "")
            else:
                parts = _MCP_PACKAGE.split(".")
                base = parts[: len(parts) - (node.level - 1)]
                found.add(".".join([*base, node.module] if node.module else base))
    return found


def test_mcp_package_respects_api_boundary():
    """The MCP server reaches data ONLY via HTTP: no query/catalog/table/ingest imports."""
    py_files = sorted(_MCP_DIR.glob("*.py"))
    assert py_files, f"no sources found under {_MCP_DIR}"
    for py in py_files:
        for module in _imports_of(py):
            top = module.partition(".")[0]
            assert top not in _FORBIDDEN_TOP_LEVEL, f"{py.name} imports {module}"
            if top == "rtdp":
                assert module in _ALLOWED_RTDP_IMPORTS, (
                    f"{py.name} imports {module}; only {_ALLOWED_RTDP_IMPORTS} are allowed"
                )
