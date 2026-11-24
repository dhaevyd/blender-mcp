from __future__ import annotations

from blender_mcp import app


def test_fleet_script_path_is_passed_as_one_non_shell_argument(monkeypatch):
    calls = {}

    def fake_popen(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)
    script = r"C:\Fleet Projects\release; Write-Output injected\start.ps1"

    app._spawn_fleet_script(script, r"C:\Fleet Projects\release", {"SAFE": "1"})

    assert calls["args"] == ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", script]
    assert calls["kwargs"]["cwd"] == r"C:\Fleet Projects\release"
    assert calls["kwargs"]["env"] == {"SAFE": "1"}
    assert "shell" not in calls["kwargs"]
