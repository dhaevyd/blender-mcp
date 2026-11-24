from __future__ import annotations

import ast
import asyncio

from blender_mcp.handlers import uv_handler


class _CapturingExecutor:
    def __init__(self):
        self.script = ""

    async def execute_script(self, script: str, **_kwargs):
        self.script = script
        return "ok"


class _FailingExecutor:
    async def execute_script(self, _script: str, **_kwargs):
        raise RuntimeError("simulated executor failure")


def test_project_from_view_keeps_object_name_as_a_literal(monkeypatch):
    executor = _CapturingExecutor()
    monkeypatch.setattr(uv_handler, "_executor", executor)
    object_name = "Cube'); __import__('os').system('touch /tmp/uv-pwned'); #"

    asyncio.run(uv_handler.project_from_view(object_name=object_name))

    tree = ast.parse(executor.script)
    assert not any(
        isinstance(node, ast.Name) and node.id == "__import__"
        for node in ast.walk(tree)
    )
    assert repr(object_name) in executor.script


def test_uv_tools_return_error_dicts_when_executor_fails(monkeypatch):
    monkeypatch.setattr(uv_handler, "_executor", _FailingExecutor())

    project_result = asyncio.run(uv_handler.project_from_view(object_name="Cube"))
    info_result = asyncio.run(uv_handler.get_uv_info(object_name="Cube"))

    assert project_result["status"] == "ERROR"
    assert info_result["status"] == "ERROR"
