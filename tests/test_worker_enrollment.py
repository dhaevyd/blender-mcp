from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from starlette.requests import Request

from blender_mcp.utils.farm_state import FarmJobStore
from blender_mcp.utils.studio_api import enroll_worker, farm_gpus_get, workers_get


def _request(path, method, body=b"", params=None):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
        "http_version": "1.1",
        "path_params": params or {},
    }
    return Request(scope, receive)


def test_farm_gpu_telemetry_aggregates_each_worker_gpu(monkeypatch):
    from blender_mcp.tools import farm_tools

    gpu = {
        "index": 0,
        "backend": "NVIDIA",
        "name": "RTX 4090",
        "memory_total_mib": 24576,
        "memory_used_mib": 1024,
        "memory_free_mib": 23552,
    }

    class FakeFarm:
        workers = {"render-vm": object()}

        async def _read_worker_identity(self, worker_id):
            assert worker_id == "render-vm"
            return {"online": True, "image_tag": "1.4.1", "version": "0.11.1", "gpu_stats": [gpu]}

    monkeypatch.setattr(farm_tools, "_FARM", FakeFarm())
    response = asyncio.run(farm_gpus_get(_request("/api/v1/farm/gpus", "GET")))
    payload = json.loads(response.body)
    assert payload["workers"] == [{"worker_id": "render-vm", "online": True, "image_tag": "1.4.1", "gpus": [gpu]}]
    assert response.headers["cache-control"] == "private, no-store"


def test_dashboard_worker_encrypted_and_persisted(tmp_path):
    store = FarmJobStore(tmp_path / "farm.sqlite3")
    key = "worker-secret-value"
    store.save_managed_worker(
        {
            "worker_id": "ui-test",
            "name": "Test",
            "url": "http://10.0.0.3:10849/mcp",
            "workspace": "/workspace",
            "slots": 2,
            "enabled": False,
        },
        key,
    )
    reopened = FarmJobStore(tmp_path / "farm.sqlite3")
    assert reopened.get_managed_worker_secret("ui-test") == key
    with sqlite3.connect(tmp_path / "farm.sqlite3") as db:
        assert key.encode() not in db.execute("SELECT encrypted_key FROM managed_workers").fetchone()[0]
    assert (tmp_path / "farm.key").stat().st_mode & 0o777 == 0o600


def test_enroll_returns_one_time_key_and_compose(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    from blender_mcp.tools import farm_tools

    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)

    async def fake_version():
        return "1.2.3"

    monkeypatch.setattr("blender_mcp.utils.studio_api.app_version", fake_version, raising=False)
    response = asyncio.run(
        enroll_worker(
            _request(
                "/api/v1/workers/enroll",
                "POST",
                json.dumps({"url": "10.0.0.3", "name": "GPU Render 2", "mode": "compose", "gpu_vendor": "nvidia"}).encode(),
            )
        )
    )
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert len(payload["api_key"]) >= 40
    assert "BLENDER_MCP_WORKER_KEY=" + payload["api_key"] in payload["worker_env"]
    assert "ghcr.io/dhaevyd/blender-mcp:" in payload["compose_yaml"]
    assert payload["compose_yaml"].startswith("name: gpu-render-2\n")
    assert payload["worker"]["worker_id"] == "gpu-render-2"
    import yaml

    compose = yaml.safe_load(payload["compose_yaml"])
    service = compose["services"]["blender-worker"]
    assert service["container_name"] == "blender_mcp-worker"
    assert service["gpus"] == "all"
    assert service["environment"]["BLENDER_FARM_WORKER_ID"] == "gpu-render-2"
    assert service["volumes"][0] == "./gpu-render-2-workspace:/workspace"
    assert "/usr/share/glvnd/egl_vendor.d/10_nvidia.json:/usr/share/glvnd/egl_vendor.d/10_nvidia.json:ro" in service["volumes"]
    assert "volumes" not in compose
    assert compose["networks"]["proxy"] == {"name": "proxy", "external": True}
    assert response.headers["cache-control"] == "private, no-store"
    assert asyncio.run(workers_get(_request("/api/v1/workers", "GET"))).body.find(payload["api_key"].encode()) == -1


