"""Fake-sysfs tests for AMD GPU telemetry and vendor probing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from blender_mcp.utils import farm_workers, gpu_sysfs
from blender_mcp.utils.gpu_sysfs import amd_gpu_stats, gpu_probe, intel_gpu_stats

GIB = 1024**3


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _make_card(
    root: Path,
    name: str = "card0",
    *,
    vendor: str = "1002",
    device: str = "73df",
    driver: str = "amdgpu",
    product_name: str | None = None,
    vram_total: int | None = None,
    vram_used: int | None = None,
    busy: int | None = None,
    temp_millidegrees: int | None = None,
    power_microwatts: int | None = None,
    power_attribute: str = "power1_average",
    gtt_total: int | None = None,
    gtt_used: int | None = None,
) -> Path:
    """Create a minimal ``/sys/class/drm/cardN`` tree for one device."""
    device_dir = root / name / "device"
    _write(device_dir / "uevent", f"DRIVER={driver}\nPCI_CLASS=30000\nPCI_ID={vendor}:{device}\n")
    if product_name is not None:
        _write(device_dir / "product_name", product_name)
    if vram_total is not None:
        _write(device_dir / "mem_info_vram_total", f"{vram_total}\n")
    if vram_used is not None:
        _write(device_dir / "mem_info_vram_used", f"{vram_used}\n")
    if gtt_total is not None:
        _write(device_dir / "mem_info_gtt_total", f"{gtt_total}\n")
    if gtt_used is not None:
        _write(device_dir / "mem_info_gtt_used", f"{gtt_used}\n")
    if busy is not None:
        _write(device_dir / "gpu_busy_percent", f"{busy}\n")
    if temp_millidegrees is not None:
        _write(device_dir / "hwmon" / "hwmon3" / "temp1_input", f"{temp_millidegrees}\n")
    if power_microwatts is not None:
        _write(device_dir / "hwmon" / "hwmon3" / power_attribute, f"{power_microwatts}\n")
    return root / name


def _no_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_sysfs, "_nvidia_smi_available", lambda: False)


def test_amd_card_with_all_fields_present(tmp_path: Path) -> None:
    root = tmp_path / "drm"
    _make_card(
        root,
        product_name="Navi 31 [Radeon RX 7900 XTX]\n",
        vram_total=24 * GIB,
        vram_used=3 * GIB,
        busy=37,
        temp_millidegrees=58000,
        power_microwatts=214_000_000,
    )

    assert amd_gpu_stats(root) == [
        {
            "name": "Navi 31 [Radeon RX 7900 XTX]",
            "memory_total_mib": 24 * 1024,
            "memory_used_mib": 3 * 1024,
            "memory_type": "dedicated",
            "utilization_percent": 37,
            "temperature_c": 58,
            "power_draw_w": pytest.approx(214.0),
        }
    ]


def test_amd_card_without_hwmon_leaves_temperature_and_power_none(tmp_path: Path) -> None:
    root = tmp_path / "drm"
    _make_card(root, vram_total=8 * GIB, vram_used=GIB, busy=5)

    (card,) = amd_gpu_stats(root)

    assert card["name"] == "AMD Radeon (1002:73df)"
    assert card["memory_total_mib"] == 8 * 1024
    assert card["memory_used_mib"] == 1024
    assert card["memory_type"] == "dedicated"
    assert card["utilization_percent"] == 5
    assert card["temperature_c"] is None
    assert card["power_draw_w"] is None


def test_amd_apu_uses_shared_gtt_when_only_stolen_vram_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "drm"
    _make_card(root, vram_total=256 * 1024**2, vram_used=64 * 1024**2, gtt_total=16 * GIB, gtt_used=GIB)

    (card,) = amd_gpu_stats(root)

    assert card["memory_type"] == "shared"
    assert card["memory_total_mib"] == 16 * 1024
    assert card["memory_used_mib"] == 1024


def test_intel_integrated_gpu_reports_shared_gtt(tmp_path: Path) -> None:
    root = tmp_path / "drm"
    _make_card(
        root,
        vendor="8086",
        device="9a49",
        driver="i915",
        product_name="Intel Iris Xe",
        gtt_total=8 * GIB,
        gtt_used=512 * 1024**2,
    )

    (card,) = intel_gpu_stats(root)

    assert card["name"] == "Intel Iris Xe"
    assert card["memory_type"] == "shared"
    assert card["memory_total_mib"] == 8 * 1024
    assert card["memory_used_mib"] == 512


def test_shared_gpu_inventory_caps_free_gtt_by_host_available_ram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        farm_workers.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError())
    )
    monkeypatch.setattr(
        farm_workers,
        "amd_gpu_stats",
        lambda: [],
    )
    monkeypatch.setattr(
        farm_workers,
        "intel_gpu_stats",
        lambda: [
            {
                "name": "Intel Iris Xe",
                "memory_total_mib": 12 * 1024,
                "memory_used_mib": 2 * 1024,
                "memory_type": "shared",
            }
        ],
    )
    monkeypatch.setattr(
        farm_workers.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=16 * GIB, available=6 * GIB),
    )

    (gpu,) = farm_workers.gpu_inventory_stats()

    assert gpu["backend"] == "INTEL"
    assert gpu["index"] == 0
    assert gpu["memory_total_mib"] == 12 * 1024
    assert gpu["memory_free_mib"] == 6 * 1024


def test_small_amd_vram_uses_host_memory_fallback_when_gtt_is_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        farm_workers.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError())
    )
    monkeypatch.setattr(
        farm_workers,
        "amd_gpu_stats",
        lambda: [{"name": "AMD APU", "memory_total_mib": 256, "memory_used_mib": 64, "memory_type": "dedicated"}],
    )
    monkeypatch.setattr(farm_workers, "intel_gpu_stats", lambda: [])
    monkeypatch.setattr(
        farm_workers.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=16 * GIB, available=6 * GIB),
    )

    (gpu,) = farm_workers.gpu_inventory_stats()

    assert gpu["memory_type"] == "shared"
    assert gpu["memory_total_mib"] == 8192
    assert gpu["memory_free_mib"] == 6 * 1024


def test_amd_card_with_only_uevent_reports_name_and_nones(tmp_path: Path) -> None:
    root = tmp_path / "drm"
    _make_card(root, vendor="1002", device="15BF")

    assert amd_gpu_stats(root) == [
        {
            "name": "AMD Radeon (1002:15bf)",
            "memory_total_mib": None,
            "memory_used_mib": None,
            "memory_type": "shared",
            "utilization_percent": None,
            "temperature_c": None,
            "power_draw_w": None,
        }
    ]


def test_power_falls_back_to_power1_input_and_rejects_implausible_values(tmp_path: Path) -> None:
    root = tmp_path / "drm"
    _make_card(root, "card0", power_microwatts=95_000_000, power_attribute="power1_input")
    _make_card(root, "card1", device="744c", power_microwatts=3_000_000_000)

    first, second = amd_gpu_stats(root)

    assert first["power_draw_w"] == pytest.approx(95.0)
    assert second["power_draw_w"] is None


def test_amd_reader_ignores_nvidia_card(tmp_path: Path) -> None:
    root = tmp_path / "drm"
    _make_card(root, vendor="10de", device="2684", driver="nvidia", vram_total=8 * GIB)

    assert amd_gpu_stats(root) == []


def test_connector_directories_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "drm"
    _make_card(root)
    connector = root / "card0-DP-1" / "device"
    _write(connector / "uevent", "DRIVER=amdgpu\nPCI_ID=1002:73df\n")

    assert len(amd_gpu_stats(root)) == 1


def test_virtio_adapter_reported_without_being_a_gpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "drm"
    _make_card(root, vendor="1af4", device="1050", driver="virtio-pci", product_name="virtio-gpu\n")
    _make_card(root, "card1", vram_total=8 * GIB)
    _no_nvidia_smi(monkeypatch)

    assert amd_gpu_stats(root)[0]["memory_total_mib"] == 8 * 1024

    probe = gpu_probe(root)
    assert probe["amd"] is True
    assert probe["nvidia"] is False
    assert probe["intel"] is False
    assert probe["adapters"] == [{"card": "card0", "driver": "virtio-pci"}]


def test_probe_detects_nvidia_and_intel_drivers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "drm"
    _make_card(root, "card0", vendor="10de", device="2684", driver="nvidia")
    _make_card(root, "card1", vendor="8086", device="56a0", driver="i915")
    _no_nvidia_smi(monkeypatch)

    probe = gpu_probe(root)

    assert probe["nvidia"] is True
    assert probe["intel"] is True
    assert probe["amd"] is False
    assert probe["adapters"] == []


def test_probe_reports_nvidia_from_nvidia_smi_without_drm_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "drm"
    _make_card(root)
    monkeypatch.setattr(gpu_sysfs, "_nvidia_smi_available", lambda: True)

    probe = gpu_probe(root)

    assert probe["nvidia"] is True
    assert probe["amd"] is True


def test_missing_root_returns_empty_stats_and_all_false_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "no-such-drm"
    _no_nvidia_smi(monkeypatch)

    assert amd_gpu_stats(missing) == []
    assert gpu_probe(missing) == {"nvidia": False, "amd": False, "intel": False, "adapters": []}


def test_reader_never_raises_on_unreadable_attributes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "drm"
    device_dir = _make_card(root) / "device"
    _write(device_dir / "gpu_busy_percent", "not-a-number\n")
    _write(device_dir / "mem_info_vram_total", "0x10000000\n")

    (card,) = amd_gpu_stats(root)

    assert card["utilization_percent"] is None
    assert card["memory_total_mib"] == 256


def test_gpu_stats_lists_nvidia_rows_before_amd_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="NVIDIA GeForce RTX 4090, 24564, 512, 12, 41, 88.5\n",
            stderr="",
        )

    amd_row = {
        "name": "AMD Radeon (1002:73df)",
        "memory_total_mib": 8192,
        "memory_used_mib": 1024,
        "utilization_percent": 5,
        "temperature_c": 40,
        "power_draw_w": 30.0,
    }
    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)
    monkeypatch.setattr(farm_workers, "amd_gpu_stats", lambda: [dict(amd_row)])
    monkeypatch.setattr(farm_workers, "intel_gpu_stats", lambda: [])

    stats = farm_workers.gpu_stats()

    assert [row["name"] for row in stats] == ["NVIDIA GeForce RTX 4090", "AMD Radeon (1002:73df)"]
    assert stats[0]["power_draw_w"] == pytest.approx(88.5)
    assert stats[1] == amd_row


def test_gpu_stats_returns_amd_rows_when_nvidia_smi_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_nvidia_smi(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    amd_row = {
        "name": "AMD Radeon (1002:73df)",
        "memory_total_mib": 8192,
        "memory_used_mib": None,
        "utilization_percent": 0,
        "temperature_c": 35,
        "power_draw_w": None,
    }
    monkeypatch.setattr(farm_workers.subprocess, "run", missing_nvidia_smi)
    monkeypatch.setattr(farm_workers, "amd_gpu_stats", lambda: [dict(amd_row)])
    monkeypatch.setattr(farm_workers, "intel_gpu_stats", lambda: [])

    assert farm_workers.gpu_stats() == [amd_row]


def test_gpu_stats_survives_amd_backend_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    def broken_amd() -> list[dict]:
        raise RuntimeError("sysfs exploded")

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)
    monkeypatch.setattr(farm_workers, "amd_gpu_stats", broken_amd)
    monkeypatch.setattr(farm_workers, "intel_gpu_stats", lambda: [])

    assert farm_workers.gpu_stats() == []
