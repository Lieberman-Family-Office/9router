#!/usr/bin/env python3
"""Probe 9router + Tailscale serve path; kickstart on hung-alive failure.

Mac-local watchdog for the EC2→Mac 9router path. launchd KeepAlive only
restarts crashed processes; this catches live-but-wedged daemons.

Probes (each cycle):
  1. local_9router  — GET http://127.0.0.1:20128/v1/models
  2. hairpin_ts     — GET http://<tailscale-ip>:20128/v1/models (TCP serve)
  3. helper         — GET http://127.0.0.1:20129/combo/subs-coding
  4. ts_backend     — `tailscale status --json` BackendState == Running

Two consecutive failures of the same probe trigger kickstart(s), subject to
per-target cooldown. Logs JSON lines to <home>/.9router/logs/path-watchdog.log.

Intended to run as root LaunchDaemon so it can:
  - kickstart system/com.lfenergy.tailscaled
  - kickstart gui/<uid>/com.lfenergy.9router
  - kickstart gui/<uid>/com.lfenergy.9router-combo-helper
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, MutableMapping, Optional, Sequence

DEFAULT_INTERVAL_S = 30
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_FAIL_THRESHOLD = 2
DEFAULT_COOLDOWN_S = 180
DEFAULT_TS_SOCKET = "/var/run/tailscaled.socket"
DEFAULT_TS_BIN = "/opt/homebrew/bin/tailscale"
LOCAL_9ROUTER = "http://127.0.0.1:20128/v1/models"
LOCAL_HELPER = "http://127.0.0.1:20129/combo/subs-coding"

TARGET_TAILSCALE = "tailscale"
TARGET_9ROUTER = "9router"
TARGET_HELPER = "helper"

PROBE_TARGETS: Mapping[str, tuple[str, ...]] = {
    "local_9router": (TARGET_9ROUTER,),
    "hairpin_ts": (TARGET_TAILSCALE, TARGET_9ROUTER),
    "helper": (TARGET_HELPER,),
    "ts_backend": (TARGET_TAILSCALE,),
}


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str
    targets: tuple[str, ...]


@dataclass
class WatchState:
    fail_streaks: MutableMapping[str, int] = field(default_factory=dict)
    last_kick_mono: MutableMapping[str, float] = field(default_factory=dict)


def http_probe(url: str, *, timeout_s: float) -> tuple[bool, str]:
    headers = {"User-Agent": "9router-path-watchdog/1.0"}
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            body = resp.read(256)
            if 200 <= int(code) < 300:
                return True, f"http_{code}"
            return False, f"http_{code}_body={body[:80]!r}"
    except urllib.error.HTTPError as exc:
        return False, f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001 — probe must never raise
        return False, f"{type(exc).__name__}:{exc}"


def resolve_tailscale_bin(explicit: Optional[str] = None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(
        [
            DEFAULT_TS_BIN,
            "/usr/local/bin/tailscale",
            "/opt/homebrew/bin/tailscale",
        ]
    )
    for path in candidates:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return path
    which = subprocess.run(
        ["/usr/bin/which", "tailscale"],
        capture_output=True,
        text=True,
        check=False,
    )
    found = (which.stdout or "").strip()
    if found and Path(found).is_file():
        return found
    raise FileNotFoundError(
        "tailscale binary not found; pass --ts-bin or install Homebrew tailscale"
    )


def tailscale_ip4(
    *,
    socket_path: str,
    ts_bin: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> Optional[str]:
    proc = runner(
        [ts_bin, f"--socket={socket_path}", "ip", "-4"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return None
    ip = (proc.stdout or "").strip().splitlines()
    return ip[0].strip() if ip else None


def tailscale_backend_running(
    *,
    socket_path: str,
    ts_bin: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> tuple[bool, str]:
    proc = runner(
        [ts_bin, f"--socket={socket_path}", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:160]
        return False, f"cli_rc={proc.returncode}:{err}"
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return False, f"json:{exc}"
    state = data.get("BackendState")
    return state == "Running", f"BackendState={state!r}"


def run_probes(
    *,
    timeout_s: float,
    ts_socket: str,
    ts_bin: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    http: Optional[Callable[[str], tuple[bool, str]]] = None,
) -> list[ProbeResult]:
    do_http = http or (lambda url: http_probe(url, timeout_s=timeout_s))
    results: list[ProbeResult] = []

    ok, detail = do_http(LOCAL_9ROUTER)
    results.append(
        ProbeResult("local_9router", ok, detail, PROBE_TARGETS["local_9router"])
    )

    ts_ip = tailscale_ip4(socket_path=ts_socket, ts_bin=ts_bin, runner=runner)
    if not ts_ip:
        results.append(
            ProbeResult(
                "hairpin_ts", False, "no_tailscale_ip", PROBE_TARGETS["hairpin_ts"]
            )
        )
    else:
        ok, detail = do_http(f"http://{ts_ip}:20128/v1/models")
        results.append(
            ProbeResult("hairpin_ts", ok, detail, PROBE_TARGETS["hairpin_ts"])
        )

    ok, detail = do_http(LOCAL_HELPER)
    results.append(ProbeResult("helper", ok, detail, PROBE_TARGETS["helper"]))

    ok, detail = tailscale_backend_running(
        socket_path=ts_socket, ts_bin=ts_bin, runner=runner
    )
    results.append(ProbeResult("ts_backend", ok, detail, PROBE_TARGETS["ts_backend"]))
    return results


def decide_kicks(
    results: Sequence[ProbeResult],
    state: WatchState,
    *,
    now_mono: float,
    fail_threshold: int,
    cooldown_s: float,
) -> tuple[list[str], list[str]]:
    """Return (targets_to_kick, notes). Updates fail streaks on ``state``."""
    notes: list[str] = []
    kick: set[str] = set()

    for result in results:
        if result.ok:
            state.fail_streaks[result.name] = 0
            continue
        streak = state.fail_streaks.get(result.name, 0) + 1
        state.fail_streaks[result.name] = streak
        notes.append(f"{result.name}:fail:{streak}:{result.detail}")
        if streak < fail_threshold:
            continue
        for target in result.targets:
            last = state.last_kick_mono.get(target)
            if last is not None and (now_mono - last) < cooldown_s:
                notes.append(f"{target}:cooldown")
                continue
            kick.add(target)

    return sorted(kick), notes


def kickstart_commands(*, uid: int, targets: Iterable[str]) -> list[list[str]]:
    cmds: list[list[str]] = []
    for target in targets:
        if target == TARGET_TAILSCALE:
            cmds.append(
                ["/bin/launchctl", "kickstart", "-k", "system/com.lfenergy.tailscaled"]
            )
        elif target == TARGET_9ROUTER:
            cmds.append(
                ["/bin/launchctl", "kickstart", "-k", f"gui/{uid}/com.lfenergy.9router"]
            )
        elif target == TARGET_HELPER:
            cmds.append(
                [
                    "/bin/launchctl",
                    "kickstart",
                    "-k",
                    f"gui/{uid}/com.lfenergy.9router-combo-helper",
                ]
            )
        else:
            raise ValueError(f"unknown kick target: {target}")
    return cmds


def apply_kicks(
    targets: Sequence[str],
    *,
    uid: int,
    state: WatchState,
    now_mono: float,
    dry_run: bool,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    actions: list[str] = []
    for cmd in kickstart_commands(uid=uid, targets=targets):
        label = " ".join(cmd)
        if dry_run:
            actions.append(f"dry_run:{label}")
            continue
        proc = runner(cmd, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode == 0:
            actions.append(f"kicked:{label}")
        else:
            err = (proc.stderr or proc.stdout or "").strip()[:200]
            actions.append(f"kick_failed:rc={proc.returncode}:{label}:{err}")
    if not dry_run:
        for target in targets:
            state.last_kick_mono[target] = now_mono
    return actions


def log_event(log_path: Path, event: Mapping[str, object]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True, separators=(",", ":"))
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def cycle(
    *,
    home: Path,
    uid: int,
    timeout_s: float,
    fail_threshold: int,
    cooldown_s: float,
    ts_socket: str,
    ts_bin: str,
    state: WatchState,
    dry_run: bool,
    log_path: Optional[Path] = None,
) -> int:
    """Run one probe/kick cycle. Exit 0 healthy, 2 kicked, 1 degraded."""
    started = time.time()
    results = run_probes(timeout_s=timeout_s, ts_socket=ts_socket, ts_bin=ts_bin)
    kicks, notes = decide_kicks(
        results,
        state,
        now_mono=time.monotonic(),
        fail_threshold=fail_threshold,
        cooldown_s=cooldown_s,
    )
    actions = apply_kicks(
        kicks,
        uid=uid,
        state=state,
        now_mono=time.monotonic(),
        dry_run=dry_run,
    )
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "ok": all(r.ok for r in results),
        "probes": {r.name: {"ok": r.ok, "detail": r.detail} for r in results},
        "notes": notes,
        "kicks": kicks,
        "actions": actions,
        "dry_run": dry_run,
        "uid": uid,
        "home": str(home),
        "ts_bin": ts_bin,
    }
    path = log_path or (home / ".9router" / "logs" / "path-watchdog.log")
    log_event(path, event)
    print(json.dumps(event, sort_keys=True), flush=True)
    if kicks:
        return 2
    if not event["ok"]:
        return 1
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--home",
        type=Path,
        required=True,
        help="Operator home (state + logs live under .9router/)",
    )
    p.add_argument(
        "--uid",
        type=int,
        required=True,
        help="GUI session UID for LaunchAgent kickstarts",
    )
    p.add_argument(
        "--loop", action="store_true", help="Run forever (LaunchDaemon mode)"
    )
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--fail-threshold", type=int, default=DEFAULT_FAIL_THRESHOLD)
    p.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_S)
    p.add_argument("--ts-socket", default=DEFAULT_TS_SOCKET)
    p.add_argument(
        "--ts-bin",
        default=DEFAULT_TS_BIN,
        help="Absolute path to tailscale CLI (LaunchDaemon PATH is minimal)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Decide kicks but do not launchctl"
    )
    p.add_argument(
        "--once", action="store_true", help="Single cycle (default when --loop absent)"
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    home = args.home.expanduser().resolve()
    ts_bin = resolve_tailscale_bin(args.ts_bin)
    state = WatchState()
    if args.loop:
        while True:
            try:
                cycle(
                    home=home,
                    uid=args.uid,
                    timeout_s=args.timeout,
                    fail_threshold=args.fail_threshold,
                    cooldown_s=args.cooldown,
                    ts_socket=args.ts_socket,
                    ts_bin=ts_bin,
                    state=state,
                    dry_run=args.dry_run,
                )
            except Exception as exc:  # noqa: BLE001 — never die the daemon silently
                log_event(
                    home / ".9router" / "logs" / "path-watchdog.log",
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "ok": False,
                        "fatal": f"{type(exc).__name__}:{exc}",
                    },
                )
            time.sleep(max(1.0, float(args.interval)))
    return cycle(
        home=home,
        uid=args.uid,
        timeout_s=args.timeout,
        fail_threshold=args.fail_threshold,
        cooldown_s=args.cooldown,
        ts_socket=args.ts_socket,
        ts_bin=ts_bin,
        state=state,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
