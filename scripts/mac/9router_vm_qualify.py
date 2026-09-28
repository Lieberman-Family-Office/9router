#!/usr/bin/env python3
"""Qualify ONE 9router release tarball in a throwaway macOS VM before production.

Host:  9router_vm_qualify.py run <tgz>
  1. clone the stopped base VM (APFS clone, seconds) and boot it
  2. copy the tgz + this script + 9router_deploy.py into the guest
  3. run `guest` there; always delete the clone afterwards
  4. write ~/.9router/qualified/<sha256>.json — 9router_deploy.py deploy refuses
     any tarball without a "pass" record for its exact bytes

Guest: 9router_vm_qualify.py guest <tgz>   (never run on the host; refuses there)
  The base VM mirrors production: Homebrew node, the live release adopted behind
  /opt/homebrew/lib/node_modules/9router, the same start.sh + hotpatches under the
  same launchd label, and its OWN provider logins (production tokens never enter).
  Checks, all must pass:
    deploy        9router_deploy.py deploy (npm install, launchd restart, version,
                  /v1/models, one stream to its end; auto-rollback on failure)
    stream:<m>    one streamed completion per model in QUALIFY_MODELS
    concurrent    4 parallel streams, while /api/version is polled: every stream
                  ends and the API never stalls past STALL_S (the wedge check)
    child-crash   SIGKILL the next-server child; cli.js must respawn it and serve
    kickstart     launchctl kickstart -k; the release must come back and stream

Exit: 0 pass · 1 usage/refused · 2 a check failed · 3 VM infrastructure failed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

BASE_VM = os.environ.get("NINEROUTER_QUALIFY_BASE", "9r-base")
QUALIFIED = Path.home() / ".9router" / "qualified"
QUALIFY_MODELS = os.environ.get(
    "NINEROUTER_QUALIFY_MODELS", "cx/gpt-5.4-mini,cc/claude-haiku-4-5-20251001"
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
    out = subprocess.run(
        ["pgrep", "-f", "node_modules/9router/app/"], capture_output=True, text=True
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


def cmd_guest(args) -> int:
    if not in_vm():
        print("refused: `guest` runs only inside the qualification VM")
        return 1
    dep = load_deploy()
    tgz = Path(args.tgz)
    version = dep.tgz_version(tgz)
    results: dict[str, str] = {}

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
    ok = all(v == "ok" for v in results.values())
    print(
        json.dumps(
            {"version": version, "result": "pass" if ok else "fail", "checks": results}
        )
    )
    return 0 if ok else 2


# ----------------------------------------------------------------- host side


def lima(*argv: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["limactl", "--tty=false", *argv], **kw)


def cmd_run(args) -> int:
    if in_vm():
        print("refused: `run` is the host side; inside the VM use `guest`")
        return 1
    tgz = Path(args.tgz).resolve()
    digest = sha256(tgz)
    inst = f"9r-q-{digest[:12]}"
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lima("delete", "--force", inst, capture_output=True)  # leftover from a crash
    # No guest->host port forwards: the clone's router binds 20128 like production.
    no_fwd = '.portForwards = [{"guestPortRange": [1, 65535], "ignore": true}]'
    if lima("clone", BASE_VM, inst, "--set", no_fwd, "--start").returncode:
        print(f"VM: could not clone/start {BASE_VM} -> {inst}")
        return 3
    try:
        cp = lima(
            "copy",
            str(tgz),
            str(HERE / "9router_deploy.py"),
            str(Path(__file__).resolve()),
            f"{inst}:/tmp/",
        )
        if cp.returncode:
            print("VM: copy into guest failed")
            return 3
        out = lima(
            "shell",
            "--workdir",
            "/",
            inst,
            "/opt/homebrew/bin/python3",
            "/tmp/9router_vm_qualify.py",
            "guest",
            f"/tmp/{tgz.name}",
            capture_output=True,
            text=True,
        )
        sys.stderr.write(out.stderr)
        lines = out.stdout.strip().splitlines()
        print("\n".join(lines))
        try:
            record = json.loads(lines[-1])
        except (IndexError, ValueError):
            print(f"VM: guest produced no result (rc={out.returncode})")
            return 3
    finally:
        lima("stop", "--force", inst, capture_output=True)
        lima("delete", "--force", inst, capture_output=True)
    record.update(
        tgz=tgz.name,
        sha256=digest,
        deploy_sha256=sha256(HERE / "9router_deploy.py"),
        base_vm=BASE_VM,
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
