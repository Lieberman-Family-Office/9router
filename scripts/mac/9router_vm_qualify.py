#!/usr/bin/env python3
"""Qualify ONE 9router release tarball on the Namespace macOS devbox before production.

Host:  9router_vm_qualify.py run <tgz>
  1. scp the tgz + this script + 9router_deploy.py to a per-run dir on the devbox
  2. run `guest` there over ssh; always delete the per-run dir afterwards
  3. write ~/.9router/qualified/<sha256>.json — 9router_deploy.py deploy refuses
     any tarball without a "pass" record for its exact bytes

Guest: 9router_vm_qualify.py guest <tgz>   (never run on the host; refuses there)
  The devbox is provisioned once by 9router_devbox_baseline.sh and mirrors
  production: Homebrew node, a baseline release behind
  /opt/homebrew/lib/node_modules/9router, the same start.sh under the same launchd
  label, and its OWN provider logins (production tokens never enter).
  Before and after every run the guest resets to the baseline release
  (~/.9router/baseline) and deletes every other release dir. The DB is NOT
  restored: provider refresh tokens rotate, so a restored DB would log them out.
  Checks, all must pass:
    deploy        9router_deploy.py deploy (npm install, launchd restart, version,
                  /v1/models, one stream to its end; auto-rollback on failure)
    stream:<m>    one streamed completion per model in QUALIFY_MODELS (incl. a
                  (compact_200k) suffixed id: it must route and stream)
    concurrent    4 parallel streams, while /api/version is polled: every stream
                  ends and the API never stalls past STALL_S (the wedge check)
    child-crash   SIGKILL the next-server child; cli.js must respawn it and serve
    kickstart     launchctl kickstart -k; the release must come back and stream

Exit: 0 pass · 1 usage/refused · 2 a check failed · 3 devbox infrastructure failed.

ponytail: one devbox, no lock — two concurrent runs would collide on port 20128.
Upgrade: `devbox acquire 9router-qualify` per run once qualification runs in parallel.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HOST = os.environ.get("NINEROUTER_QUALIFY_HOST", "9router-test-vm.devbox.namespace")
QUALIFIED = Path.home() / ".9router" / "qualified"
QUALIFY_MODELS = os.environ.get(
    "NINEROUTER_QUALIFY_MODELS",
    # ponytail: (compact_200k) proves a suffixed id routes and streams, NOT that
    # context_management reached upstream (an ignored suffix also streams). Upgrade:
    # assert the outbound body via the DBG chunk logger if compaction ever regresses.
    "cx/gpt-5.4-mini,cc/claude-haiku-4-5-20251001,cx/gpt-5.4-mini(compact_200k)",
).split(",")
STALL_S = 5.0
HERE = Path(__file__).resolve().parent


def in_vm() -> bool:
    out = subprocess.run(
        ["sysctl", "-n", "kern.hv_vmm_present"], capture_output=True, text=True
    )
    return out.stdout.strip() == "1"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- guest side


def load_deploy():
    spec = importlib.util.spec_from_file_location("dep", HERE / "9router_deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def stream(dep, model: str) -> str | None:
    dep.PROBE_MODEL = model
    return dep.stream_probe()


def check_concurrent(dep, model: str) -> str | None:
    errors: list[str] = []
    worst = 0.0

    def one():
        err = stream(dep, model)
        if err:
            errors.append(err)

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        t0 = time.monotonic()
        try:
            with dep.http("/api/version", timeout=STALL_S) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"api stalled: {exc}")
            break
        worst = max(worst, time.monotonic() - t0)
        time.sleep(0.5)
    for t in threads:
        t.join(120)
    if worst > STALL_S:
        errors.append(f"api latency {worst:.1f}s > {STALL_S}s")
    return "; ".join(errors) or None


def child_pid() -> int | None:
    # Next rewrites the child's argv to "next-server (vX.Y.Z)"; the path never shows.
    out = subprocess.run(
        ["pgrep", "-f", "^next-server "], capture_output=True, text=True
    )
    pids = [int(p) for p in out.stdout.split()]
    return pids[0] if pids else None


def check_child_crash(dep, version: str) -> str | None:
    pid = child_pid()
    if pid is None:
        return "no next-server child found"
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        new = child_pid()
        if new and new != pid:
            return dep.verify(version, ready_timeout=60)
        time.sleep(1)
    return "child not respawned within 60s"


def check_kickstart(dep, version: str) -> str | None:
    dep.restart()
    return dep.verify(version)


def reset_to_baseline(dep, baseline: str) -> str | None:
    """Point the link at the baseline release, restart, drop every other release."""
    target = dep.release_dir(baseline)
    if not target.is_dir():
        return f"baseline release {baseline} missing at {target}"
    if dep.live() != target:
        dep.switch(target)
        dep.restart()
    for d in dep.RELEASES.iterdir():
        if d.name != baseline:
            shutil.rmtree(d)
    return None


def cmd_guest(args) -> int:
    if not in_vm():
        print("refused: `guest` runs only on the qualification devbox")
        return 1
    dep = load_deploy()
    tgz = Path(args.tgz)
    version = dep.tgz_version(tgz)
    baseline = (dep.HOME / "baseline").read_text().strip()
    if version == baseline:
        print(f"refused: {version} is the devbox baseline; qualify a newer version")
        return 1
    err = reset_to_baseline(dep, baseline)
    if err:
        print(f"devbox: {err}")
        return 3
    results: dict[str, str] = {}
    try:
        rc = subprocess.run(
            [sys.executable, str(HERE / "9router_deploy.py"), "deploy", str(tgz)]
        ).returncode
        results["deploy"] = "ok" if rc == 0 else f"FAIL rc={rc}"
        if rc == 0:
            for m in QUALIFY_MODELS:
                results[f"stream:{m}"] = stream(dep, m) or "ok"
            results["concurrent"] = check_concurrent(dep, QUALIFY_MODELS[0]) or "ok"
            results["child-crash"] = check_child_crash(dep, version) or "ok"
            results["kickstart"] = check_kickstart(dep, version) or "ok"
    finally:
        err = reset_to_baseline(dep, baseline)
        if err:
            results["reset"] = err
    ok = all(v == "ok" for v in results.values())
    print(
        json.dumps(
            {"version": version, "result": "pass" if ok else "fail", "checks": results}
        )
    )
    return 0 if ok else 2


# ----------------------------------------------------------------- host side


def ssh(*argv: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, *argv], **kw)


def cmd_run(args) -> int:
    if in_vm():
        print("refused: `run` is the host side; on the devbox use `guest`")
        return 1
    tgz = Path(args.tgz).resolve()
    digest = sha256(tgz)
    work = f"/tmp/9r-q-{digest[:12]}"  # fixed name: a crashed run's dir is reused
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if ssh("mkdir", "-p", work).returncode:
        print(f"devbox: {HOST} unreachable")
        return 3
    try:
        cp = subprocess.run(
            [
                "scp",
                "-q",
                "-o",
                "BatchMode=yes",
                str(tgz),
                str(HERE / "9router_deploy.py"),
                str(HERE / "9router_devbox_restore.sh"),
                str(Path(__file__).resolve()),
                f"{HOST}:{work}/",
            ]
        )
        if cp.returncode:
            print("devbox: copy failed")
            return 3
        if ssh("bash", f"{work}/9router_devbox_restore.sh").returncode:
            print("devbox: persistent runtime restoration failed")
            return 3
        out = ssh(
            "/opt/homebrew/bin/python3",
            f"{work}/9router_vm_qualify.py",
            "guest",
            f"{work}/{tgz.name}",
            capture_output=True,
            text=True,
        )
        sys.stderr.write(out.stderr)
        lines = out.stdout.strip().splitlines()
        print("\n".join(lines))
        try:
            record = json.loads(lines[-1])
        except (IndexError, ValueError):
            print(f"devbox: guest produced no result (rc={out.returncode})")
            return 1 if out.returncode == 1 else 3  # 1 = guest refused
    finally:
        ssh("rm", "-rf", work, capture_output=True)
    record.update(
        tgz=tgz.name,
        sha256=digest,
        deploy_sha256=sha256(HERE / "9router_deploy.py"),
        host=HOST,
        started=started,
    )
    QUALIFIED.mkdir(parents=True, exist_ok=True)
    (QUALIFIED / f"{digest}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"{record['result'].upper()}: {tgz.name} sha256={digest[:12]}")
    return 0 if record["result"] == "pass" else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("guest", cmd_guest)):
        s = sub.add_parser(name)
        s.add_argument("tgz")
        s.set_defaults(fn=fn)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
