"""Unit tests for mac_memory_pressure band + remediation decisions."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "mac" / "mac_memory_pressure.py"
)


def _load():
    name = "og_mac_memory_pressure"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mp():
    return _load()


def test_classify_ok_warn_critical(mp):
    ok = mp.MemSample(
        page_size=16384,
        compressor_pages=int(10 * (1024**3) / 16384),
        purgeable_pages=0,
        swap_used_mb=4 * 1024,
        swap_total_mb=20 * 1024,
    )
    assert mp.classify_band(ok) == "ok"

    warn = mp.MemSample(
        page_size=16384,
        compressor_pages=int(22 * (1024**3) / 16384),
        purgeable_pages=0,
        swap_used_mb=4 * 1024,
        swap_total_mb=20 * 1024,
    )
    assert mp.classify_band(warn) == "warn"

    crit_swap = mp.MemSample(
        page_size=16384,
        compressor_pages=int(10 * (1024**3) / 16384),
        purgeable_pages=0,
        swap_used_mb=17 * 1024,
        swap_total_mb=22 * 1024,
    )
    assert mp.classify_band(crit_swap) == "critical"


def test_parse_vm_stat_and_swap(mp):
    vm = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                  100.
Pages purgeable:              50.
Pages occupied by compressor: 2000000.
"""
    parsed = mp.parse_vm_stat(vm)
    assert parsed["page_size"] == 16384
    assert parsed["pages_occupied_by_compressor"] == 2000000
    assert parsed["pages_purgeable"] == 50

    used, total = mp.parse_swapusage(
        "vm.swapusage: total = 22528.00M  used = 21250.88M  free = 1277.12M"
    )
    assert abs(used - 21250.88) < 0.01
    assert abs(total - 22528.0) < 0.01


def test_parse_etime(mp):
    assert mp.parse_etime_to_seconds("01:02") == 62
    assert mp.parse_etime_to_seconds("01:02:03") == 3723
    assert mp.parse_etime_to_seconds("2-01:00:00") == 2 * 86400 + 3600


def test_no_remediation_before_threshold(mp):
    sample = mp.MemSample(
        page_size=16384,
        compressor_pages=int(30 * (1024**3) / 16384),
        purgeable_pages=0,
        swap_used_mb=20 * 1024,
        swap_total_mb=22 * 1024,
    )
    procs = [
        mp.ProcInfo(
            pid=1,
            rss_kb=2_000_000,
            etime_s=20 * 3600,
            command="next-server (v16)",
        )
    ]
    actions, notes = mp.decide_remediations(
        band="critical",
        stress_streak=1,
        fail_threshold=2,
        sample=sample,
        procs=procs,
        now_mono=100.0,
        last_remediation_mono={},
        cooldown_s=1800,
    )
    assert actions == []
    assert any("streak:1<2" in n for n in notes)


def test_warn_triggers_purge_and_gated_kicks(mp):
    sample = mp.MemSample(
        page_size=16384,
        compressor_pages=int(22 * (1024**3) / 16384),
        purgeable_pages=10,
        swap_used_mb=9 * 1024,
        swap_total_mb=22 * 1024,
    )
    procs = [
        mp.ProcInfo(
            pid=10,
            rss_kb=2_000_000,  # ~1953 MB
            etime_s=100,
            command="/path/next-server (v16.3.4)",
        ),
        mp.ProcInfo(
            pid=11,
            rss_kb=600_000,  # ~586 MB
            etime_s=50,
            command="/Users/x/.9router/combo-helper/combo-helper -listen",
        ),
    ]
    actions, _notes = mp.decide_remediations(
        band="warn",
        stress_streak=2,
        fail_threshold=2,
        sample=sample,
        procs=procs,
        now_mono=100.0,
        last_remediation_mono={},
        cooldown_s=1800,
    )
    assert "purge" in actions
    assert "kick:9router" in actions
    assert "kick:helper" in actions
    assert "cache:homebrew" not in actions


def test_critical_adds_cache_trim(mp):
    sample = mp.MemSample(
        page_size=16384,
        compressor_pages=int(30 * (1024**3) / 16384),
        purgeable_pages=0,
        swap_used_mb=18 * 1024,
        swap_total_mb=22 * 1024,
    )
    actions, _ = mp.decide_remediations(
        band="critical",
        stress_streak=2,
        fail_threshold=2,
        sample=sample,
        procs=[],
        now_mono=50.0,
        last_remediation_mono={},
        cooldown_s=1800,
    )
    assert "purge" in actions
    assert "cache:homebrew" in actions


def test_cooldown_suppresses_classes(mp):
    sample = mp.MemSample(
        page_size=16384,
        compressor_pages=int(30 * (1024**3) / 16384),
        purgeable_pages=0,
        swap_used_mb=18 * 1024,
        swap_total_mb=22 * 1024,
    )
    actions, notes = mp.decide_remediations(
        band="critical",
        stress_streak=5,
        fail_threshold=2,
        sample=sample,
        procs=[],
        now_mono=1000.0,
        last_remediation_mono={
            "purge": 100.0,
            "kick": 100.0,
            "cache": 100.0,
        },
        cooldown_s=1800,
    )
    assert actions == []
    assert "purge:cooldown" in notes
    assert "kick:cooldown" in notes
    assert "cache:cooldown" in notes


