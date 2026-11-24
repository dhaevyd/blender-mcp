"""Host capability detection: GPU presence and virtualization type."""

from __future__ import annotations

import subprocess

import pytest

import blender_mcp.utils.host_capabilities as host


def _clear(fn):
    # A test may have replaced the cached function with a plain lambda; only clear real caches.
    clear = getattr(fn, "cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture(autouse=True)
def _clear_caches():
    _clear(host.host_gpu_present)
    _clear(host.virtualization)
    yield
    _clear(host.host_gpu_present)
    _clear(host.virtualization)


def _fake_detect_virt(monkeypatch, output: str | None):
    monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/bin/systemd-detect-virt" if output is not None else None)

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=(output or "") + "\n", stderr="")

    monkeypatch.setattr(host.subprocess, "run", fake_run)


def _tmp(monkeypatch, name, content):
    from pathlib import Path
    import tempfile

    path = Path(tempfile.mkdtemp()) / name
    if content is not None:
        path.write_text(content, encoding="utf-8")
    return path


def _tmp_dir(monkeypatch, sys_vendor):
    from pathlib import Path
    import tempfile

    root = Path(tempfile.mkdtemp())
    if sys_vendor is not None:
        (root / "sys_vendor").write_text(sys_vendor, encoding="utf-8")
    return root


def test_gpu_absent_when_probe_reports_only_virtual_adapters(monkeypatch):
    monkeypatch.setattr(
        host,
        "gpu_probe",
        lambda: {"nvidia": False, "amd": False, "intel": False, "adapters": [{"card": "card0", "driver": "virtio-pci"}]},
    )
    assert host.host_gpu_present() is False


def test_gpu_present_when_nvidia_visible(monkeypatch):
    monkeypatch.setattr(host, "gpu_probe", lambda: {"nvidia": True, "amd": False, "intel": False, "adapters": []})
    assert host.host_gpu_present() is True


def test_gpu_probe_failure_reports_no_gpu(monkeypatch):
    def boom():
        raise OSError("drm unreadable")

    monkeypatch.setattr(host, "gpu_probe", boom)
    assert host.host_gpu_present() is False


def test_container_is_reported_as_container(monkeypatch):
    _fake_detect_virt(monkeypatch, "docker")
    result = host.virtualization()
    assert result == {"kind": "docker", "virtual": False, "container": True}


def test_virtual_machine_reported_by_systemd(monkeypatch):
    _fake_detect_virt(monkeypatch, "kvm")
    assert host.virtualization() == {"kind": "kvm", "virtual": True, "container": False}


def test_bare_metal_reported_as_none(monkeypatch):
    _fake_detect_virt(monkeypatch, "none")
    assert host.virtualization() == {"kind": "none", "virtual": False, "container": False}


def test_dmi_fallback_identifies_platform_when_systemd_missing(monkeypatch):
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    monkeypatch.setattr(host, "_CPUINFO", _tmp(monkeypatch, "cpuinfo", "flags : fpu hypervisor\n"))
    monkeypatch.setattr(host, "_DMI_ROOT", _tmp_dir(monkeypatch, "QEMU"))
    assert host.virtualization() == {"kind": "kvm", "virtual": True, "container": False}


def test_unknown_when_nothing_can_be_read(monkeypatch):
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    monkeypatch.setattr(host, "_CPUINFO", _tmp(monkeypatch, "cpuinfo", None))
    monkeypatch.setattr(host, "_DMI_ROOT", _tmp_dir(monkeypatch, None))
    assert host.virtualization() == {"kind": "unknown", "virtual": None, "container": None}


def test_describe_host_summarises_gpu_and_platform(monkeypatch):
    monkeypatch.setattr(host, "host_gpu_present", lambda: False)
    monkeypatch.setattr(host, "virtualization", lambda: {"kind": "kvm", "virtual": True, "container": False})
    assert host.describe_host() == {
        "gpu_available": False,
        "virtualization": {"kind": "kvm", "virtual": True, "container": False},
    }