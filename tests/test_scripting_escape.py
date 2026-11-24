import pytest

from blender_mcp.handlers import scripting_handler


class _FakeExecutor:
    def __init__(self):
        self.code = None

    async def execute_script(self, code):
        self.code = code
        return "{}"


@pytest.mark.asyncio
async def test_triple_quotes_cannot_break_wrapper(monkeypatch):
    fake = _FakeExecutor()
    monkeypatch.setattr(scripting_handler, "_executor", fake)
    payload = "x = '''\n'''; print('pwned')\n"
    try:
        await scripting_handler.execute_script(payload, "PYTHON")
    except Exception:
        pass
    assert fake.code is not None
    compile(fake.code, "<wrapper>", "exec")
    assert repr(payload) in fake.code
