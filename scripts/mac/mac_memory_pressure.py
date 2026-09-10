#!/usr/bin/env python3
"""Monitor Mac memory/compressor/swap pressure; apply safe remediations.

Root LaunchDaemon. Does NOT kill Cursor/agent sessions or reboot.

Bands (page size typically 16 KiB on Apple Silicon):
  ok       — compressor < 20 GB and swap used < 8 GB
  warn     — compressor >= 20 GB OR swap used >= 8 GB
  critical — compressor >= 32 GB OR swap used >= 22 GB

Under warn/critical (2 consecutive samples): purge; optionally kickstart
9router / combo-helper when RSS/uptime gates match; under critical also
trim Homebrew download caches older than 14 days.

Memory gate (<home>/.9router/state/memory-gate.json): after a critical
purge, the latch fires if band stays critical for --gate-persist seconds
(default 14400 = 4h). While active, Cursor hooks deny nested Task tool calls.

Logs JSON lines to <home>/.9router/logs/memory-pressure.log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Optional, Sequence

PAGE_SIZE_DEFAULT = 16384

WARN_COMPRESSOR_GB = 20.0
WARN_SWAP_GB = 8.0
CRITICAL_COMPRESSOR_GB = 32.0
CRITICAL_SWAP_GB = 22.0

NINE_ROUTER_RSS_MB = 1536.0  # 1.5 GB
NINE_ROUTER_UPTIME_S = 12 * 3600
HELPER_RSS_MB = 512.0

DEFAULT_OK_INTERVAL_S = 300.0
DEFAULT_STRESS_INTERVAL_S = 60.0
DEFAULT_FAIL_THRESHOLD = 2
DEFAULT_COOLDOWN_S = 1800.0  # 30 min
DEFAULT_TOP_N = 15
HOMEBREW_CACHE_MAX_AGE_S = 14 * 86400

DEFAULT_GATE_PERSIST_S = 14400.0  # 4h critical after purge
GATE_SCHEMA = "og.memory_gate.v1"
NINE_ROUTER_DIRNAME = ".9router"
ISO_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
GATE_STATE_REL = (NINE_ROUTER_DIRNAME, "state", "memory-gate.json")
GATE_TMP_SUFFIX = ".tmp"


@dataclass
class GateLatchState:
    purge_anchor_wall: Optional[float] = None
    latched: bool = False
    notify_sent: bool = False
    latched_at_wall: Optional[float] = None


def update_gate_latch(
    *,
    band: str,
    applied: Sequence[str],
    notes: Sequence[str],
    now_wall: float,
    persist_s: float,
    latch: GateLatchState,
) -> tuple[GateLatchState, Optional[str]]:
    """Advance latch state. Returns (new_state, event) with event in {None, latch, clear}."""
    new = GateLatchState(
        purge_anchor_wall=latch.purge_anchor_wall,
        latched=latch.latched,
        notify_sent=latch.notify_sent,
        latched_at_wall=latch.latched_at_wall,
    )

    if band == "ok":
        was = new.latched
        new.purge_anchor_wall = None
        new.latched = False
        new.notify_sent = False
        new.latched_at_wall = None
        return new, ("clear" if was else None)

    purge_signal = any(
        a == "purge"
        or a == "purged"
        or a.startswith("dry_run:purge")
        for a in applied
    ) or ("purge:cooldown" in notes and band == "critical")

    if band == "critical" and purge_signal and new.purge_anchor_wall is None:
        new.purge_anchor_wall = now_wall

    if (
        band == "critical"
        and new.purge_anchor_wall is not None
        and (now_wall - new.purge_anchor_wall) >= persist_s
        and not new.latched
    ):
        new.latched = True
        new.notify_sent = True
        new.latched_at_wall = now_wall
        return new, "latch"

    return new, None


def gate_path(home: Path) -> Path:
    return Path(os.path.realpath(home)).joinpath(*GATE_STATE_REL)


def _iso_utc(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime(ISO_UTC_FMT)
    )


def safe_path_under(path: Path | str, *, root: Path | str) -> str:
    """Canonicalize ``path`` and refuse escapes outside ``root`` (S8707)."""
    resolved = os.path.realpath(path)
    base_dir = os.path.realpath(root)
    if resolved != base_dir and not resolved.startswith(base_dir + os.sep):
        raise ValueError(f"path {path!r} is outside the allowed directory")
    return resolved


def build_gate_payload(
    *,
    latch: GateLatchState,
    band: str,
    sample: MemSample,
    persist_s: float,
    now_wall: float,
    event: Optional[str],
) -> dict:
    if latch.latched:
        reason = "critical_persisted_after_purge"
        state = "active"
        cleared_at = None
        latched_at = _iso_utc(latch.latched_at_wall)
        notify_sent_at = latched_at if latch.notify_sent else None
    else:
        reason = "band_ok" if band == "ok" else "tracking"
        state = "clear"
        cleared_at = _iso_utc(now_wall) if event == "clear" else None
        latched_at = None
        notify_sent_at = None
    return {
        "schema": GATE_SCHEMA,
        "state": state,
        "reason": reason,
        "latched_at": latched_at,
        "cleared_at": cleared_at,
        "persist_s": persist_s,
        "band": band,
        "compressor_gb": round(sample.compressor_gb, 2),
        "swap_used_gb": round(sample.swap_used_gb, 2),
        "purge_anchor_wall": _iso_utc(latch.purge_anchor_wall),
        "notify_sent_at": notify_sent_at,
    }


def write_gate_file(
    *,
    home: Path,
    payload: Mapping[str, object],
    uid: Optional[int] = None,
) -> Path:
    """Atomically write the gate file under ``home/.9router/state/``.

    Path is derived only from ``home`` plus fixed relative segments — never from
    a caller-supplied file path — then canonicalized with ``safe_path_under``.
    When ``uid`` is set, chown is best-effort and does not raise.
    """
    home_root = os.path.realpath(home)
    candidate = Path(home_root).joinpath(*GATE_STATE_REL)
    path_s = safe_path_under(candidate, root=home_root)
    parent_s = os.path.dirname(path_s)
    os.makedirs(parent_s, exist_ok=True)
    tmp_s = safe_path_under(path_s + GATE_TMP_SUFFIX, root=home_root)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with open(tmp_s, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp_s, path_s)
    if uid is not None:
        for target in (parent_s, path_s):
            try:
                os.chown(target, uid, -1)
            except OSError:
                # Best-effort only: gate bytes are already written; ownership fix
                # may fail on some FS layouts and must not abort the cycle.
                continue
    return Path(path_s)


def notify_memory_gate(
    *,
    uid: int,
    dry_run: bool,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    if dry_run:
        return "dry_run:notify"
    script = (
        'display notification "Mac memory still critical after purge; '
        'nested Task spawn is gated." with title "LF Energy memory gate"'
    )
    cmd = [
        "/bin/launchctl",
        "asuser",
        str(uid),
        "/usr/bin/osascript",
        "-e",
        script,
    ]
    try:
        runner(cmd, capture_output=True, text=True, timeout=15, check=False)
    except Exception as exc:  # noqa: BLE001
        return f"notify:failed:{type(exc).__name__}"
    return "notify:sent"


VM_STAT = "/usr/bin/vm_stat"
SYSCTL = "/usr/sbin/sysctl"
PURGE = "/usr/sbin/purge"
PS = "/bin/ps"
LAUNCHCTL = "/bin/launchctl"

CLASS_PURGE = "purge"
CLASS_KICK = "kick"
CLASS_CACHE = "cache"


@dataclass
class MemSample:
    page_size: int
    compressor_pages: int
    purgeable_pages: int
    swap_used_mb: float
    swap_total_mb: float
    free_pct: Optional[float] = None

    @property
    def compressor_gb(self) -> float:
        return (self.compressor_pages * self.page_size) / (1024.0**3)

    @property
    def swap_used_gb(self) -> float:
        return self.swap_used_mb / 1024.0


@dataclass
class ProcInfo:
    pid: int
    rss_kb: int
    etime_s: float
    command: str

    @property
    def rss_mb(self) -> float:
        return self.rss_kb / 1024.0


@dataclass
class PressureState:
    stress_streak: int = 0
    last_remediation_mono: MutableMapping[str, float] = field(default_factory=dict)
    gate: GateLatchState = field(default_factory=GateLatchState)


def classify_band(sample: MemSample) -> str:
    if (
        sample.compressor_gb >= CRITICAL_COMPRESSOR_GB
        or sample.swap_used_gb >= CRITICAL_SWAP_GB
    ):
        return "critical"
    if (
        sample.compressor_gb >= WARN_COMPRESSOR_GB
        or sample.swap_used_gb >= WARN_SWAP_GB
    ):
        return "warn"
    return "ok"


def parse_vm_stat(text: str, *, page_size: int = PAGE_SIZE_DEFAULT) -> dict:
    """Parse `vm_stat` output into a dict of int counters."""
    out: dict = {"page_size": page_size}
    m = re.search(r"page size of\s+(\d+)\s+bytes", text)
    if m:
        out["page_size"] = int(m.group(1))
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip().strip('"')
        num = re.search(r"(\d+)", rest.replace(".", ""))
        if not num:
            continue
        # Normalize keys used below
        norm = key.lower().replace(" ", "_")
        out[norm] = int(num.group(1))
    return out


def parse_swapusage(text: str) -> tuple[float, float]:
    """Return (used_mb, total_mb) from `sysctl vm.swapusage`."""
    # vm.swapusage: total = 22528.00M  used = 21250.88M  free = 1277.12M
    total_m = re.search(r"total\s*=\s*([\d.]+)M", text)
    used_m = re.search(r"used\s*=\s*([\d.]+)M", text)
    if not total_m or not used_m:
        return 0.0, 0.0
    return float(used_m.group(1)), float(total_m.group(1))


def parse_etime_to_seconds(etime: str) -> float:
    """Parse ps etime ([[dd-]hh:]mm:ss) to seconds."""
    etime = etime.strip()
    days = 0
    if "-" in etime:
        day_s, etime = etime.split("-", 1)
        days = int(day_s)
    parts = [int(p) for p in etime.split(":")]
    if len(parts) == 3:
        hh, mm, ss = parts
    elif len(parts) == 2:
        hh = 0
        mm, ss = parts
    else:
        return float(days * 86400)
    return float(days * 86400 + hh * 3600 + mm * 60 + ss)


def sample_memory(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> MemSample:
    vm = runner([VM_STAT], capture_output=True, text=True, timeout=15, check=False)
    parsed = parse_vm_stat(vm.stdout or "", page_size=PAGE_SIZE_DEFAULT)
    page_size = int(parsed.get("page_size", PAGE_SIZE_DEFAULT))
    compressor = int(
        parsed.get("pages_occupied_by_compressor")
        or parsed.get("pages_used_by_compressor")
        or 0
    )
    purgeable = int(parsed.get("pages_purgeable") or 0)

    sw = runner(
        [SYSCTL, "vm.swapusage"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    used_mb, total_mb = parse_swapusage(sw.stdout or "")

    # Skip memory_pressure — it can block for minutes under swap thrash.
    return MemSample(
        page_size=page_size,
        compressor_pages=compressor,
        purgeable_pages=purgeable,
        swap_used_mb=used_mb,
        swap_total_mb=total_mb,
        free_pct=None,
    )


def _proc_detail_from_pid(
    pid: int,
    command: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> Optional[ProcInfo]:
    detail = runner(
        [PS, "-p", str(pid), "-o", "rss=,etime="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    fields = (detail.stdout or "").strip().split()
    if len(fields) < 2:
        return None
    try:
        rss_kb = int(fields[0])
        etime_s = parse_etime_to_seconds(fields[1])
    except ValueError:
        return None
    return ProcInfo(
        pid=pid,
        rss_kb=rss_kb,
        etime_s=etime_s,
        command=command[:200],
    )


def _procs_matching_pattern(
    pattern: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> list[ProcInfo]:
    pg = runner(
        ["/usr/bin/pgrep", "-lf", pattern],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    rows: list[ProcInfo] = []
    for line in (pg.stdout or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        info = _proc_detail_from_pid(pid, parts[1], runner)
        if info is not None:
            rows.append(info)
    return rows


def _dedupe_procs_by_pid(rows: Sequence[ProcInfo]) -> list[ProcInfo]:
    by_pid: dict[int, ProcInfo] = {}
    for p in rows:
        prev = by_pid.get(p.pid)
        if prev is None or p.rss_kb > prev.rss_kb:
            by_pid[p.pid] = p
    return list(by_pid.values())


def list_top_rss(
    *,
    top_n: int = DEFAULT_TOP_N,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[ProcInfo]:
    # Targeted lookup only — full `ps -ax` can hang under swap thrash.
    rows: list[ProcInfo] = []
    for pattern in ("next-server", "combo-helper", "Cursor Helper", "Cursor$"):
        rows.extend(_procs_matching_pattern(pattern, runner))
    ranked = sorted(
        _dedupe_procs_by_pid(rows),
        key=lambda p: p.rss_kb,
        reverse=True,
    )
    return ranked[:top_n]


def find_proc(procs: Sequence[ProcInfo], *needles: str) -> Optional[ProcInfo]:
    for p in procs:
        cmd = p.command.lower()
        if all(n.lower() in cmd for n in needles):
            return p
    return None


def _cooled(
    cls: str,
    *,
    now_mono: float,
    last_remediation_mono: Mapping[str, float],
    cooldown_s: float,
) -> bool:
    last = last_remediation_mono.get(cls)
    if last is None:
        return False
    return (now_mono - last) < cooldown_s


def _note_kick_candidate(
    *,
    label: str,
    proc: Optional[ProcInfo],
    rss_limit_mb: float,
    uptime_limit_s: Optional[float],
    actions: list[str],
    notes: list[str],
    kick_token: str,
) -> None:
    if proc is None:
        notes.append(f"{label}:not_found")
        return
    over_rss = proc.rss_mb >= rss_limit_mb
    over_uptime = (
        uptime_limit_s is not None and proc.etime_s >= uptime_limit_s
    )
    if over_rss or over_uptime:
        actions.append(kick_token)
        notes.append(f"{label}:rss_mb={proc.rss_mb:.0f}:etime_s={proc.etime_s:.0f}")
        return
    notes.append(f"{label}:skip:rss_mb={proc.rss_mb:.0f}:etime_s={proc.etime_s:.0f}")


def decide_remediations(
    *,
    band: str,
    stress_streak: int,
    fail_threshold: int,
    sample: MemSample,
    procs: Sequence[ProcInfo],
    now_mono: float,
    last_remediation_mono: Mapping[str, float],
    cooldown_s: float,
) -> tuple[list[str], list[str]]:
    """Return (actions, notes). Actions are tokens: purge, kick:9router, …"""
    notes: list[str] = []
    actions: list[str] = []

    if band == "ok":
        return actions, notes
    if stress_streak < fail_threshold:
        notes.append(f"streak:{stress_streak}<{fail_threshold}")
        return actions, notes

    def cooled(cls: str) -> bool:
        return _cooled(
            cls,
            now_mono=now_mono,
            last_remediation_mono=last_remediation_mono,
            cooldown_s=cooldown_s,
        )

    if cooled(CLASS_PURGE):
        notes.append("purge:cooldown")
    else:
        actions.append("purge")

    if cooled(CLASS_KICK):
        notes.append("kick:cooldown")
    else:
        nine = find_proc(procs, "next-server") or find_proc(procs, "9router")
        helper = find_proc(procs, "combo-helper")
        _note_kick_candidate(
            label="9router",
            proc=nine,
            rss_limit_mb=NINE_ROUTER_RSS_MB,
            uptime_limit_s=NINE_ROUTER_UPTIME_S,
            actions=actions,
            notes=notes,
            kick_token="kick:9router",
        )
        _note_kick_candidate(
            label="helper",
            proc=helper,
            rss_limit_mb=HELPER_RSS_MB,
            uptime_limit_s=None,
            actions=actions,
            notes=notes,
            kick_token="kick:helper",
        )

    if band == "critical":
        if cooled(CLASS_CACHE):
            notes.append("cache:cooldown")
        else:
            actions.append("cache:homebrew")
            notes.append(f"purgeable_pages={sample.purgeable_pages}")

    return actions, notes


def apply_purge(
    *,
    dry_run: bool,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    if dry_run:
        return "dry_run:purge"
    proc = runner([PURGE], capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode == 0:
        return "purged"
    err = (proc.stderr or proc.stdout or "").strip()[:160]
    return f"purge_failed:rc={proc.returncode}:{err}"


def apply_kick(
    target: str,
    *,
    uid: int,
    dry_run: bool,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    if target == "9router":
        label = f"gui/{uid}/com.lfenergy.9router"
    elif target == "helper":
        label = f"gui/{uid}/com.lfenergy.9router-combo-helper"
    else:
        return f"kick_unknown:{target}"
    cmd = [LAUNCHCTL, "kickstart", "-k", label]
    if dry_run:
        return f"dry_run:{' '.join(cmd)}"
    proc = runner(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode == 0:
        return f"kicked:{label}"
    err = (proc.stderr or proc.stdout or "").strip()[:160]
    return f"kick_failed:rc={proc.returncode}:{label}:{err}"


def _homebrew_file_age_bytes(
    path: Path, *, now_ts: float, max_age_s: float
) -> Optional[int]:
    """Return size if path is an old file; else None."""
    if not path.is_file():
        return None
    try:
        age = now_ts - path.stat().st_mtime
    except OSError:
        return None
    if age < max_age_s:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return 0


def trim_homebrew_cache(
    home: Path,
    *,
    dry_run: bool,
    now: Optional[float] = None,
    max_age_s: float = HOMEBREW_CACHE_MAX_AGE_S,
) -> str:
    """Remove Homebrew download files older than max_age_s."""
    root = home / "Library" / "Caches" / "Homebrew" / "downloads"
    if not root.is_dir():
        return "cache:homebrew:absent"
    now_ts = time.time() if now is None else now
    removed = 0
    bytes_freed = 0
    for path in root.iterdir():
        size = _homebrew_file_age_bytes(path, now_ts=now_ts, max_age_s=max_age_s)
        if size is None:
            continue
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                continue
        removed += 1
        bytes_freed += size
    prefix = "dry_run:" if dry_run else ""
    return f"{prefix}cache:homebrew:removed={removed}:bytes={bytes_freed}"


def apply_actions(
    actions: Sequence[str],
    *,
    home: Path,
    uid: int,
    dry_run: bool,
    state: PressureState,
    now_mono: float,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    results: list[str] = []
    for action in actions:
        if action == "purge":
            results.append(apply_purge(dry_run=dry_run, runner=runner))
            if not dry_run:
                state.last_remediation_mono[CLASS_PURGE] = now_mono
        elif action.startswith("kick:"):
            target = action.split(":", 1)[1]
            results.append(apply_kick(target, uid=uid, dry_run=dry_run, runner=runner))
            if not dry_run:
                state.last_remediation_mono[CLASS_KICK] = now_mono
        elif action == "cache:homebrew":
            results.append(trim_homebrew_cache(home, dry_run=dry_run))
            if not dry_run:
                state.last_remediation_mono[CLASS_CACHE] = now_mono
        else:
            results.append(f"unknown_action:{action}")
    return results


def log_event(log_path: Path, event: Mapping[str, object]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True, separators=(",", ":"))
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def cycle(
    *,
    home: Path,
    uid: int,
    state: PressureState,
    fail_threshold: int,
    cooldown_s: float,
    top_n: int,
    dry_run: bool,
    persist_s: float = DEFAULT_GATE_PERSIST_S,
    log_path: Optional[Path] = None,
) -> tuple[int, str]:
    """One sample/remediate cycle. Returns (exit_code, band)."""
    started = time.time()
    sample = sample_memory()
    band = classify_band(sample)
    if band == "ok":
        state.stress_streak = 0
    else:
        state.stress_streak += 1

    # For top RSS, under ok we still sample lightly but may skip on error
    try:
        procs = list_top_rss(top_n=top_n)
    except Exception as exc:  # noqa: BLE001
        procs = []
        top_err = f"{type(exc).__name__}:{exc}"
    else:
        top_err = None

    actions, notes = decide_remediations(
        band=band,
        stress_streak=state.stress_streak,
        fail_threshold=fail_threshold,
        sample=sample,
        procs=procs,
        now_mono=time.monotonic(),
        last_remediation_mono=state.last_remediation_mono,
        cooldown_s=cooldown_s,
    )
    applied = apply_actions(
        actions,
        home=home,
        uid=uid,
        dry_run=dry_run,
        state=state,
        now_mono=time.monotonic(),
    )
    gate_event: Optional[str]
    state.gate, gate_event = update_gate_latch(
        band=band,
        applied=applied,
        notes=notes,
        now_wall=started,
        persist_s=persist_s,
        latch=state.gate,
    )
    gate_payload = build_gate_payload(
        latch=state.gate,
        band=band,
        sample=sample,
        persist_s=persist_s,
        now_wall=started,
        event=gate_event,
    )
    try:
        write_gate_file(home=home, payload=gate_payload, uid=uid)
    except (OSError, ValueError) as exc:
        notes = list(notes) + [f"gate_write:{type(exc).__name__}"]
    notify_result = None
    if gate_event == "latch":
        notify_result = notify_memory_gate(uid=uid, dry_run=dry_run)
    event = {
        "ts": time.strftime(ISO_UTC_FMT, time.gmtime(started)),
        "band": band,
        "stress_streak": state.stress_streak,
        "compressor_gb": round(sample.compressor_gb, 2),
        "swap_used_gb": round(sample.swap_used_gb, 2),
        "swap_total_gb": round(sample.swap_total_mb / 1024.0, 2),
        "purgeable_pages": sample.purgeable_pages,
        "free_pct": sample.free_pct,
        "page_size": sample.page_size,
        "top_rss": [
            {
                "pid": p.pid,
                "rss_mb": round(p.rss_mb, 1),
                "etime_s": int(p.etime_s),
                "command": p.command[:160],
            }
            for p in procs
        ],
        "notes": notes,
        "actions": actions,
        "applied": applied,
        "dry_run": dry_run,
        "uid": uid,
        "home": str(home),
        "gate_event": gate_event,
        "gate_state": gate_payload["state"],
    }
    if notify_result is not None:
        event["notify_result"] = notify_result
    if top_err:
        event["top_rss_error"] = top_err
    path = log_path or (home / NINE_ROUTER_DIRNAME / "logs" / "memory-pressure.log")
    log_event(path, event)
    print(json.dumps(event, sort_keys=True), flush=True)
    if applied and any(not a.startswith("dry_run:") for a in applied):
        return 2, band
    if band != "ok":
        return 1, band
    return 0, band


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--home", type=Path, required=True)
    p.add_argument("--uid", type=int, required=True)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--ok-interval", type=float, default=DEFAULT_OK_INTERVAL_S)
    p.add_argument("--stress-interval", type=float, default=DEFAULT_STRESS_INTERVAL_S)
    p.add_argument("--fail-threshold", type=int, default=DEFAULT_FAIL_THRESHOLD)
    p.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_S)
    p.add_argument("--gate-persist", type=float, default=DEFAULT_GATE_PERSIST_S)
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    home = args.home.expanduser().resolve()
    state = PressureState()
    if args.loop:
        while True:
            try:
                _rc, band = cycle(
                    home=home,
                    uid=args.uid,
                    state=state,
                    fail_threshold=args.fail_threshold,
                    cooldown_s=args.cooldown,
                    top_n=args.top_n,
                    dry_run=args.dry_run,
                    persist_s=args.gate_persist,
                )
            except Exception as exc:  # noqa: BLE001
                log_event(
                    home / NINE_ROUTER_DIRNAME / "logs" / "memory-pressure.log",
                    {
                        "ts": time.strftime(ISO_UTC_FMT, time.gmtime()),
                        "band": "error",
                        "fatal": f"{type(exc).__name__}:{exc}",
                    },
                )
                band = "warn"
            sleep_s = args.ok_interval if band == "ok" else args.stress_interval
            time.sleep(max(5.0, float(sleep_s)))
    rc, _band = cycle(
        home=home,
        uid=args.uid,
        state=state,
        fail_threshold=args.fail_threshold,
        cooldown_s=args.cooldown,
        top_n=args.top_n,
        dry_run=args.dry_run,
        persist_s=args.gate_persist,
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
