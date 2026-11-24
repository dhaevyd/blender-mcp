"""Public diagnostics exposes bounded numeric telemetry without private paths."""

import asyncio
import json
from types import SimpleNamespace

import psutil

from blender_mcp import server


def _fake_sensors(monkeypatch):
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(percent=25, used=2 * 1024**3, total=8 * 1024**3))
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval: 35.0)
    monkeypatch.setattr(psutil, "cpu_count", lambda logical: 8)
    monkeypatch.setattr(psutil, "disk_usage", lambda path: SimpleNamespace(percent=60, used=60 * 1024**3, total=100 * 1024**3))


def test_workspace_disk_uses_configured_mount_not_current_directory(monkeypatch, tmp_path):
    _fake_sensors(monkeypatch)
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    monkeypatch.setenv("BLENDER_WORKSPACE", str(workspace))
    paths = []
    monkeypatch.setattr(psutil, "disk_usage", lambda path: paths.append(path) or SimpleNamespace(percent=60, used=60 * 1024**3, total=100 * 1024**3))
    monkeypatch.setattr(server.os, "getloadavg", lambda: (2.25, 1.5, 1.0))
    result = server._system_diagnostics()
    assert paths == [str(workspace.resolve())]
    assert result["load_1m"] == 2.25
    assert result["disk_used_gb"] == 60
    assert result["disk_total_gb"] == 100
    assert result["disk_percent"] == 60
    assert result["disk_scope"] == "workspace filesystem"
    assert str(workspace) not in json.dumps(result)
    assert result["cpu_logical_threads"] == 8


def test_missing_workspace_does_not_report_container_overlay(monkeypatch, tmp_path):
    _fake_sensors(monkeypatch)
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path / "unmounted"))
    monkeypatch.setattr(psutil, "disk_usage", lambda path: (_ for _ in ()).throw(AssertionError("must not use overlay")))
    result = server._system_diagnostics()
    assert result["disk_percent"] is None
    assert result["disk_used_gb"] is None
    assert result["disk_total_gb"] is None
    assert result["disk_scope"] == "workspace unavailable"


def test_optional_load_and_cpu_count_unavailable(monkeypatch, tmp_path):
    _fake_sensors(monkeypatch)
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(server.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    monkeypatch.setattr(psutil, "cpu_count", lambda logical: None)
    result = server._system_diagnostics()
    assert result["load_1m"] is None
    assert result["cpu_logical_threads"] is None
    assert result["memory_used_gb"] == 2


def test_optional_psutil_cpu_count_failure_and_unreadable_mount(monkeypatch, tmp_path):
    _fake_sensors(monkeypatch)
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(psutil, "cpu_count", lambda logical: (_ for _ in ()).throw(NotImplementedError()))
    monkeypatch.setattr(psutil, "disk_usage", lambda path: (_ for _ in ()).throw(PermissionError("private mount")))
    result = server._system_diagnostics()
    assert result["cpu_logical_threads"] is None
    assert result["disk_scope"] == "workspace unavailable"
    assert "private mount" not in json.dumps(result)


def test_default_workspace_mount_does_not_fall_back_to_overlay(monkeypatch):
    _fake_sensors(monkeypatch)
    monkeypatch.delenv("BLENDER_WORKSPACE", raising=False)
    monkeypatch.setattr(server.Path, "is_dir", lambda path: str(path) == "/workspace")
    paths = []
    monkeypatch.setattr(psutil, "disk_usage", lambda path: paths.append(path) or SimpleNamespace(percent=60, used=60 * 1024**3, total=100 * 1024**3))
    result = server._system_diagnostics()
    assert paths == ["/workspace"]
    assert result["disk_scope"] == "workspace filesystem"


def test_public_endpoint_does_not_echo_bridge_host_or_workspace(monkeypatch, tmp_path):
    _fake_sensors(monkeypatch)
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("BLENDER_BRIDGE_HOST", "private-bridge.example")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "secret-never-echo")
    async def tools():
        return [SimpleNamespace(name="sample")]
    monkeypatch.setattr(server.app, "list_tools", tools)
    monkeypatch.setattr("blender_mcp.utils.farm_workers.gpu_stats", lambda: [])
    monkeypatch.setenv("BLENDER_BRIDGE_PORT", "1")
    payload = json.loads(asyncio.run(server._diagnostics_endpoint(None)).body)
    assert payload["server"]["tool_count"] == 1
    assert payload["system"]["disk_scope"] == "workspace filesystem"
    assert payload["system"]["sampled_at"]
    assert payload["blender"]["connected"] is False
    serialized = json.dumps(payload)
    for private in ("secret-never-echo", "private-bridge.example", str(tmp_path)):
        assert private not in serialized