@pytest.mark.parametrize("gpu_vendor", ["amd", "intel"])
def test_enroll_generates_vendor_specific_compose(monkeypatch, tmp_path, gpu_vendor):
    import yaml

    from blender_mcp.tools import farm_tools

    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    response = asyncio.run(
        enroll_worker(
            _request(
                "/api/v1/workers/enroll",
                "POST",
                json.dumps({"url": "10.0.0.4", "name": "GPU Node", "mode": "compose", "gpu_vendor": gpu_vendor}).encode(),
            )
        )
    )

    assert response.status_code == 200
    service = yaml.safe_load(json.loads(response.body)["compose_yaml"])["services"]["blender-worker"]
    assert "/dev/dri:/dev/dri" in service["devices"]
    assert "gpus" not in service
    if gpu_vendor == "amd":
        assert "/dev/kfd:/dev/kfd" in service["devices"]
        assert "/opt/rocm:/opt/rocm:ro" in service["volumes"]
        assert service["security_opt"] == ["seccomp=unconfined"]
    else:
        assert service["devices"] == ["/dev/dri:/dev/dri"]
    assert service["volumes"][0] == "./gpu-node-workspace:/workspace"
    assert "volumes" not in yaml.safe_load(json.loads(response.body)["compose_yaml"])


def test_enroll_auto_generates_single_profile_compose_and_host_detector(monkeypatch, tmp_path):
    import yaml

    from blender_mcp.tools import farm_tools

    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    response = asyncio.run(
        enroll_worker(
            _request(
                "/api/v1/workers/enroll",
                "POST",
                json.dumps({"url": "10.0.0.6", "name": "Auto GPU", "mode": "compose", "gpu_vendor": "auto"}).encode(),
            )
        )
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert "compose_variants" not in payload
    compose = yaml.safe_load(payload["compose_yaml"])
    assert compose["name"] == "auto-gpu"
    services = compose["services"]
    assert list(services) == ["blender-worker-nvidia", "blender-worker-amd", "blender-worker-intel"]
    for vendor, service in services.items():
        assert service["profiles"] == [vendor.split("-")[-1]]
        assert service["container_name"] == "blender_mcp-worker"
        assert service["env_file"] == [".env"]
        assert service["ports"] == ["10849:10849"]
        assert service["networks"] == ["proxy"]
        assert service["volumes"][0] == "./auto-gpu-workspace:/workspace"
        assert service["environment"]["BLENDER_FARM_WORKER_ID"] == "auto-gpu"
    assert services["blender-worker-nvidia"]["gpus"] == "all"
    assert "/dev/kfd:/dev/kfd" in services["blender-worker-amd"]["devices"]
    assert services["blender-worker-intel"]["devices"] == ["/dev/dri:/dev/dri"]
    assert compose["networks"]["proxy"] == {"name": "proxy", "external": True}
    assert "volumes" not in compose
    setup_script = payload["setup_script"]
    assert "nvidia-smi" in setup_script
    assert "has_pci_vendor 1002" in setup_script
    assert "has_pci_vendor 8086" in setup_script
    assert '--profile "$vendor" up -d --remove-orphans' in setup_script
    assert "compose.yaml" in setup_script
    assert "compose.$vendor.yaml" not in setup_script


def test_enroll_rejects_duplicate_worker_label(monkeypatch, tmp_path):
    from blender_mcp.tools import farm_tools

    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    body = json.dumps({"url": "10.0.0.5", "name": "Render VM 2", "mode": "existing"}).encode()

    first = asyncio.run(enroll_worker(_request("/api/v1/workers/enroll", "POST", body)))
    second = asyncio.run(enroll_worker(_request("/api/v1/workers/enroll", "POST", body)))

    assert first.status_code == 200
    assert json.loads(first.body)["worker"]["worker_id"] == "render-vm-2"
    assert second.status_code == 409


def test_enroll_rejects_public_or_dns_destinations(monkeypatch, tmp_path):
    from blender_mcp.tools import farm_tools

    monkeypatch.setattr(farm_tools, "_FARM", farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", []))
    for target in ("8.8.8.8", "worker.example"):
        response = asyncio.run(
            enroll_worker(
                _request("/api/v1/workers/enroll", "POST", json.dumps({"url": target, "mode": "existing"}).encode())
            )
        )
        assert response.status_code == 400


def test_worker_store_updates_and_deletes(tmp_path):
    store = FarmJobStore(tmp_path / "farm.sqlite3")
    store.save_managed_worker(
        {
            "worker_id": "w1",
            "name": "A",
            "url": "http://10.0.0.1/mcp",
            "workspace": "/workspace",
            "slots": 1,
            "enabled": False,
        },
        "secret",
    )
    store.update_managed_worker("w1", {"name": "B", "slots": 3})
    assert store.get_managed_worker("w1")["name"] == "B"
    assert store.get_managed_worker("w1")["slots"] == 3
    assert store.delete_managed_worker("w1") is True
    assert store.list_managed_workers() == []


def test_worker_list_distinguishes_pending_from_verified_disabled(monkeypatch, tmp_path):
    from blender_mcp.tools import farm_tools

    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    farm.store.save_managed_worker({"worker_id": "w1", "name": "A", "url": "http://10.0.0.1/mcp", "workspace": "/workspace", "slots": 1, "enabled": False}, "secret")
    response = asyncio.run(workers_get(_request("/api/v1/workers", "GET")))
    assert json.loads(response.body)["workers"][0]["status"] == "pending"


def test_store_does_not_change_parent_directory_permissions(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    FarmJobStore(shared / "farm.sqlite3")
    assert shared.stat().st_mode & 0o777 == 0o755


def test_enroll_compose_references_env_file_and_worker_url_is_consistent(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_WORKSPACE", str(tmp_path))
    from blender_mcp.tools import farm_tools
    monkeypatch.setattr(farm_tools, "_FARM", farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", []))
    response = asyncio.run(enroll_worker(_request("/api/v1/workers/enroll", "POST", json.dumps({"url": "https://[fd00::2]:9443/mcp", "mode": "compose"}).encode())))
    assert response.status_code == 400  # Compose cannot proxy an unrelated HTTPS endpoint.


def test_store_verified_state_is_persisted(tmp_path):
    store = FarmJobStore(tmp_path / "farm.sqlite3")
    store.save_managed_worker({"worker_id": "w1", "name": "A", "url": "http://10.0.0.1/mcp", "workspace": "/workspace", "slots": 1, "enabled": False}, "secret")
    assert store.get_managed_worker("w1")["verified"] == 0


def test_successful_test_registers_runtime_and_marks_verified(monkeypatch, tmp_path):
    from blender_mcp.tools import farm_tools
    from blender_mcp.utils import studio_api

    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    farm.store.save_managed_worker({"worker_id": "w", "name": "w", "url": "http://10.0.0.1/mcp", "workspace": "/workspace", "slots": 1, "enabled": True}, "secret")

    async def good_list(self):
        return [{"name": "blender_farm_worker"}]

    async def good_call(self, name, arguments):
        return {"online": True, "worker_id": "w"}

    monkeypatch.setattr(studio_api, "MCPWorkerClient", lambda config: type("Client", (), {"list_tools": good_list, "call_tool": good_call})())
    response = asyncio.run(studio_api.worker_test(_request("/api/v1/workers/w/test", "POST", params={"worker_id": "w"})))
    assert response.status_code == 200
    assert farm.store.get_managed_worker("w")["verified"] == 1
    assert farm.workers["w"].worker_id == "w" and "w" in farm._clients


def test_failed_test_removes_runtime_and_marks_pending(monkeypatch, tmp_path):
    from blender_mcp.tools import farm_tools
    from blender_mcp.utils import studio_api
    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    farm.store.save_managed_worker({"worker_id": "w", "name": "w", "url": "http://10.0.0.1/mcp", "workspace": "/workspace", "slots": 1, "enabled": True}, "secret")
    farm.workers["w"] = __import__("blender_mcp.utils.farm_workers", fromlist=["WorkerConfig"]).WorkerConfig("w", "http://10.0.0.1/mcp", "secret")
    farm._clients["w"] = object()
    async def bad_client(config):
        raise RuntimeError("secret")
    monkeypatch.setattr(studio_api, "MCPWorkerClient", lambda config: type("Client", (), {"list_tools": bad_client})())
    response = asyncio.run(studio_api.worker_test(_request("/api/v1/workers/w/test", "POST", params={"worker_id": "w"})))
    assert response.status_code == 502
    assert "w" not in farm.workers and "w" not in farm._clients
    assert farm.store.get_managed_worker("w")["verified"] == 0


def test_patch_rejects_enabling_unverified_and_invalid_workspace(monkeypatch, tmp_path):
    from blender_mcp.tools import farm_tools
    from blender_mcp.utils.studio_api import worker_patch
    farm = farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", [])
    monkeypatch.setattr(farm_tools, "_FARM", farm)
    farm.store.save_managed_worker({"worker_id": "w", "name": "w", "url": "http://10.0.0.1/mcp", "workspace": "/workspace", "slots": 1, "enabled": False}, "secret")
    response = asyncio.run(worker_patch(_request("/api/v1/workers/w", "PATCH", json.dumps({"enabled": True}).encode(), {"worker_id": "w"})))
    assert response.status_code == 409
    response = asyncio.run(worker_patch(_request("/api/v1/workers/w", "PATCH", json.dumps({"workspace": "bad\npath"}).encode(), {"worker_id": "w"})))
    assert response.status_code == 400


def test_enroll_normalizes_ipv6_with_port(monkeypatch, tmp_path):
    from blender_mcp.tools import farm_tools
    monkeypatch.setattr(farm_tools, "_FARM", farm_tools.BlenderFarmCoordinator(tmp_path, tmp_path / "farm.sqlite3", []))
    response = asyncio.run(enroll_worker(_request("/api/v1/workers/enroll", "POST", json.dumps({"url": "fd00::2:9443", "mode": "existing"}).encode())))
    assert response.status_code == 200
    assert json.loads(response.body)["worker"]["url"] == "http://[fd00::2]:9443/mcp"