def test_trim_homebrew_cache_age_gate(mp, tmp_path):
    downloads = tmp_path / "Library" / "Caches" / "Homebrew" / "downloads"
    downloads.mkdir(parents=True)
    old = downloads / "old.tgz"
    new = downloads / "new.tgz"
    old.write_bytes(b"x" * 100)
    new.write_bytes(b"y" * 50)
    now = 1_700_000_000.0
    # old = 20 days ago, new = 1 day ago
    import os

    os.utime(old, (now - 20 * 86400, now - 20 * 86400))
    os.utime(new, (now - 86400, now - 86400))
    msg = mp.trim_homebrew_cache(tmp_path, dry_run=False, now=now)
    assert "removed=1" in msg
    assert not old.exists()
    assert new.exists()


def test_gate_latch_requires_persist_window(mp):
    latch = mp.GateLatchState()
    latch, event = mp.update_gate_latch(
        band="critical",
        applied=["purge"],
        notes=[],
        now_wall=1_000.0,
        persist_s=600.0,
        latch=latch,
    )
    assert event is None
    assert latch.purge_anchor_wall == 1_000.0
    assert latch.latched is False

    latch, event = mp.update_gate_latch(
        band="critical",
        applied=[],
        notes=["purge:cooldown"],
        now_wall=1_500.0,
        persist_s=600.0,
        latch=latch,
    )
    assert event is None
    assert latch.latched is False

    latch, event = mp.update_gate_latch(
        band="critical",
        applied=[],
        notes=["purge:cooldown"],
        now_wall=1_600.0,
        persist_s=600.0,
        latch=latch,
    )
    assert event == "latch"
    assert latch.latched is True
    assert latch.notify_sent is True
    assert latch.latched_at_wall == 1_600.0


def test_gate_clear_only_on_ok(mp):
    latch = mp.GateLatchState(
        purge_anchor_wall=1_000.0,
        latched=True,
        notify_sent=True,
        latched_at_wall=1_600.0,
    )
    latch, event = mp.update_gate_latch(
        band="warn",
        applied=[],
        notes=[],
        now_wall=2_000.0,
        persist_s=600.0,
        latch=latch,
    )
    assert event is None
    assert latch.latched is True

    latch, event = mp.update_gate_latch(
        band="ok",
        applied=[],
        notes=[],
        now_wall=2_100.0,
        persist_s=600.0,
        latch=latch,
    )
    assert event == "clear"
    assert latch.latched is False
    assert latch.purge_anchor_wall is None
    assert latch.notify_sent is False


def test_gate_anchor_not_reset_while_critical(mp):
    latch = mp.GateLatchState()
    latch, _ = mp.update_gate_latch(
        band="critical",
        applied=["purge"],
        notes=[],
        now_wall=1_000.0,
        persist_s=600.0,
        latch=latch,
    )
    latch, _ = mp.update_gate_latch(
        band="critical",
        applied=["purge"],
        notes=[],
        now_wall=1_100.0,
        persist_s=600.0,
        latch=latch,
    )
    assert latch.purge_anchor_wall == 1_000.0


def test_gate_anchor_from_purged_applied_token(mp):
    latch = mp.GateLatchState()
    latch, event = mp.update_gate_latch(
        band="critical",
        applied=["purged"],
        notes=[],
        now_wall=1_000.0,
        persist_s=600.0,
        latch=latch,
    )
    assert event is None
    assert latch.purge_anchor_wall == 1_000.0


def test_build_gate_payload_active(mp):
    sample = mp.MemSample(
        page_size=16384,
        compressor_pages=int(30 * (1024**3) / 16384),
        purgeable_pages=0,
        swap_used_mb=20 * 1024,
        swap_total_mb=22 * 1024,
    )
    latch = mp.GateLatchState(
        purge_anchor_wall=1_000.0,
        latched=True,
        notify_sent=True,
        latched_at_wall=1_600.0,
    )
    payload = mp.build_gate_payload(
        latch=latch,
        band="critical",
        sample=sample,
        persist_s=600.0,
        now_wall=1_600.0,
        event="latch",
    )
    assert payload["schema"] == "og.memory_gate.v1"
    assert payload["state"] == "active"
    assert payload["persist_s"] == 600.0
    assert payload["reason"] == "critical_persisted_after_purge"


def test_write_gate_file_atomic(mp, tmp_path):
    path = tmp_path / "state" / "memory-gate.json"
    mp.write_gate_file(
        path,
        payload={
            "schema": "og.memory_gate.v1",
            "state": "clear",
            "reason": "band_ok",
            "latched_at": None,
            "cleared_at": "2026-09-10T00:00:00Z",
            "persist_s": 600,
            "band": "ok",
            "compressor_gb": 1.0,
            "swap_used_gb": 1.0,
            "purge_anchor_wall": None,
            "notify_sent_at": None,
        },
    )
    assert path.is_file()
    data = __import__("json").loads(path.read_text())
    assert data["state"] == "clear"


def test_write_gate_file_chowns_when_uid_set(mp, tmp_path, monkeypatch):
    path = tmp_path / "state" / "memory-gate.json"
    seen = []

    def fake_chown(target, uid, gid):
        seen.append((str(target), uid, gid))

    monkeypatch.setattr(mp.os, "chown", fake_chown)
    mp.write_gate_file(
        path,
        payload={"schema": "og.memory_gate.v1", "state": "clear"},
        uid=501,
    )
    assert (str(path.parent), 501, -1) in seen
    assert (str(path), 501, -1) in seen


def test_notify_memory_gate_dry_run(mp):
    out = mp.notify_memory_gate(uid=501, dry_run=True)
    assert out == "dry_run:notify"
