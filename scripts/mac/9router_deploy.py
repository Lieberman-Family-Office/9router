#!/usr/bin/env python3
"""Deploy or roll back ONE 9router release on the Mac hop.

Layout:
  ~/.9router/releases/<version>/lib/node_modules/9router   one install per version
  /opt/homebrew/lib/node_modules/9router -> <that dir>      the only live pointer
  ~/.9router/deploys.log                                    one JSON line per action

Rules this enforces: a version directory is never overwritten; every deploy is
verified (version, /v1/models, one streamed completion to its end); a failed
verify switches the pointer back and restarts. start.sh, the watchdog and the
hotpatches keep using the /opt/homebrew path and follow the symlink.

ponytail: the runtime hotpatches in ~/.9router/apply-*.sh still mutate the live
release in place, so a release dir is not byte-immutable until they are folded
into source (one PR per patch).

Usage:
  9router_deploy.py adopt            # one-time: move the current real dir into releases/
  9router_deploy.py deploy <tgz>
  9router_deploy.py rollback [version]
  9router_deploy.py status
  9router_deploy.py probe            # verify the live release without changing it

Exit: 0 ok · 1 refused/usage · 2 verify failed, rolled back · 3 rollback failed too.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home() / ".9router"
RELEASES = HOME / "releases"
LOG = HOME / "deploys.log"
DB = HOME / "db" / "data.sqlite"
LINK = Path("/opt/homebrew/lib/node_modules/9router")
BASE = "http://127.0.0.1:20128"
LABEL = "com.lfenergy.9router"
PROBE_MODEL = os.environ.get("NINEROUTER_PROBE_MODEL", "cx/gpt-5.4-mini")
PROBE_ATTEMPTS = 3
PROBE_BACKOFF_S = 5.0
TRANSIENT_HTTP = {429, 500, 502, 503, 504}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(**entry) -> None:
    entry = {"ts": now(), **entry}
    print(json.dumps(entry))
    with LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def release_dir(version: str) -> Path:
    return RELEASES / version / "lib" / "node_modules" / "9router"


def pkg_version(pkg_dir: Path) -> str:
    return json.loads((pkg_dir / "package.json").read_text())["version"]


def live() -> Path | None:
    return LINK.resolve() if LINK.is_symlink() else None


def switch(target: Path) -> None:
    """Atomically point LINK at target (rename(2) replaces the old symlink)."""
    tmp = LINK.with_name(LINK.name + ".switch")
    if tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, LINK)


def restart() -> None:
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], check=False
    )


def api_key() -> str:
    key = os.environ.get("NINEROUTER_PROBE_KEY")
    if key:
        return key
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = con.execute("select key from apiKeys where isActive=1 limit 1").fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("no active API key for the stream probe")
    return row[0]


def http(path: str, body: dict | None = None, timeout: float = 10):
    headers = {"Content-Type": "application/json"}
    if body is not None:
        headers["Authorization"] = f"Bearer {api_key()}"
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    return urllib.request.urlopen(req, timeout=timeout)


def stream_probe(timeout: float = 90) -> str | None:
    """One streamed completion of PROBE_MODEL must terminate before the deadline.

    A timeout, 429 or 5xx is retried (PROBE_ATTEMPTS total, linear backoff): one
    overloaded upstream response must not roll back a good release. Anything
    else fails at once. A wedged release still fails: every attempt times out.
    """
    for n in range(1, PROBE_ATTEMPTS + 1):
        reason, transient = stream_once(timeout)
        if reason is None:
            return None
        if not transient or n == PROBE_ATTEMPTS:
            return reason if n == 1 else f"{reason} (attempt {n}/{PROBE_ATTEMPTS})"
        time.sleep(PROBE_BACKOFF_S * n)
    return None  # unreachable: the loop always returns


def stream_once(timeout: float) -> tuple[str | None, bool]:
    """(None, False) on a terminal chunk, else (reason, transient).

    Terminal = `data: [DONE]` or a chunk carrying a non-null finish_reason
    (this fork ends Codex streams with the latter and no [DONE]).
    """
    deadline = time.monotonic() + timeout
    body = {
        "model": PROBE_MODEL,
        "stream": True,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with: ok"}],
    }
    try:
        with http("/v1/chat/completions", body, timeout=30) as resp:
            for raw in resp:
                if is_terminal(raw):
                    return None, False
                if time.monotonic() > deadline:
                    return f"stream: not terminated within {timeout}s", True
    except urllib.error.HTTPError as exc:
        return f"stream: {exc}", exc.code in TRANSIENT_HTTP
    except urllib.error.URLError as exc:
        return f"stream: {exc}", isinstance(exc.reason, TimeoutError)
    except TimeoutError as exc:
        return f"stream: {exc}", True
    except (OSError, RuntimeError) as exc:
        return f"stream: {exc}", False
    return "stream: closed before a terminal chunk", False


def is_terminal(raw: bytes) -> bool:
    line = raw.strip()
    if not line.startswith(b"data: "):
        return False
    payload = line[6:]
    if payload == b"[DONE]":
        return True
    try:
        choices = json.loads(payload).get("choices") or []
    except ValueError:
        return False
    return any(c.get("finish_reason") for c in choices if isinstance(c, dict))


def verify(version: str, ready_timeout: float = 120) -> str | None:
    """Return None when healthy, else the first failing check."""
    deadline = time.monotonic() + ready_timeout
    seen = None
    while time.monotonic() < deadline:
        try:
            with http("/api/version") as resp:
                seen = json.load(resp).get("currentVersion")
            if seen == version:
                break
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(2)
    else:
        return f"version: expected {version}, saw {seen}"
    try:
        with http("/v1/models") as resp:
            if not json.load(resp).get("data"):
                return "models: empty list"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"models: {exc}"
    return stream_probe()


def tgz_version(tgz: Path) -> str:
    with tarfile.open(tgz) as tf:
        member = tf.extractfile("package/package.json")
        if member is None:
            raise ValueError(f"{tgz}: no package/package.json")
        return json.load(member)["version"]


def snapshot_db(dest: Path) -> None:
    """Consistent copy of the live DB (safe while the router is writing)."""
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def cmd_adopt(_args) -> int:
    if LINK.is_symlink():
        print(f"already adopted: {LINK} -> {live()}")
        return 0
    version = pkg_version(LINK)
    dest = release_dir(version)
    if dest.exists():
        print(f"refused: {dest} already exists")
        return 1
    dest.parent.mkdir(parents=True)
    os.rename(LINK, dest)  # same volume: a rename, open files stay valid
    LINK.symlink_to(dest)
    log(action="adopt", version=version, result="ok")
    return 0


def rollback_to(prev: Path, why: str, frm: str) -> int:
    switch(prev)
    restart()
    prev_version = pkg_version(prev)
    reason = verify(prev_version)
    log(
        action="rollback",
        version=prev_version,
        frm=frm,
        why=why,
        result="ok" if reason is None else f"FAILED: {reason}",
    )
    return 2 if reason is None else 3


def cmd_deploy(args) -> int:
    prev = live()
    if prev is None:
        print(f"refused: {LINK} is not a symlink; run `adopt` first")
        return 1
    tgz = Path(args.tgz).resolve()
    version = tgz_version(tgz)
    dest = release_dir(version)
    root = RELEASES / version
    if root.exists():
        print(f"refused: {root} exists; bump the version, never overwrite a release")
        return 1
    root.mkdir(parents=True)
    snapshot_db(root / "pre-deploy-data.sqlite")
    subprocess.run(
        ["npm", "install", "-g", "--prefix", str(root), str(tgz)], check=True
    )
    if pkg_version(dest) != version:
        print(f"refused: installed package is not {version}")
        return 1
    frm = pkg_version(prev)
    switch(dest)
    restart()
    reason = verify(version)
    if reason is None:
        log(action="deploy", version=version, frm=frm, result="ok")
        return 0
    log(action="deploy", version=version, frm=frm, result=f"FAILED: {reason}")
    return rollback_to(prev, reason, version)


def previous_version(current: str) -> str | None:
    """The version the last successful deploy of `current` replaced."""
    if not LOG.exists():
        return None
    found = None
    for line in LOG.read_text().splitlines():
        e = json.loads(line)
        if (
            e.get("action") == "deploy"
            and e.get("result") == "ok"
            and e.get("version") == current
        ):
            found = e.get("frm")
    return found


def cmd_rollback(args) -> int:
    cur = live()
    if cur is None:
        print(f"refused: {LINK} is not a symlink")
        return 1
    version = args.version or previous_version(pkg_version(cur))
    if version is None:
        print(
            "refused: no successful deploy of the live version in the log; pass a version"
        )
        return 1
    target = release_dir(version)
    if not target.is_dir():
        print(f"refused: no release {version} under {RELEASES}")
        return 1
    return 0 if rollback_to(target, "manual", pkg_version(cur)) == 2 else 3


def cmd_status(_args) -> int:
    cur = live()
    print(f"pointer: {LINK} -> {cur or 'NOT A SYMLINK (run adopt)'}")
    for d in sorted(RELEASES.glob("*")) if RELEASES.exists() else []:
        mark = "*" if cur and cur == release_dir(d.name) else " "
        print(f" {mark} {d.name}")
    try:
        with http("/api/version") as resp:
            print("serving:", json.load(resp).get("currentVersion"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("serving: UNREACHABLE", exc)
        return 1
    return 0


def cmd_probe(_args) -> int:
    cur = live() or LINK
    reason = verify(pkg_version(cur), ready_timeout=10)
    print("ok" if reason is None else f"FAIL {reason}")
    return 0 if reason is None else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("adopt").set_defaults(fn=cmd_adopt)
    d = sub.add_parser("deploy")
    d.add_argument("tgz")
    d.set_defaults(fn=cmd_deploy)
    r = sub.add_parser("rollback")
    r.add_argument("version", nargs="?")
    r.set_defaults(fn=cmd_rollback)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("probe").set_defaults(fn=cmd_probe)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
