from __future__ import annotations

import asyncio

from blender_mcp.handlers import validation_handler


class _StubExecutor:
    async def execute_script(self, _script: str, **_kwargs):
        return "TRIANGLES: 100"


def test_generic_avatar_validation_awaits_blender_executor(monkeypatch):
    monkeypatch.setattr(validation_handler, "_executor", _StubExecutor())

    result = asyncio.run(
        validation_handler.validate_avatar(
            target_platform="generic",
            check_materials=False,
            check_rigging=False,
            check_transforms=False,
            check_textures=False,
        )
    )

    assert result["status"] == "PASS"
    assert result["platform"] == "Generic"
    assert result["stats"]["triangles"] == 100
