"""AMD and Intel GPU telemetry from Linux DRM sysfs (``/sys/class/drm``).

The sysfs readers expose per-device VRAM or shared GTT capacity, utilization,
temperature, and power when the kernel exports them. Optional attributes degrade
to ``None``; these helpers never break diagnostics when a container cannot read
part of the host's DRM tree.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DRM_ROOT = "/sys/class/drm"

AMD_VENDOR_ID = "1002"
# PCI drivers that own a real GPU. Anything else on a DRM card (e.g. virtio-pci
# or vkms) is informational only and is reported as an adapter instead.
GPU_DRIVERS = frozenset({"nvidia", "amdgpu", "i915", "xe"})
INTEL_DRIVERS = frozenset({"i915", "xe"})

MIB = 1024 * 1024
MICROWATTS_PER_WATT = 1_000_000
MAX_PLAUSIBLE_POWER_W = 2000.0
MAX_ATTRIBUTE_BYTES = 4096

_CARD_PATTERN = re.compile(r"^card\d+$")


def _read_text(path: Path) -> str | None:
    """Return the stripped contents of a sysfs attribute, or None if unreadable."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            value = handle.read(MAX_ATTRIBUTE_BYTES).strip()
    except OSError:
        return None
    return value or None


def _read_int(path: Path) -> int | None:
    """Parse a sysfs attribute as an integer (decimal or ``0x`` hex)."""
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        return None


def _iter_cards(root: Path) -> list[Path]:
    """Return ``cardN`` entries under ``root``; connector links are ignored."""
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    cards = []
    for entry in entries:
        if not _CARD_PATTERN.match(entry.name):
            continue
        try:
            if entry.is_dir():
                cards.append(entry)
        except OSError:
            continue
    return cards


