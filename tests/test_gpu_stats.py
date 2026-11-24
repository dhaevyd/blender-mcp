"""Tests for GPU stats collection via nvidia-smi."""

from __future__ import annotations

import subprocess

import pytest

from blender_mcp.utils import farm_workers


@pytest.fixture(autouse=True)
def no_intel_gpu(monkeypatch):
    monkeypatch.setattr(farm_workers, "intel_gpu_stats", lambda: [])

SAMPLE_GPU = {
    "name": "NVIDIA GeForce RTX 3060",
    "memory_total_mib": 12288,
    "memory_used_mib": 2019,
    "utilization_percent": 0,
    "temperature_c": 42,
    "power_draw_w": 148.5,
}


def test_gpu_stats_parses_sample_csv(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="NVIDIA GeForce RTX 3060, 12288, 2019, 0, 42, 148.5\n",
            stderr="",
        )

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)

    assert farm_workers.gpu_stats() == [SAMPLE_GPU]


def test_gpu_stats_strips_watt_suffix(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="Tesla T4, 15360, 1234, 10, 55, 71.0 W\n",
            stderr="",
        )

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)

    assert farm_workers.gpu_stats() == [
        {
            "name": "Tesla T4",
            "memory_total_mib": 15360,
            "memory_used_mib": 1234,
            "utilization_percent": 10,
            "temperature_c": 55,
            "power_draw_w": 71.0,
        }
    ]


def test_gpu_stats_power_none_when_unavailable(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="Tesla T4, 15360, 1234, 10, 55, [N/A]\n",
            stderr="",
        )

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)

    assert farm_workers.gpu_stats() == [
        {
            "name": "Tesla T4",
            "memory_total_mib": 15360,
            "memory_used_mib": 1234,
            "utilization_percent": 10,
            "temperature_c": 55,
            "power_draw_w": None,
        }
    ]


def test_gpu_stats_power_none_when_malformed(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="Tesla T4, 15360, 1234, 10, 55, not-a-number\n",
            stderr="",
        )

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)

    assert farm_workers.gpu_stats() == [
        {
            "name": "Tesla T4",
            "memory_total_mib": 15360,
            "memory_used_mib": 1234,
            "utilization_percent": 10,
            "temperature_c": 55,
            "power_draw_w": None,
        }
    ]


def test_gpu_stats_returns_empty_on_missing_binary(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)

    assert farm_workers.gpu_stats() == []


def test_gpu_stats_returns_empty_on_nonzero_returncode(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="no devices")

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)

    assert farm_workers.gpu_stats() == []


def test_gpu_stats_skips_malformed_rows(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                "NVIDIA GeForce RTX 3060, 12288, 2019, 0, 42, 148.5\n"
                "broken row\n"
                "Tesla T4, 15360, 1234, 10, not-a-number, 70\n"
                "\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)

    assert farm_workers.gpu_stats() == [SAMPLE_GPU]


def test_gpu_stats_returns_empty_on_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)

    monkeypatch.setattr(farm_workers.subprocess, "run", fake_run)

    assert farm_workers.gpu_stats() == []
