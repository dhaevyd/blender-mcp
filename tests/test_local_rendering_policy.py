from __future__ import annotations

from types import SimpleNamespace

from blender_mcp.utils import farm_workers
from blender_mcp.utils.local_rendering import local_rendering_disabled_reason


def test_local_render_policy_only_applies_to_master(monkeypatch):
    monkeypatch.setattr(farm_workers, "mcp_role", lambda: "stand-alone")
    assert local_rendering_disabled_reason() is None


def test_disabled_master_local_rendering_routes_to_farm(monkeypatch):
    from blender_mcp.tools import farm_tools

    store = SimpleNamespace(get_local_rendering_enabled=lambda: False)
    monkeypatch.setattr(farm_workers, "mcp_role", lambda: "master")
    monkeypatch.setattr(farm_tools, "_coordinator", lambda: SimpleNamespace(store=store))

    reason = local_rendering_disabled_reason()
    assert reason is not None
    assert "blender_farm(operation='submit')" in reason


def test_enabled_master_local_rendering_has_no_block(monkeypatch):
    from blender_mcp.tools import farm_tools

    store = SimpleNamespace(get_local_rendering_enabled=lambda: True)
    monkeypatch.setattr(farm_workers, "mcp_role", lambda: "master")
    monkeypatch.setattr(farm_tools, "_coordinator", lambda: SimpleNamespace(store=store))
    assert local_rendering_disabled_reason() is None
