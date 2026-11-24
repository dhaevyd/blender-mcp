"""Detect what this machine offers: GPUs, and the virtualization it runs under.

The answers are advisory and cached for the life of the process. Detection never
raises: a probe that cannot read something reports "unknown" instead of breaking
the caller (diagnostics, render setup).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from blender_mcp.utils.gpu_sysfs import gpu_probe

logger = logging.getLogger(__name__)

_DMI_ROOT = Path("/sys/class/dmi/id")
_CPUINFO = Path("/proc/cpuinfo")

#: Substrings in DMI vendor/product strings mapped to a readable platform name.
_DMI_HINTS: tuple[tuple[str, str], ...] = (
    ("qemu", "kvm"),
    ("kvm", "kvm"),
    ("vmware", "vmware"),
    ("virtualbox", "virtualbox"),
    ("microsoft", "hyper-v"),
    ("xen", "xen"),
    ("amazon ec2", "aws"),
    ("google compute engine", "gcp"),
)

#: ``systemd-detect-virt`` results that mean "container", not "virtual machine".
_CONTAINER_KINDS = frozenset({"docker", "podman", "lxc", "lxc-libvirt", "systemd-nspawn", "containerd", "rkt"})


def _read(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value or None


@lru_cache(maxsize=1)
def host_gpu_present() -> bool:
    """True when the host exposes an NVIDIA, AMD or Intel GPU.

    Uses the same DRM and ``nvidia-smi`` probe as diagnostics. Virtual adapters
    such as ``virtio-pci`` do not count.
    """
    try:
        probe = gpu_probe()
    except Exception:
        logger.debug("GPU probe failed; assuming no GPU", exc_info=True)
        return False
    return bool(probe.get("nvidia") or probe.get("amd") or probe.get("intel"))


@lru_cache(maxsize=1)
def virtualization() -> dict[str, Any]:
    """Describe the platform the process runs on.

    ``kind`` is the platform name (for example ``kvm``, ``vmware``, ``aws``, or
    ``docker``), ``"none"`` on bare metal, or ``"unknown"``. ``virtual`` and
    ``container`` are booleans, or ``None`` when that cannot be determined.
    """
    kind: str | None = None
    exe = shutil.which("systemd-detect-virt")
    if exe:
        try:
            completed = subprocess.run(
                [exe], capture_output=True, text=True, timeout=3, check=False
            )
            kind = completed.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            kind = None

    cpu_flags = _read(_CPUINFO)
    hypervisor_flag = None
    if cpu_flags is not None:
        flag_lines = [line for line in cpu_flags.splitlines() if line.startswith("flags")]
        hypervisor_flag = any("hypervisor" in line.split() for line in flag_lines)

    if kind is None:
        vendor = (_read(_DMI_ROOT / "sys_vendor") or "").lower()
        product = (_read(_DMI_ROOT / "product_name") or "").lower()
        for needle, name in _DMI_HINTS:
            if needle in vendor or needle in product:
                kind = name
                break

    if kind is None and hypervisor_flag is None:
        return {"kind": "unknown", "virtual": None, "container": None}

    if kind in _CONTAINER_KINDS:
        return {"kind": kind, "virtual": False, "container": True}
    if kind == "none" or (kind is None and hypervisor_flag is False):
        return {"kind": "none", "virtual": False, "container": False}
    if kind is None:
        # The CPU says it is virtualized but no platform name matched.
        return {"kind": "unknown", "virtual": True, "container": False}
    return {"kind": kind, "virtual": True, "container": False}


def describe_host() -> dict[str, Any]:
    """Return the host summary used by diagnostics."""
    return {
        "gpu_available": host_gpu_present(),
        "virtualization": virtualization(),
    }