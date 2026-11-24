"""Image version must be used by the HTTP and MCP public surfaces."""

import asyncio
import json
import os
import subprocess
import sys

from fastmcp import FastMCP
from starlette.testclient import TestClient

from blender_mcp import __version__, app_version
from blender_mcp.app import _register_fleet_api, get_app


def test_app_version_uses_environment_and_package_fallback(monkeypatch, capsys):
    monkeypatch.setenv("BLENDER_MCP_VERSION", "1.0.8")
    assert app_version() == "1.0.8"
    monkeypatch.delenv("BLENDER_MCP_VERSION")
    assert app_version() == __version__ == "0.11.1"
    assert capsys.readouterr().out == ""


def test_health_returns_runtime_version(monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_VERSION", "1.0.8")
    mcp = FastMCP("health-test")
    _register_fleet_api(mcp)
    with TestClient(mcp.http_app()) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["version"] == "1.0.8"


def test_mcp_server_metadata_uses_runtime_version():
    env = {**os.environ, "BLENDER_MCP_VERSION": "1.0.8"}
    result = subprocess.run(
        [sys.executable, "-c", "from blender_mcp.app import get_app; print(get_app().version)"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "1.0.8"


def test_status_tools_use_runtime_version(monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_VERSION", "1.0.8")
    mcp = get_app()

    async def call_versions():
        status = await mcp.call_tool("blender_status", {"operation": "status", "format": "json"})
        info = await mcp.call_tool("server_info", {})
        card = await mcp.call_tool("show_blender_status_app", {})
        return status, info, card

    status, info, card = asyncio.run(call_versions())
    assert json.loads(status.content[0].text)["version"] == "1.0.8"
    assert json.loads(info.content[0].text)["version"] == "1.0.8"
    assert "Version 1.0.8" in card.content[0].text
