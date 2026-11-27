from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from blender_mcp.utils.farm_workers import (
    MCPWorkerClient,
    WorkerConfig,
    load_worker_configs,
    validate_worker_destination,
    worker_identity,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "tag"),
    [({"online": True}, "1.4.1"), ({"image_tag": "1.4.2"}, "1.4.2"), ({"image_tag": None}, None)],
)
async def test_worker_image_tag_falls_back_to_mcp_handshake(monkeypatch, identity, tag):
    class Session:
        async def initialize(self):
            return SimpleNamespace(serverInfo=SimpleNamespace(version="1.4.1"))

        async def call_tool(self, name, arguments):
            return SimpleNamespace(isError=False, structuredContent=identity)

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield None, None, None

    @asynccontextmanager
    async def session(*args):
        yield Session()

    monkeypatch.setattr("mcp.ClientSession", session)
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", transport)
    client = MCPWorkerClient(WorkerConfig("worker", "http://10.0.0.2/mcp", "test-key"))
    result = await client.call_tool("blender_farm_worker", {"operation": "identity"})
    assert result["version"] == "1.4.1"
    assert result["image_tag"] == tag


def test_load_worker_configs_resolves_tokens_from_named_environment_variables(monkeypatch):
    monkeypatch.setenv(
        "BLENDER_FARM_WORKERS",
        json.dumps([{"id": "gpu-a", "url": "https://10.0.0.8/mcp", "api_key_env": "GPU_A_KEY", "slots": 8}]),
    )
    monkeypatch.setenv("GPU_A_KEY", "secret-value")

    configs = load_worker_configs()

    assert len(configs) == 1
    assert configs[0].worker_id == "gpu-a"
    assert configs[0].url == "https://10.0.0.8/mcp"
    assert configs[0].api_key == "secret-value"
    assert configs[0].workspace == "/workspace"
    assert not hasattr(configs[0], "slots")


def test_load_worker_configs_rejects_duplicate_ids(monkeypatch):
    monkeypatch.setenv(
        "BLENDER_FARM_WORKERS",
        json.dumps(
            [
                {"id": "gpu-a", "url": "https://10.0.0.8/mcp", "api_key_env": "GPU_A_KEY"},
                {"id": "gpu-a", "url": "https://10.0.0.9/mcp", "api_key_env": "GPU_B_KEY"},
            ]
        ),
    )
    monkeypatch.setenv("GPU_A_KEY", "a")
    monkeypatch.setenv("GPU_B_KEY", "b")

    with pytest.raises(ValueError, match="duplicate worker id"):
        load_worker_configs()


def test_load_worker_configs_rejects_insecure_public_http(monkeypatch):
    monkeypatch.setenv(
        "BLENDER_FARM_WORKERS",
        json.dumps([{"id": "gpu-a", "url": "http://8.8.8.8/mcp", "api_key_env": "GPU_A_KEY"}]),
    )
    monkeypatch.setenv("GPU_A_KEY", "secret")

    with pytest.raises(ValueError, match="RFC1918"):
        load_worker_configs()


def test_load_worker_configs_allows_private_http_and_requires_credentials(monkeypatch):
    monkeypatch.setenv(
        "BLENDER_FARM_WORKERS",
        json.dumps([{"id": "local", "url": "http://192.168.2.55:10849/mcp", "api_key_env": "LOCAL_KEY"}]),
    )
    monkeypatch.delenv("LOCAL_KEY", raising=False)

    with pytest.raises(ValueError, match="missing worker API key"):
        load_worker_configs()


def test_worker_identity_reports_id_workspace_and_capabilities(monkeypatch, tmp_path):
    config = WorkerConfig("gpu-test", "http://127.0.0.1:10849/mcp", "hidden", str(tmp_path))
    monkeypatch.setenv("BLENDER_FARM_WORKER_ID", "gpu-test")

    identity = worker_identity(config)

    assert identity["worker_id"] == "gpu-test"
    assert identity["workspace"] == str(tmp_path)
    assert "render_frame" in identity["capabilities"]
    assert "cpu_threads" in identity
    assert "gpu_devices" in identity
    assert "api_key" not in identity


@pytest.mark.parametrize("host", ["8.8.8.8", "169.254.169.254", "192.0.2.1", "0.0.0.0", "224.0.0.1", "::", "fe80::1"])  # noqa: S104
def test_worker_destination_rejects_non_private_literals(host):
    with pytest.raises(ValueError):
        validate_worker_destination(f"http://[{host}]:10849/mcp" if ":" in host else f"http://{host}:10849/mcp")


@pytest.mark.parametrize("host", ["10.1.2.3", "172.16.0.1", "192.168.1.2", "100.100.1.2", "fc00::1"])
def test_worker_destination_accepts_explicit_private_ranges(host):
    validate_worker_destination(f"http://[{host}]:10849/mcp" if ":" in host else f"http://{host}:10849/mcp")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_worker_destination_rejects_loopback(host):
    with pytest.raises(ValueError):
        validate_worker_destination(f"http://[{host}]:10849/mcp" if ":" in host else f"http://{host}:10849/mcp")


def test_worker_destination_rejects_localhost_hostname():
    with pytest.raises(ValueError, match="IP literal"):
        validate_worker_destination("http://localhost:10849/mcp")


def test_worker_destination_rejects_hostname_without_dns_resolution():
    with pytest.raises(ValueError, match="IP literal"):
        validate_worker_destination("https://worker.example/mcp")