def _parse_uevent(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` uevent file into a dict; {} when unreadable."""
    text = _read_text(path)
    if text is None:
        return {}
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            fields[key.strip()] = value.strip()
    return fields


def _pci_id(uevent: dict[str, str]) -> tuple[str, str] | None:
    """Split ``PCI_ID`` into lowercase ``(vendor, device)`` ids."""
    vendor, separator, device = (uevent.get("PCI_ID") or "").partition(":")
    if not separator or not vendor or not device:
        return None
    return vendor.strip().lower(), device.strip().lower()


def _bytes_to_mib(raw: int | None) -> int | None:
    if raw is None or raw < 0:
        return None
    return raw // MIB


def _millidegrees_to_celsius(raw: int | None) -> int | None:
    if raw is None:
        return None
    return raw // 1000


def _microwatts_to_watts(raw: int | None) -> float | None:
    """Convert hwmon microwatts to watts, rejecting implausible readings."""
    if raw is None or raw < 0:
        return None
    watts = raw / MICROWATTS_PER_WATT
    if watts > MAX_PLAUSIBLE_POWER_W:
        return None
    return watts


def _hwmon_attribute(device: Path, attribute: str) -> int | None:
    """Read ``hwmon/hwmon*/<attribute>`` from the first hwmon that exports it."""
    try:
        hwmon_dirs = sorted(device.glob("hwmon/hwmon*"), key=lambda entry: entry.name)
    except OSError:
        return None
    for hwmon in hwmon_dirs:
        value = _read_int(hwmon / attribute)
        if value is not None:
            return value
    return None


def _card_memory_stats(device: Path) -> tuple[int | None, int | None, str]:
    """Read local VRAM or shared GTT capacity from a DRM device."""
    vram_total = _read_int(device / "mem_info_vram_total")
    vram_used = _read_int(device / "mem_info_vram_used")
    gtt_total = _read_int(device / "mem_info_gtt_total")
    gtt_used = _read_int(device / "mem_info_gtt_used")

    # APUs can expose a small stolen-memory region as VRAM while the renderable
    # allocation lives in system RAM. Prefer GTT when available for regions <=512 MiB.
    if vram_total and 0 < vram_total <= 512 * MIB and gtt_total and gtt_total > 0:
        return _bytes_to_mib(gtt_total), _bytes_to_mib(gtt_used), "shared"
    if not vram_total and gtt_total and gtt_total > 0:
        return _bytes_to_mib(gtt_total), _bytes_to_mib(gtt_used), "shared"
    if vram_total and vram_total > 0:
        return _bytes_to_mib(vram_total), _bytes_to_mib(vram_used), "dedicated"
    if gtt_total and gtt_total > 0:
        return _bytes_to_mib(gtt_total), _bytes_to_mib(gtt_used), "shared"
    return None, None, "shared"


def _card_name(card: Path, pci: tuple[str, str], vendor_name: str) -> str:
    product = _read_text(card / "device" / "product_name")
    if product:
        return product
    return f"{vendor_name} ({pci[0]}:{pci[1]})"


def _card_stats(card: Path, *, vendor_id: str, vendor_name: str) -> dict[str, Any] | None:
    """Build one telemetry row for a supported vendor card."""
    device = card / "device"
    pci = _pci_id(_parse_uevent(device / "uevent"))
    if pci is None or pci[0] != vendor_id:
        return None

    memory_total, memory_used, memory_type = _card_memory_stats(device)
    power_raw = _hwmon_attribute(device, "power1_average")
    if power_raw is None:
        power_raw = _hwmon_attribute(device, "power1_input")

    return {
        "name": _card_name(card, pci, vendor_name),
        "memory_total_mib": memory_total,
        "memory_used_mib": memory_used,
        "memory_type": memory_type,
        "utilization_percent": _read_int(device / "gpu_busy_percent"),
        "temperature_c": _millidegrees_to_celsius(_hwmon_attribute(device, "temp1_input")),
        "power_draw_w": _microwatts_to_watts(power_raw),
    }


def _amd_card_stats(card: Path) -> dict[str, Any] | None:
    """Build one telemetry row for ``card``; None when it is not an AMD GPU."""
    return _card_stats(card, vendor_id=AMD_VENDOR_ID, vendor_name="AMD Radeon")


def _intel_card_stats(card: Path) -> dict[str, Any] | None:
    """Build one telemetry row for an Intel GPU using i915 or xe."""
    device = card / "device"
    uevent = _parse_uevent(device / "uevent")
    driver = (uevent.get("DRIVER") or "").strip().lower()
    if driver not in INTEL_DRIVERS:
        return None
    return _card_stats(card, vendor_id="8086", vendor_name="Intel Graphics")


def amd_gpu_stats(root: str | os.PathLike[str] = DEFAULT_DRM_ROOT) -> list[dict[str, Any]]:
    """Return per-AMD-GPU stats from DRM sysfs; an empty list on any failure.

    Each row carries the same keys as the nvidia-smi path: ``name``,
    ``memory_total_mib``, ``memory_used_mib``, ``utilization_percent``,
    ``temperature_c``, ``power_draw_w``. Attributes the kernel does not export
    (or that this process cannot read) are ``None``.
    """
    devices: list[dict[str, Any]] = []
    try:
        for card in _iter_cards(Path(root)):
            stats = _amd_card_stats(card)
            if stats is not None:
                devices.append(stats)
    except Exception:
        # Telemetry is advisory; an unreadable tree must not break diagnostics.
        logger.debug("AMD sysfs telemetry read failed", exc_info=True)
        return []
    return devices


def intel_gpu_stats(root: str | os.PathLike[str] = DEFAULT_DRM_ROOT) -> list[dict[str, Any]]:
    """Return per-Intel-GPU telemetry for i915/xe devices."""
    devices: list[dict[str, Any]] = []
    try:
        for card in _iter_cards(Path(root)):
            stats = _intel_card_stats(card)
            if stats is not None:
                devices.append(stats)
    except Exception:
        logger.debug("Intel sysfs telemetry read failed", exc_info=True)
        return []
    return devices


def _nvidia_smi_available() -> bool:
    try:
        return shutil.which("nvidia-smi") is not None
    except Exception:
        # PATH probing is advisory only.
        return False


def gpu_probe(root: str | os.PathLike[str] = DEFAULT_DRM_ROOT) -> dict[str, Any]:
    """Report which GPU vendors are present, plus non-GPU DRM adapters.

    ``nvidia`` is True when a DRM card uses the ``nvidia`` driver or when
    ``nvidia-smi`` is on PATH (it can be visible without a bound DRM card in
    containers). ``adapters`` lists cards driven by anything that is not a GPU
    driver, such as ``virtio-pci``; they are informational, not GPUs.
    """
    result: dict[str, Any] = {"nvidia": False, "amd": False, "intel": False, "adapters": []}
    try:
        for card in _iter_cards(Path(root)):
            uevent = _parse_uevent(card / "device" / "uevent")
            driver = (uevent.get("DRIVER") or "").strip().lower()
            pci = _pci_id(uevent)
            if driver == "nvidia":
                result["nvidia"] = True
            if driver == "amdgpu" or (pci is not None and pci[0] == AMD_VENDOR_ID):
                result["amd"] = True
            if driver in INTEL_DRIVERS:
                result["intel"] = True
            if driver and driver not in GPU_DRIVERS:
                result["adapters"].append({"card": card.name, "driver": driver})
    except Exception:
        # The probe is advisory; never fail the diagnostics endpoint.
        logger.debug("GPU probe failed for %s", root, exc_info=True)
        return {"nvidia": False, "amd": False, "intel": False, "adapters": []}

    if not result["nvidia"] and _nvidia_smi_available():
        result["nvidia"] = True
    return result
