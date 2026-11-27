"""Tests for MCP_ROLE-driven farm tool gating and the mcp_role() resolver."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from blender_mcp.utils.farm_workers import mcp_role, worker_identity

_SRC = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# mcp_role() resolution
# ---------------------------------------------------------------------------


def test_mcp_role_defaults_to_stand_alone_when_unset():
    assert mcp_role({}) == "stand-alone"
    assert mcp_role({"MCP_ROLE": ""}) == "stand-alone"
    assert mcp_role({"MCP_ROLE": "   "}) == "stand-alone"


def test_mcp_role_normalizes_case_and_whitespace():
    assert mcp_role({"MCP_ROLE": "  MASTER "}) == "master"
    assert mcp_role({"MCP_ROLE": "Worker"}) == "worker"


@pytest.mark.parametrize("value", ["stand-alone", "standalone", "stand_alone", " STANDALONE "])
def test_mcp_role_accepts_standalone_aliases(value):
    assert mcp_role({"MCP_ROLE": value}) == "stand-alone"


def test_mcp_role_falls_back_with_warning_on_unknown_value(caplog):
    with caplog.at_level(logging.WARNING, logger="blender_mcp.utils.farm_workers"):
        assert mcp_role({"MCP_ROLE": "garbage"}) == "stand-alone"
    assert any("MCP_ROLE" in record.message for record in caplog.records)


def test_mcp_role_reads_real_environment(monkeypatch):
    monkeypatch.setenv("MCP_ROLE", "worker")
    assert mcp_role() == "worker"
    monkeypatch.delenv("MCP_ROLE")
    assert mcp_role() == "stand-alone"


# ---------------------------------------------------------------------------
# worker_identity() role field
# ---------------------------------------------------------------------------


def test_worker_identity_includes_resolved_role(monkeypatch):
    monkeypatch.setenv("MCP_ROLE", "master")
    monkeypatch.setenv("BLENDER_FARM_WORKER_ID", "role-worker")
    identity = worker_identity()
    assert identity["role"] == "master"
    assert identity["worker_id"] == "role-worker"


# ---------------------------------------------------------------------------
# Gating: fresh subprocess per role, no pre-warmed singleton
# ---------------------------------------------------------------------------


def _list_tools_for_role(role: str | None) -> dict[str, bool]:
    """Import blender_mcp.app in a fresh interpreter and report farm tool presence."""
    env = os.environ.copy()
    env.pop("MCP_ROLE", None)
    if role is not None:
        env["MCP_ROLE"] = role
    env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import asyncio, json\n"
        "from blender_mcp.app import get_app\n"
        "app = get_app()\n"
        "tools = [t.name for t in asyncio.run(app.list_tools())]\n"
        "print(json.dumps({'farm': 'blender_farm' in tools, 'worker': 'blender_farm_worker' in tools}))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=_SRC.parent,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_master_role_exposes_only_blender_farm():
    tools = _list_tools_for_role("master")
    assert tools == {"farm": True, "worker": False}


def test_worker_role_exposes_only_blender_farm_worker():
    tools = _list_tools_for_role("worker")
    assert tools == {"farm": False, "worker": True}


def test_standalone_role_exposes_both_farm_tools():
    tools = _list_tools_for_role("stand-alone")
    assert tools == {"farm": True, "worker": True}


def test_unset_role_exposes_both_farm_tools():
    tools = _list_tools_for_role(None)
    assert tools == {"farm": True, "worker": True}
