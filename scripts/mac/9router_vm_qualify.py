#!/usr/bin/env python3
"""Qualify exact protocol-1 packages in Namespace; never run guest checks locally.

An isolated controller qualification scope is not a deployment receipt. Only
verified guest exports and both stopped/no-instance observations permit finish.

ponytail: one owned devbox per batch. SIGKILL and host power loss require an
external supervisor to stop compute and collect two metadata observations.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = os.environ.get("NINEROUTER_QUALIFY_HOST", "9router-test-vm.devbox.namespace")
QUALIFIED = Path.home() / ".9router" / "qualified"
QUALIFY_MODELS = os.environ.get(
    "NINEROUTER_QUALIFY_MODELS",
    "cx/gpt-5.4-mini,cc/claude-haiku-4-5-20251001,cx/gpt-5.4-mini(compact_200k)",
).split(",")
RUNTIME_SOURCES = {
    "controller": "scripts/mac/9router_hotswap.py",
    "deployer": "scripts/mac/9router_deploy.py",
    "worker": "scripts/mac/9router_worker.cjs",
    "managed": "src/lib/db/managed.cjs",
    "caddy_template": "scripts/mac/templates/9router.Caddyfile",
    "worker_template": "scripts/mac/templates/com.lfenergy.9router-worker.plist",
    "proxy_template": "scripts/mac/templates/com.lfenergy.9router-proxy.plist",
}
RUNTIME_KEYS = RUNTIME_SOURCES.keys() | {"node", "caddy"}
CADDY_URL = "https://github.com/caddyserver/caddy/releases/download/v2.11.4/caddy_2.11.4_mac_arm64.tar.gz"
CADDY_ARCHIVE_SHA256 = (
    "9efb0af2d6cf09cfb5053c0e51721b9b3d4956d346234f39368d943d25a3c9a7"
)
STALL_S = 5.0
CHECKS = {"continuity", "authentication", "recovery"}
BINDINGS = (
    "protocol",
    "sha256",
    "runtime_sha256",
    "source_commit",
    "source_patch_sha256",
    "namespace",
    "proxy_version",
    "guest_versions",
    "guest_binaries",
    "manifest_sha256",
    "persistenceFingerprint",
    "package_files",
    "run_id",
    "execution_id",
    "required_checks",
)
# Qualification never writes a passing receipt in the guest or its controller scope.


def in_vm() -> bool:
    result = subprocess.run(
        ["sysctl", "-n", "kern.hv_vmm_present"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def count(value, minimum=1):
    return type(value) is int and value >= minimum


def regular(path):
    require(not path.is_symlink() and path.is_file(), "regular evidence file required")
    require(path.stat().st_size > 0, "empty evidence file")
    return path


def evidence_name(value):
    require(
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is not None,
        "unsafe evidence filename",
    )
    return value


def validate_cleanup(cleanup):
    require(isinstance(cleanup, dict), "missing cleanup evidence")
    observations = cleanup.get("observations")
    require(
        cleanup.get("verified") is True
        and cleanup.get("stop_succeeded") is True
        and cleanup.get("connections_closed") is True
        and not cleanup.get("failure")
        and isinstance(observations, list)
        and len(observations) == 2,
        "cleanup incomplete",
    )
    for item in observations:
        require(
            isinstance(item, dict)
            and item.get("state") == "stopped"
            and "instance_id" in item
            and item["instance_id"] is None
            and type(item.get("at")) in {int, float}
            and math.isfinite(item["at"])
            and type(item.get("monotonic")) in {int, float}
            and math.isfinite(item["monotonic"]),
            "shutdown observation unreadable or reactivated",
        )
    require(
        observations[1]["at"] - observations[0]["at"] >= 60
        and observations[1]["monotonic"] - observations[0]["monotonic"] >= 60,
        "shutdown observations less than 60 seconds apart",
    )


def validate_continuity(measurement):
    transitions = measurement.get("transitions")
    require(
        isinstance(transitions, list) and len(transitions) >= 2,
        "continuity requires at least two transitions",
    )
    for index, item in enumerate(transitions):
        require(isinstance(item, dict), "invalid transition evidence")
        old, new = ("a", "b") if index % 2 == 0 else ("b", "a")
        require(
            item.get("from") == old and item.get("to") == new,
            "deploy and rollback transitions required",
        )
        for key, minimum in (
            ("new_http_completed", 100),
            ("old_sse_open_before", 1),
            ("old_ws_open_before", 1),
            ("old_sse_terminal", 1),
            ("old_ws_turns_after", 2),
        ):
            require(count(item.get(key), minimum), "continuity population incomplete")
        for key in ("errors", "truncations"):
            require(
                type(item.get(key)) is int and item[key] == 0,
                "healthy continuity failed",
            )
        require(
            count(item.get("proxy_pid_before"))
            and item.get("proxy_pid_after") == item["proxy_pid_before"]
            and type(item["proxy_pid_after"]) is int,
            "stable proxy identity unproven",
        )
    require(
        measurement.get("provider") == "deterministic-guest-fixture",
        "live-provider timing cannot prove deterministic continuity",
    )


def validate_measurement(name, measurement):
    require(isinstance(measurement, dict), "missing check measurement")
    if name == "continuity":
        validate_continuity(measurement)
    elif name == "authentication":
        require(
            count(measurement.get("live_streams_completed"))
            and count(measurement.get("concurrent_streams_completed"), 4)
            and count(measurement.get("routing_models"))
            and count(measurement.get("unauthorized_refusals"))
            and measurement.get("guest_credentials_only") is True,
            "live authentication/routing population incomplete",
        )
        require(
            count(measurement.get("concurrent_api_polls"))
            and type(measurement.get("concurrent_api_max_latency_s")) in {int, float}
            and math.isfinite(measurement["concurrent_api_max_latency_s"])
            and 0 <= measurement["concurrent_api_max_latency_s"] <= STALL_S,
            "concurrent API responsiveness unproven",
        )
        require(
            type(measurement.get("errors")) is int and measurement["errors"] == 0,
            "live authentication failed",
        )
    elif name == "recovery":
        require(
            count(measurement.get("intentional_crashes"))
            and count(measurement.get("recovered_requests"))
            and count(measurement.get("app_pid_before"))
            and count(measurement.get("app_pid_after"))
            and measurement["app_pid_before"] != measurement["app_pid_after"]
            and measurement.get("pid_source") == "private-worker-status",
            "intentional crash recovery population incomplete",
        )


def validate_export_identity(record, expected):
    require(
        isinstance(record, dict) and isinstance(expected, dict),
        "qualification identity unreadable",
    )
    identity = {key: record.get(key) for key in BINDINGS}
    require(
        all(key in expected for key in BINDINGS)
        and identity == {key: expected[key] for key in BINDINGS},
        "qualification identity differs from current run",
    )
    require(
        type(record.get("protocol")) is int and record["protocol"] == 1,
        "protocol-1 qualification required",
    )
    for key in ("sha256", "manifest_sha256", "source_patch_sha256"):
        require(digest(record.get(key)), "invalid artifact binding")
    runtime = record.get("runtime_sha256")
    require(
        isinstance(runtime, dict)
        and runtime.keys() == RUNTIME_KEYS
        and all(digest(value) for value in runtime.values()),
        "runtime bundle binding incomplete",
    )
    require(
        isinstance(record.get("source_commit"), str)
        and re.fullmatch(r"[a-f0-9]{40}", record["source_commit"]) is not None,
        "source revision binding incomplete",
    )
    namespace = record.get("namespace")
    require(
        isinstance(namespace, dict)
        and all(
            isinstance(namespace.get(key), str) and namespace[key]
            for key in ("devbox_id", "instance_id")
        ),
        "Namespace execution identity incomplete",
    )
    for key in ("run_id", "execution_id", "proxy_version", "persistenceFingerprint"):
        require(
            isinstance(record.get(key), str) and record[key],
            "runtime identity incomplete",
        )
    binaries = record.get("guest_binaries")
    validate_guest_binaries(binaries)
    require(
        binaries["caddy"]["version"].split()[0] == "v2.11.4", "guest Caddy pin differs"
    )
    require(
        all(runtime[name] == item["sha256"] for name, item in binaries.items())
        and record["proxy_version"] == binaries["caddy"]["version"],
        "exported runtime binary binding differs",
    )
    validate_export_versions(record, binaries)
    validate_export_package(record)
    return identity


def validate_export_versions(record, binaries):
    versions = record.get("guest_versions")
    require(
        isinstance(versions, dict)
        and all(
            versions.get(name) == item["version"] for name, item in binaries.items()
        )
        and all(
            isinstance(versions.get(key), str) and versions[key]
            for key in ("macos", "node", "python", "caddy")
        )
        and versions.get("architecture") == "arm64"
        and isinstance(versions.get("locked_dependencies"), dict)
        and versions["locked_dependencies"]
        and all(digest(value) for value in versions["locked_dependencies"].values()),
        "guest runtime identity incomplete",
    )


def validate_export_package(record):
    files = record.get("package_files")
    require(
        isinstance(files, dict)
        and files
        and all(
            isinstance(name, str)
            and name
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and digest(value)
            for name, value in files.items()
        ),
        "installed package binding incomplete",
    )


def verify_export(record, evidence: Path, expected):
    """Compare exported bytes to current-run identities and guest manifest bytes."""
    identity = validate_export_identity(record, expected)
    require(not evidence.is_symlink() and evidence.is_dir(), "unsafe evidence root")
    manifest_file = regular(evidence / "manifest.json")
    require(
        digest(expected.get("evidence_manifest_sha256"))
        and sha256(manifest_file) == expected["evidence_manifest_sha256"],
        "exported manifest differs from guest-observed bytes",
    )
    manifest = json.loads(manifest_file.read_text())
    require(
        isinstance(manifest, dict) and manifest.get("identity") == identity,
        "exported manifest execution binding differs",
    )
    exported = manifest.get("files")
    require(isinstance(exported, dict) and exported, "exported evidence manifest empty")
    for name, value in exported.items():
        path = evidence / evidence_name(name)
        require(
            name != "manifest.json"
            and digest(value)
            and sha256(regular(path)) == value,
            "exported evidence hash mismatch",
        )
    require(
        {"identity.json", "checks.json"}.issubset(exported),
        "required guest evidence missing",
    )
    require(
        json.loads((evidence / "identity.json").read_text()) == identity,
        "exported guest identity differs",
    )
    checks = record.get("checks")
    required_checks = record.get("required_checks")
    require(
        isinstance(required_checks, list)
        and required_checks
        and all(isinstance(name, str) for name in required_checks)
        and len(required_checks) == len(set(required_checks))
        and CHECKS.issubset(required_checks)
        and isinstance(checks, dict)
        and set(required_checks) == set(checks)
        and json.loads((evidence / "checks.json").read_text()) == checks,
        "missing or differing exported checks",
    )
    for name, check in checks.items():
        verify_exported_check(name, check, evidence, exported, identity)


def verify_exported_check(name, check, evidence, exported, identity):
    require(
        isinstance(check, dict)
        and type(check.get("exit_code")) is int
        and check["exit_code"] == 0
        and count(check.get("subjects")),
        "failed or zero-subject check",
    )
    filename = evidence_name(check.get("evidence_file"))
    logname = evidence_name(check.get("log_file"))
    require(
        filename in exported
        and logname in exported
        and digest(check.get("evidence_sha256"))
        and exported[filename] == check["evidence_sha256"],
        "check evidence is not exported or hash-bound",
    )
    measured = json.loads((evidence / filename).read_text())
    require(
        isinstance(measured, dict)
        and measured.get("identity") == identity
        and measured.get("exit_code") == check["exit_code"]
        and type(measured.get("exit_code")) is int
        and measured.get("subjects") == check["subjects"]
        and type(measured.get("subjects")) is int,
        "check measurement execution binding differs",
    )
    validate_measurement(name, measured.get("measurement"))


def finish(record, evidence, expected):
    require(
        record.get("guest_result") == "pass"
        and not record.get("failure")
        and not record.get("interrupted"),
        "guest qualification did not pass",
    )
    verify_export(record, evidence, expected)
    validate_cleanup(record.get("cleanup"))
    return {**record, "result": "pass"}


def stop_devbox(name):
    require(
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is not None,
        "exact devbox name required",
    )
    subprocess.run(
        ["devbox", "stop", name, "--force"],
        check=True,
        capture_output=True,
        timeout=120,
    )


def cleanup_action(cleanup, key, label, action, *args):
    try:
        action(*args)
        cleanup[key] = True
    except BaseException as error:
        cleanup["failure"] = label + ":" + type(error).__name__


def observe_shutdown(cleanup, name, devbox_id, metadata, wait, clock, now):
    first_observation_at = None
    for index in range(2):
        try:
            if index:
                while clock() - first_observation_at < 60:
                    wait(60 - (clock() - first_observation_at))
            else:
                first_observation_at = clock()
            observed = metadata(devbox_id)
            require(
                observed.get("id") == devbox_id and observed.get("name") == name,
                "shutdown devbox identity differs",
            )
            cleanup["observations"].append(
                {
                    "at": now(),
                    "monotonic": clock(),
                    "state": observed.get("state"),
                    **(
                        {"instance_id": observed["instance_id"]}
                        if "instance_id" in observed
                        else {}
                    ),
                }
            )
        except BaseException as error:
            cleanup["failure"] = "metadata:" + type(error).__name__
        finally:
            if not index:
                first_observation_at = clock()


def close_qualification(
    cleanup, name, devbox_id, metadata, close, stop, wait, clock, now
):
    cleanup_action(cleanup, "connections_closed", "connection-close", close)
    cleanup_action(cleanup, "stop_succeeded", "stop", stop, name)
    observe_shutdown(cleanup, name, devbox_id, metadata, wait, clock, now)
    cleanup["verified"] = not cleanup.get("failure")
    try:
        validate_cleanup(cleanup)
    except (ValueError, TypeError, KeyError):
        cleanup["verified"] = False


def qualification_lifecycle(
    name,
    devbox_id,
    work,
    metadata,
    close,
    *,
    stop=stop_devbox,
    wait=time.sleep,
    clock=time.monotonic,
    now=time.time,
):
    """The one outer boundary: work includes activation, setup, checks and export.

    Call only after acquiring exclusive lifecycle ownership. metadata MUST use
    Namespace devboxes.get, never SSH, exec, session listing, or a connection.
    A successful work callback is still incomplete until finish verifies bytes.
    """
    require(
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is not None
        and isinstance(devbox_id, str)
        and devbox_id,
        "exact devbox identity required",
    )
    record = {"result": "incomplete", "guest_result": "unrun"}
    cleanup = {
        "verified": False,
        "observations": [],
        "stop_succeeded": False,
        "connections_closed": False,
    }
    owns = False
    try:
        before = metadata(devbox_id)
        require(
            before.get("id") == devbox_id
            and before.get("name") == name
            and before.get("state") == "stopped"
            and "instance_id" in before
            and before["instance_id"] is None,
            "devbox identity or pre-activation stopped state unproven",
        )
        owns = True  # An uncertain activation failure still requires stop.
        value = work()
        require(isinstance(value, dict), "guest outcome unreadable")
        record.update(value)
        record["result"] = "incomplete"
    except BaseException as error:
        record["failure"] = type(
            error
        ).__name__  # Never emit credential-bearing errors.
    finally:
        if owns:
            close_qualification(
                cleanup, name, devbox_id, metadata, close, stop, wait, clock, now
            )
        record["cleanup"] = cleanup
        if record.get("failure") or not cleanup["verified"]:
            record["result"] = "fail"
    return record


def package_preflight(path):
    """Read only; refuse unsafe archives before any installer or package helper."""
    regular(path)
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [str(Path(item.name)) for item in members]
        require(members and len(names) == len(set(names)), "duplicate package paths")
        for item in members:
            parts = Path(item.name).parts
            require(
                parts
                and parts[0] == "package"
                and ".." not in parts
                and not Path(item.name).is_absolute()
                and "\\" not in item.name
                and (item.isfile() or item.isdir())
                and (len(parts) > 1 or item.isdir()),
                "unsafe package entry",
            )
        stream = archive.extractfile("package/app/hotswap-manifest.json")
        require(stream is not None, "managed manifest missing")
        with stream:
            manifest = json.load(stream)
        require(
            isinstance(manifest, dict)
            and type(manifest.get("protocol")) is int
            and manifest["protocol"] == 1,
            "protocol-1 package required",
        )
    return sha256(path)


ROOT = HERE.parents[1]
GUEST_ROOT = Path("/Volumes/devbox/9router/qualification")
BASELINE = Path("/Volumes/devbox/9router/baseline.json")
TEST_LOCK = Path(
    "docs/superpowers/evidence/task-2-retry-f76093a4f8ec-de12e533/tests-package-lock.json"
)
TASK2_MODULES = (
    "managed-buffer",
    "managed-detached",
    "managed-worker",
    "background-token-refresh",
    "quota-auto-ping",
    "cli-build-artifacts",
    "custom-server-peer-headers",
    "managed-credentials",
    "request-details-metadata-mode",
    "responses-mid-turn-steering",
)
TASK3_MODULES = (
    "managed-review-batch",
    "managed-credentials",
    "db-driver-chain",
    "db-migration-chain",
    "token-refresh-generic",
    "codex-refresh-token",
)
MAC_MODULES = (
    "test_9router_deploy.py",
    "test_9router_hotswap.py",
    "test_9router_vm_qualify.py",
    "test_9router_path_watchdog.py",
    "test_9router_devbox_restore.py",
    "test_9router_worker_templates.py",
)
NATIVE = (
    ("proxy", ["node", "tests/mac/9router_hotswap.check.cjs"], "PASS", 240),
    (
        "proxy-counterfactual",
        ["node", "tests/mac/9router_hotswap_counterfactual.check.cjs"],
        "PASS: routing counterfactual",
        360,
    ),
    (
        "worker",
        ["node", "tests/mac/9router_worker.check.cjs"],
        "PASS: real pinned workers",
        900,
    ),
    (
        "cleanup",
        [
            "node",
            "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON",
            "tests/unit/managed-cleanup.check.mjs",
        ],
        "PASS",
        180,
    ),
    (
        "dispatch",
        ["node", "tests/unit/managed-dispatch.check.mjs"],
        "GREEN: AST/import-aware census",
        180,
    ),
    (
        "state",
        ["node", "tests/unit/managed-state.check.mjs"],
        "GREEN: layout/metadata/fingerprint refusal",
        240,
    ),
    (
        "refresh",
        ["node", "tests/unit/token-refresh-cross-process.check.mjs"],
        "GREEN: two-process single-use refresh",
        240,
    ),
    (
        "cas",
        ["node", "tests/unit/managed-cas-process.check.mjs"],
        "GREEN: actual repo OAuth/Copilot CAS",
        240,
    ),
    (
        "review-counterfactual",
        ["node", "tests/unit/managed-review-counterfactual.check.mjs"],
        "RED: baseline mirror fails all eight named review mechanisms while pending/uncertain safety passes",
        360,
    ),
)
SDK_METADATA = """
const { createRequire } = require('node:module');
const req = createRequire(process.argv[1]);
const { createDevboxClient } = req('@namespacelabs/sdk');
(async () => {
  const client = createDevboxClient({ connectionTimeoutMs: 15000 });
  try {
    const box = await client.devboxes.get(process.argv[2], { timeoutMs: 15000 });
    console.log(JSON.stringify({id:box.id,name:box.name,state:box.info.state,
      instance_id:box.info.instanceId || null,os:box.info.shape.os,
      architecture:box.info.shape.architecture}));
  } finally { client.close(); }
})().catch(() => { process.exitCode = 1; });
"""


def private_json(path):
    regular(path)
    info = path.stat()
    require(
        info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1,
        "private owned JSON required",
    )
    value = json.loads(path.read_text())
    require(isinstance(value, dict), "JSON object required")
    return value


def new_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def output(argv, cwd=None, env=None, timeout=30):
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def controller_module():
    spec = importlib.util.spec_from_file_location(
        "qualification_controller", HERE / "9router_hotswap.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guest_guard():
    require(
        in_vm() and sys.platform == "darwin" and output(["uname", "-m"]) == "arm64",
        "isolated ARM64 macOS guest required",
    )


def safe_run_id(value):
    require(
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value) is not None,
        "unsafe qualification run ID",
    )
    return value


def isolated_environment(home, source=None):
    # Never inherit provider keys, production paths, NODE_OPTIONS,
    # or module search paths.
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = home / "tmp"
    temporary.mkdir(exist_ok=True, mode=0o700)
    env = {
        "PATH": os.environ.get(
            "PATH", "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        ),
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "DATA_DIR": str(home / ".9router"),
        "CI": "true",
        "NODE_ENV": "production",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NEXT_TELEMETRY_DISABLED": "1",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_cache": str(home / "npm-cache"),
    }
    if source is not None:
        env["NINEROUTER_TEST_PACKAGES"] = str(source)
    return env


def source_runtime_hashes(source):
    return {
        name: sha256(regular(source / relative))
        for name, relative in RUNTIME_SOURCES.items()
    }


def validate_guest_binaries(binaries):
    require(
        isinstance(binaries, dict) and binaries.keys() == {"node", "caddy"},
        "guest binary binding incomplete",
    )
    for item in binaries.values():
        require(
            isinstance(item, dict)
            and item.keys() == {"path", "sha256", "version"}
            and isinstance(item["path"], str)
            and Path(item["path"]).is_absolute()
            and ".." not in Path(item["path"]).parts
            and not any(c in item["path"] for c in "\x00\r\n:")
            and digest(item["sha256"])
            and isinstance(item["version"], str)
            and item["version"]
            and item["version"] == item["version"].strip()
            and not any(c in item["version"] for c in "\x00\r\n"),
            "guest binary identity incomplete",
        )


def guest_binary_bindings():
    binaries = {}
    for name, argument in (("node", "--version"), ("caddy", "version")):
        found = shutil.which(name)
        require(found, "missing guest runtime executable: " + name)
        path = regular(Path(found).resolve())
        require(os.access(path, os.X_OK), "guest runtime is not executable")
        binaries[name] = {
            "path": str(path),
            "sha256": sha256(path),
            "version": output([str(path), argument]),
        }
    validate_guest_binaries(binaries)
    require(
        binaries["caddy"]["version"].split()[0] == "v2.11.4", "guest Caddy pin differs"
    )
    return binaries


def provision_guest_caddy(tools):
    tools.parent.mkdir(mode=0o700, exist_ok=True)
    tools.mkdir(mode=0o700, exist_ok=True)
    for directory in (tools.parent, tools):
        require(
            directory.stat().st_uid == os.getuid()
            and stat.S_IMODE(directory.stat().st_mode) == 0o700,
            "private guest tool directory required",
        )
    archive_path = tools / "caddy.tgz"
    with tempfile.TemporaryDirectory(prefix=".caddy-", dir=tools) as temporary:
        staged = Path(temporary)
        if not os.path.lexists(archive_path):
            with (
                urllib.request.urlopen(CADDY_URL, timeout=120) as incoming,
                (staged / "archive").open("xb") as outgoing,
            ):
                shutil.copyfileobj(incoming, outgoing)
            (staged / "archive").chmod(0o600)
            require(
                sha256(staged / "archive") == CADDY_ARCHIVE_SHA256,
                "guest Caddy archive digest differs",
            )
            os.replace(staged / "archive", archive_path)
        info = regular(archive_path).stat()
        require(
            info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1,
            "private guest Caddy archive required",
        )
        require(
            sha256(archive_path) == CADDY_ARCHIVE_SHA256,
            "guest Caddy archive digest differs",
        )
        with tarfile.open(archive_path, "r:gz") as archive:
            members = [
                member for member in archive.getmembers() if member.name == "caddy"
            ]
            require(
                len(members) == 1 and members[0].isfile(),
                "guest Caddy archive entry differs",
            )
            with (
                archive.extractfile(members[0]) as incoming,
                (staged / "caddy").open("xb") as outgoing,
            ):
                shutil.copyfileobj(incoming, outgoing)
        expected = sha256(staged / "caddy")
        binary = tools / "caddy"
        if not os.path.lexists(binary):
            (staged / "caddy").chmod(0o700)
            os.replace(staged / "caddy", binary)
        require(
            sha256(regular(binary)) == expected
            and binary.stat().st_uid == os.getuid()
            and stat.S_IMODE(binary.stat().st_mode) == 0o700
            and binary.stat().st_nlink == 1,
            "persistent guest Caddy binary differs",
        )


def cmd_runtime(args):
    guest_guard()
    tools = Path(args.tools)
    require(
        tools.resolve() == tools
        and tools == GUEST_ROOT / "tools" / CADDY_ARCHIVE_SHA256,
        "guest tools must use pinned persistent qualification storage",
    )
    if os.path.lexists(tools / "caddy") or not shutil.which("caddy"):
        provision_guest_caddy(tools)
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")
    print(json.dumps(guest_binary_bindings(), sort_keys=True))
    return 0


def bind_guest_runtime(binding, binaries):
    validate_guest_binaries(binaries)
    static = binding["source_runtime_sha256"]
    require(
        isinstance(static, dict)
        and static.keys() == RUNTIME_SOURCES.keys()
        and all(digest(value) for value in static.values()),
        "static runtime binding incomplete",
    )
    require(
        binaries["caddy"]["version"].split()[0] == "v2.11.4", "guest Caddy pin differs"
    )
    binding.update(
        guest_binaries=binaries,
        proxy_version=binaries["caddy"]["version"],
        runtime_sha256={
            **static,
            **{name: item["sha256"] for name, item in binaries.items()},
        },
    )


def runtime_preflight(binding, run_root):
    binaries = binding["guest_binaries"]
    validate_guest_binaries(binaries)
    require(
        source_runtime_hashes(ROOT) == binding["source_runtime_sha256"],
        "guest static runtime differs from staged source",
    )
    directory = run_root / "bin"
    directory.mkdir(mode=0o700, exist_ok=True)
    require(
        directory.resolve() == directory and not directory.is_symlink(),
        "guest runtime PATH is not concrete",
    )
    for name, item in binaries.items():
        path = Path(item["path"])
        require(path.resolve() == path, "bound guest binary path is not concrete")
        alias = directory / name
        if not alias.is_symlink() and not alias.exists():
            alias.symlink_to(path)
        require(
            alias.is_symlink() and alias.resolve() == path,
            "guest runtime PATH binding differs",
        )
    os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
    require(
        guest_binary_bindings() == binaries,
        "guest binaries differ from pre-test observation",
    )
    require(
        controller_module().runtime_hashes() == binding["runtime_sha256"]
        and binding["proxy_version"] == binaries["caddy"]["version"],
        "qualification runtime differs from pre-test binding",
    )


def source_binding(source, binding):
    require(
        output(["git", "rev-parse", "HEAD"], source) == binding["source_commit"],
        "source revision changed",
    )
    for name, expected in binding["source_files"].items():
        path = Path(name)
        require(
            not path.is_absolute() and ".." not in path.parts and digest(expected),
            "unsafe source binding",
        )
        require(sha256(regular(source / path)) == expected, "source bytes changed")
    patch_path = source / "qualification.patch"
    require(
        patch_path.is_file()
        and not patch_path.is_symlink()
        and sha256(patch_path) == binding["source_patch_sha256"],
        "enumerated source patch changed",
    )
    require(
        package_preflight(Path(binding["tgz"])) == binding["sha256"],
        "exact tarball changed",
    )


def copy_scoped_tarball(tgz, scoped_tarball, binding):
    if scoped_tarball == tgz:
        return
    if not scoped_tarball.exists():
        with regular(tgz).open("rb") as incoming, scoped_tarball.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        scoped_tarball.chmod(0o600)
    require(
        sha256(regular(scoped_tarball)) == binding["sha256"],
        "persistent scoped tarball differs",
    )


def archive_package_hashes(archive):
    package_files = {}
    for member in archive.getmembers():
        if member.isfile():
            with archive.extractfile(member) as stream:
                package_files[Path(member.name).relative_to("package").as_posix()] = (
                    hashlib.file_digest(stream, "sha256").hexdigest()
                )
    return package_files


def install_scoped_release(archive, release):
    release.mkdir(parents=True, mode=0o700)
    for member in archive.getmembers():
        relative = Path(member.name).relative_to("package")
        target = release / relative
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with archive.extractfile(member) as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        target.chmod(0o700 if member.mode & 0o111 else 0o600)


def verify_scoped_release(archive, release):
    require(release.resolve() == release, "installed release is not concrete")
    for member in archive.getmembers():
        if member.isfile():
            target = regular(release / Path(member.name).relative_to("package"))
            with archive.extractfile(member) as stream:
                require(
                    sha256(target) == hashlib.file_digest(stream, "sha256").hexdigest(),
                    "existing release differs; never overwrite",
                )


def write_scope(scope_path, scope, home, run_id):
    if not scope_path.exists():
        new_json(scope_path, scope)
        return
    previous_scope = private_json(scope_path)
    require(
        previous_scope.get("home") == str(home)
        and previous_scope.get("run_id") == run_id
        and previous_scope.get("phase") == "qualification"
        and "result" not in previous_scope,
        "persistent baseline scope differs",
    )
    for old_digest, old_package in previous_scope.get("packages", {}).items():
        if old_digest in scope["packages"]:
            continue
        old_tarball = Path(old_package["tarball"])
        require(
            old_tarball.is_absolute()
            and old_tarball.resolve() == old_tarball
            and home.parent in old_tarball.parents
            and digest(old_digest)
            and sha256(regular(old_tarball)) == old_digest,
            "referenced baseline tarball changed",
        )
        scope["packages"][old_digest] = old_package
    temporary = home / (".scope-" + uuid.uuid4().hex + ".json")
    new_json(temporary, scope)
    os.replace(temporary, scope_path)


def prepare_scope(tgz, binding, *, run_id=None, install=True):
    guest_guard()
    run_id = safe_run_id(run_id or binding["run_id"])
    home = GUEST_ROOT / run_id / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(
        home.resolve() == home and not home.is_symlink(),
        "qualification HOME is not concrete",
    )
    module = controller_module()
    module.private_directory(home)
    state = home / ".9router"
    runtime = Path("/tmp") / ("9rq-" + hashlib.sha256(run_id.encode()).hexdigest()[:12])
    if install:
        for suffix in ("releases", "db", "hotswap", "qualified", "mitm"):
            (state / suffix).mkdir(parents=True, exist_ok=True, mode=0o700)
            module.private_directory(state / suffix)
        runtime.mkdir(mode=0o700, exist_ok=True)
        module.private_directory(runtime)
    else:
        require(
            not state.exists() and not runtime.exists(),
            "fresh fixture state/runtime required",
        )
    require(package_preflight(tgz) == binding["sha256"], "candidate tarball mismatch")
    scoped_tarball = home.parent / (binding["sha256"] + ".tgz")
    copy_scoped_tarball(tgz, scoped_tarball, binding)
    with tarfile.open(tgz, "r:gz") as archive:
        package = json.load(archive.extractfile("package/package.json"))
        version = package.get("version")
        require(
            isinstance(version, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version),
            "unsafe package version",
        )
        release = state / "releases" / version / "lib/node_modules/9router"
        # Fresh packaged fixtures bind both slots.
        # Persistent live baselines keep one release.
        destinations = (
            {"release": str(release)}
            if install
            else {
                "installations": {
                    slot: str(
                        state / "releases" / version / slot / "lib/node_modules/9router"
                    )
                    for slot in module.PORTS
                }
            }
        )
        manifest_stream = archive.extractfile("package/app/hotswap-manifest.json")
        require(manifest_stream is not None, "package manifest missing")
        with manifest_stream:
            manifest_bytes = manifest_stream.read()
        manifest = json.loads(manifest_bytes)
        package_files = archive_package_hashes(archive)
        if install:
            if release.exists():
                verify_scoped_release(archive, release)
            else:
                install_scoped_release(archive, release)
    if install:
        module.concrete_release(release, releases=state / "releases")
        require(
            module.installed_package_hashes(release) == package_files,
            "installed package population or bytes differ",
        )
    require(
        module.runtime_hashes() == binding["runtime_sha256"],
        "qualification runtime differs",
    )
    scope = {
        "protocol": 1,
        "phase": "qualification",
        "run_id": run_id,
        "namespace": binding["namespace"],
        "home": str(home),
        "source_root": str(ROOT),
        "runtime": str(runtime),
        "source_commit": binding["source_commit"],
        "source_patch_sha256": binding["source_patch_sha256"],
        "runtime_sha256": binding["runtime_sha256"],
        "proxy_version": binding["proxy_version"],
        "packages": {
            binding["sha256"]: {
                **destinations,
                "tarball": str(scoped_tarball),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "persistenceFingerprint": manifest["persistenceFingerprint"],
                "package_files": package_files,
            }
        },
    }
    scope_path = home / "scope.json"
    # Preserve the persistent baseline database and referenced releases.
    write_scope(scope_path, scope, home, run_id)
    return scope_path, scope, manifest


def initialize_baseline(scope, manifest):
    home = Path(scope["home"])
    state = home / ".9router"
    database = state / "db/data.sqlite"
    env = state / "env.sh"
    require(
        database.exists() == env.exists(), "partial baseline requires explicit recovery"
    )
    if database.exists():
        regular(database)
        regular(env)
        require(
            all(
                path.stat().st_uid == os.getuid()
                and stat.S_IMODE(path.stat().st_mode) == 0o600
                and path.stat().st_nlink == 1
                for path in (database, env)
            ),
            "baseline database/environment are not private owned files",
        )
        return  # Never reset guest-owned provider credentials.
    environment = {
        "DATA_DIR": str(state),
        "JWT_SECRET": secrets.token_hex(32),
        "API_KEY_SECRET": secrets.token_hex(32),
        "MACHINE_ID_SALT": secrets.token_hex(32),
        "INITIAL_PASSWORD": secrets.token_hex(24),
        "NODE_ENV": "production",
        "REQUEST_DETAILS_MODE": "disabled",
    }
    with env.open("x") as stream:
        env.chmod(0o600)
        for key, value in environment.items():
            stream.write(f'export {key}="{value}"\n')
    # The exact manifest supplies native SQLite schema.
    # No production database is copied.
    database.touch(mode=0o600, exist_ok=False)
    with sqlite3.connect(database) as db:
        for kind in ("table", "index", "view", "trigger"):
            for row in manifest["layout"]:
                if row["type"] == kind:
                    db.execute(row["sql"])
        db.execute(
            "INSERT INTO _meta(key,value) VALUES(?,?)",
            ("schemaVersion", str(manifest["migrationVersion"])),
        )
        db.execute(
            "INSERT INTO _meta(key,value) VALUES(?,?)",
            ("backupSchemaVersion", str(manifest["schemaVersion"])),
        )
        db.execute(
            "INSERT INTO settings(id,data) VALUES(1,?)",
            (
                json.dumps(
                    {
                        "requireLogin": True,
                        "tunnelEnabled": False,
                        "tailscaleEnabled": False,
                        "mitmEnabled": False,
                        "requestDetailsMode": "disabled",
                    }
                ),
            ),
        )


def controller_argv(scope_path, *arguments):
    return [
        sys.executable,
        str(HERE / "9router_hotswap.py"),
        "--qualification-scope",
        str(scope_path),
        *arguments,
    ]


def controller_call(scope_path, *arguments):
    scope = private_json(scope_path)
    guest_guard()
    return output(
        controller_argv(scope_path, *arguments),
        ROOT,
        isolated_environment(Path(scope["home"])),
        timeout=180,
    )


def cmd_baseline(args):
    guest_guard()
    os.umask(0o077)
    binding = private_json(Path(args.input))
    source_binding(ROOT, binding)
    provision_dependencies(binding, Path(args.input).parent)
    scope_path, scope, manifest = prepare_scope(Path(args.tgz), binding)
    initialize_baseline(scope, manifest)
    if BASELINE.exists():
        previous = private_json(BASELINE)
        require(
            previous.get("run_id") == scope["run_id"]
            and previous.get("scope") == str(scope_path)
            and previous.get("devbox_id") == binding["namespace"]["devbox_id"],
            "another guest-owned baseline exists; never replace its identity",
        )
    package = scope["packages"][binding["sha256"]]
    state = Path(scope["home"]) / ".9router/hotswap/state.json"
    if state.exists():
        controller_call(scope_path, "restore")
    else:
        controller_call(
            scope_path,
            "enroll",
            "--acknowledge-maintenance",
            "--release",
            package["release"],
            "--digest",
            binding["sha256"],
        )
    if not BASELINE.exists():
        new_json(
            BASELINE,
            {
                "protocol": 1,
                "run_id": scope["run_id"],
                "devbox_id": scope["namespace"]["devbox_id"],
                "credential_origin": "guest-owned",
                "scope": str(scope_path),
            },
        )
    print(
        "Guest baseline ready; existing guest-owned provider logins "
        "are required for live qualification."
    )
    return 0


def cmd_restore(args):
    guest_guard()
    scope_path = Path(args.scope)
    scope = private_json(scope_path)
    require(
        scope.get("phase") == "qualification" and "result" not in scope,
        "isolated qualification scope required",
    )
    controller_call(
        scope_path, "restore"
    )  # Controller recreates sockets/plists, then proxy.
    print(
        "Restored isolated managed slots; provider database "
        "and referenced releases retained."
    )
    return 0


def record_check(evidence, identity, checks, name, subjects, measurement, log):
    require(count(subjects), "zero-subject guest check")
    filename, logname = evidence_name(name + ".json"), evidence_name(name + ".log")
    validate_measurement(name, measurement)
    new_json(
        evidence / filename,
        {
            "identity": identity,
            "exit_code": 0,
            "subjects": subjects,
            "measurement": measurement,
        },
    )
    with (evidence / logname).open("x") as stream:
        (evidence / logname).chmod(0o600)
        stream.write(log or "Completed measured guest check.\n")
    checks[name] = {
        "exit_code": 0,
        "subjects": subjects,
        "evidence_file": filename,
        "evidence_sha256": sha256(evidence / filename),
        "log_file": logname,
    }


class GuestCheckFailure(RuntimeError):
    """Carry sanitized deterministic check output, never provider exceptions."""


def guest_command(argv, cwd, env, timeout, *, expected_exit=0):
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        text, _ = child.communicate(timeout=timeout)
        if child.returncode != expected_exit:
            sanitized = re.sub(
                r"\b(?:nsct_|ghp_|github_pat_|sk-)[A-Za-z0-9_.-]+", "[REDACTED]", text
            )
            sanitized = re.sub(
                r"(?i)(access_token|refresh_token|api_key|password|authorization)([\"'\s:=]+)[^\s,\"'}]+",
                r"\1\2[REDACTED]",
                sanitized,
            )
            raise GuestCheckFailure(
                "guest command exited " + str(child.returncode) + ": " + sanitized
            )
        require(text.strip(), "guest command produced no evidence")
        return text
    finally:
        # Only this check's session group is owned; descendants may outlive its leader.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if child.poll() is None:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.killpg(child.pid, signal.SIGKILL)
            require(False, "owned check descendants outlived graceful cleanup")


def provision_dependencies(binding, run_root):
    locks = {}
    test_lock = ROOT / "tests/package-lock.json"
    if not test_lock.exists():
        incoming = regular(ROOT / TEST_LOCK)
        require(
            json.loads(incoming.read_text())["packages"][""]["devDependencies"]
            == json.loads((ROOT / "tests/package.json").read_text())["devDependencies"],
            "supplementary test lock differs from manifest",
        )
        shutil.copyfile(incoming, test_lock)
    env = isolated_environment(run_root / "tools-home", ROOT)
    env.pop(
        "NODE_ENV"
    )  # esbuild's locked native optional package needs dev resolution.
    logs = []
    for relative in (Path("."), Path("tests"), Path("cli")):
        folder = ROOT / relative
        package = json.loads(regular(folder / "package.json").read_text())
        lock_path = regular(folder / "package-lock.json")
        lock = json.loads(lock_path.read_text())
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            require(
                package.get(key, {}) == lock["packages"][""].get(key, {}),
                "dependency lock mismatch",
            )
        key = (relative / "package-lock.json").as_posix()
        locks[key] = sha256(lock_path)
        logs.append(
            guest_command(
                [
                    "npm",
                    "ci",
                    "--ignore-scripts",
                    "--include=dev",
                    "--include=optional",
                    "--no-audit",
                    "--no-fund",
                ],
                folder,
                env,
                900,
            )
        )
        require(sha256(lock_path) == locks[key], "dependency installer changed lock")
    venv = run_root / "python-tools"
    output([sys.executable, "-m", "venv", str(venv)], env=env, timeout=60)
    python = str(venv / "bin/python")
    logs.append(
        guest_command(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "pytest==8.4.2",
                "ruff==0.15.18",
            ],
            ROOT,
            env,
            240,
        )
    )
    require(
        output(
            [
                python,
                "-c",
                "import importlib.metadata as m; "
                'assert m.version("pytest")=="8.4.2"; '
                'assert m.version("ruff")=="0.15.18"; print("locked tools")',
            ],
            env=env,
        ),
        "Python tool identity absent",
    )
    source_binding(ROOT, binding)
    return locks, python, "\n".join(logs)


def unit_population(path, modules, minimum):
    report = json.loads(regular(path).read_text())
    require(
        report.get("success") is True
        and count(report.get("numTotalTests"), minimum)
        and report.get("numPendingTests") == 0
        and report.get("numFailedTests") == 0
        and report.get("numTodoTests", 0) == 0,
        "unit check failed, skipped, or empty",
    )
    suites = report.get("testResults", [])
    require(
        len(suites) == len(modules)
        and {Path(item["name"]).name for item in suites}
        == {name + ".test.js" for name in modules}
        and all(
            item.get("status") == "passed"
            and item.get("assertionResults")
            and all(case.get("status") == "passed" for case in item["assertionResults"])
            for item in suites
        ),
        "unit module population differs",
    )
    return report["numTotalTests"]


PREREQUISITES = {name for name, *_ in NATIVE} | {
    "dependencies",
    "task2-unit",
    "task3-unit",
    "task4b-unit",
    "mac-unit",
    "h2c-head",
    "h2c-managed",
    "h2c-baseline",
    "h2c-counterfactual",
    "syntax",
}
SYNTAX_FILES = (
    "scripts/mac/9router_worker.cjs",
    "custom-server.js",
    "src/lib/db/managed.cjs",
    "src/lib/db/repos/settingsRepo.js",
    "src/shared/services/initializeApp.js",
    "tests/mac/9router_worker.check.cjs",
    "tests/mac/9router_hotswap_counterfactual.check.cjs",
    "tests/unit/managed-worker.test.js",
    "tests/unit/managed-credentials.test.js",
    "cli/scripts/build-cli.js",
    "tests/unit/cli-build-artifacts.test.js",
)


def prerequisite_native_checks(binding, env, evidence, identity, checks):
    for name, argv, marker, timeout in NATIVE:
        source_binding(ROOT, binding)
        log = guest_command(argv, ROOT, env, timeout)
        require(
            marker in log and not re.search(r"\b(?:SKIP|TODO)\b", log),
            "native proof identity absent",
        )
        if name == "proxy-counterfactual":
            require(
                "GREEN: unchanged byte-identical mirror" in log
                and "RED: removing only" in log,
                "route mutation proof incomplete",
            )
        record_check(
            evidence,
            identity,
            checks,
            name,
            1,
            {"population": "named native proof commands"},
            log,
        )


def prerequisite_checks(binding, run_root, evidence, identity, checks, python):
    env = isolated_environment(run_root / "checks-home", ROOT)
    # Native checks make private temporary directories; keep Darwin socket paths short.
    env["TMPDIR"] = "/tmp"
    config = run_root / "vitest.config.mjs"
    config.write_text(
        'import path from "node:path"; '
        'import {createRequire} from "node:module"; const root='
        + json.dumps(str(ROOT))
        + (
            '; const req=createRequire(path.join(root,"packag'
            'e.json")); export default {root,test:{environmen'
            't:"node",globals:true},resolve:{alias:[{find:"op'
            'en-sse",replacement:path.join(root,"open-sse")},'
            '{find:"@",replacement:path.join(root,"src")},{fi'
            'nd:"vitest",replacement:path.join(root,"tests/no'
            'de_modules/vitest/dist/index.js")},...["uuid","s'
            'ql.js","undici","bcryptjs","node-machine-id"].ma'
            "p(name=>({find:name,replacement:req.resolve(name"
            ")}))]}};"
        )
    )
    prerequisite_native_checks(binding, env, evidence, identity, checks)
    for name, modules, minimum in (
        ("task2-unit", TASK2_MODULES, 66),
        ("task3-unit", TASK3_MODULES, 45),
        ("task4b-unit", ("managed-update",), 8),
    ):
        report = evidence / (name + "-report.json")
        argv = [
            "node",
            "tests/node_modules/vitest/vitest.mjs",
            "run",
            "--config",
            str(config),
            *["tests/unit/" + module + ".test.js" for module in modules],
            "--reporter=json",
            "--outputFile=" + str(report),
        ]
        log = guest_command(argv, ROOT, env, 600)
        record_check(
            evidence,
            identity,
            checks,
            name,
            unit_population(report, modules, minimum),
            {"modules": list(modules)},
            log,
        )
    syntax_log = []
    for filename in SYNTAX_FILES:
        source_binding(ROOT, binding)
        output(["node", "--check", filename], ROOT, env, 30)
        syntax_log.append("Checked guest source syntax: " + filename)
    record_check(
        evidence,
        identity,
        checks,
        "syntax",
        len(SYNTAX_FILES),
        {"population": "guest JavaScript syntax files", "files": list(SYNTAX_FILES)},
        "\n".join(syntax_log),
    )
    xml = evidence / "mac-tests.xml"
    log = guest_command(
        [
            python,
            "-m",
            "pytest",
            *["tests/mac/" + name for name in MAC_MODULES],
            "-v",
            "--junitxml=" + str(xml),
        ],
        ROOT,
        env,
        1200,
    )
    cases = list(ET.parse(xml).getroot().iter("testcase"))
    require(
        cases
        and all(
            not list(case.iter("failure"))
            and not list(case.iter("error"))
            and not list(case.iter("skipped"))
            for case in cases
        ),
        "mac check skipped or failed",
    )
    require(
        all(
            any(name.removesuffix(".py") in case.get("classname", "") for case in cases)
            for name in MAC_MODULES
        ),
        "missing mac test module",
    )
    record_check(
        evidence,
        identity,
        checks,
        "mac-unit",
        len(cases),
        {"modules": list(MAC_MODULES)},
        log,
    )
    for name, filename, minimum in (
        ("h2c-head", "custom-server-h2c.test.cjs", 5),
        ("h2c-managed", "custom-server-h2c-managed.test.cjs", 1),
    ):
        log = guest_command(
            [
                "node",
                "--test",
                "--test-reporter=tap",
                "--test-timeout=10000",
                "tests/unit/" + filename,
            ],
            ROOT,
            env,
            60,
        )
        for field, amount in (
            ("tests", minimum),
            ("pass", minimum),
            ("fail", 0),
            ("cancelled", 0),
            ("skipped", 0),
            ("todo", 0),
        ):
            require(
                re.search(r"^# " + field + " " + str(amount) + "$", log, re.M),
                "h2c TAP population incomplete",
            )
        record_check(
            evidence,
            identity,
            checks,
            name,
            minimum,
            {"population": "h2c regression cases"},
            log,
        )
    # Baseline and pre-fix mirrors are separate checkouts.
    # Never modify the candidate.
    for name, revision, original in (
        ("h2c-baseline", "5fd82262218f5f00b4f987587318908e548ad65a", True),
        ("h2c-counterfactual", "e08fa1a43d89caeefa880a612db445c68d578d01", False),
    ):
        mirror = run_root / name
        (mirror / "tests/unit").mkdir(parents=True, mode=0o700)
        (mirror / "custom-server.js").write_text(
            output(["git", "show", revision + ":custom-server.js"], ROOT) + "\n"
        )
        test = mirror / "tests/unit/custom-server-h2c.test.cjs"
        test.write_text(
            output(
                ["git", "show", revision + ":tests/unit/custom-server-h2c.test.cjs"],
                ROOT,
            )
            + "\n"
            if original
            else (ROOT / "tests/unit/custom-server-h2c.test.cjs").read_text()
        )
        log = guest_command(
            [
                "node",
                "--test",
                "--test-reporter=tap",
                "--test-timeout=10000",
                str(test),
            ],
            mirror,
            env,
            60,
            expected_exit=1,
        )
        require(
            "test timed out" in log
            if original
            else "h2c fallback response timed out" in log,
            "h2c mirror did not reproduce the specific assertion",
        )
        record_check(
            evidence,
            identity,
            checks,
            name,
            1,
            {"expected_exit_code": 1, "population": "specific failing h2c mirrors"},
            log,
        )
    source_binding(ROOT, binding)


def check_concurrent(stream, models, base, version):
    polls, worst = 0, 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(stream, models[index % len(models)]) for index in range(4)
        ]
        while any(not future.done() for future in futures):
            running = any(future.running() for future in futures)
            started = time.monotonic()
            with urllib.request.urlopen(
                base + "/api/version", timeout=STALL_S
            ) as response:
                require(
                    response.status == 200
                    and json.load(response).get("currentVersion") == version,
                    "concurrent API version differs",
                )
            latency = time.monotonic() - started
            require(latency <= STALL_S, "concurrent API stalled")
            if running:
                polls += 1
                worst = max(worst, latency)
            time.sleep(0.5)
        completed = sum(future.result() for future in futures)
    require(polls > 0, "concurrent API polling population empty")
    return {
        "concurrent_streams_completed": completed,
        "concurrent_api_polls": polls,
        "concurrent_api_max_latency_s": worst,
    }


def live_authentication(scope_path, scope, models):
    require(
        scope.get("phase") == "qualification" and "result" not in scope,
        "live authentication requires isolated qualification scope",
    )
    require(
        models
        and all(
            isinstance(model, str) and model and not any(c in model for c in "\x00\r\n")
            for model in models
        ),
        "routing model selection empty or invalid",
    )
    database = Path(scope["home"]) / ".9router/db/data.sqlite"
    require(
        database.is_file() and not database.is_symlink(),
        "BLOCKED: existing guest-owned provider database is absent; "
        "no provider login is authorized",
    )
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT key FROM apiKeys WHERE isActive=1 LIMIT 1").fetchone()
        providers = db.execute(
            "SELECT COUNT(*) FROM providerConnections WHERE isActive=1"
        ).fetchone()[0]
    require(
        row and isinstance(row[0], str) and row[0] and providers > 0,
        "BLOCKED: existing guest-owned live provider logins "
        "and an active guest API key are required; "
        "no login authorization is implied",
    )
    key = row[0]
    deployer = controller_module().deployer()
    base = "http://127.0.0.1:20128"
    with urllib.request.urlopen(base + "/api/version", timeout=30) as response:
        served = json.load(response).get("currentVersion")
    candidate = Path(scope["packages"][next(iter(scope["packages"]))]["release"])
    require(
        served
        == json.loads(regular(candidate / "package.json").read_text())["version"],
        "live authentication target is not the exact candidate version",
    )

    def stream(model):
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            method="POST",
            data=json.dumps(
                {
                    "model": model,
                    "messages": [
                        {"role": "user", "content": "Reply with the word ready."}
                    ],
                    "stream": True,
                    "max_tokens": 32,
                }
            ).encode(),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
        )
        terminal = False
        with urllib.request.urlopen(request, timeout=180) as response:
            require(response.status == 200, "live stream refused")
            for line in response:
                require(
                    not deployer.is_stream_error(line), "live stream emitted an error"
                )
                terminal = terminal or deployer.is_terminal(line)
        require(terminal, "live stream truncated")
        return 1

    live = sum(stream(model) for model in models)
    concurrent_measurement = check_concurrent(stream, models, base, served)
    request = urllib.request.Request(
        base + "/v1/chat/completions",
        method="POST",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer guest-invalid-key",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=30).close()
        raise ValueError("unauthenticated request accepted")
    except urllib.error.HTTPError as error:
        require(error.code == 401, "unauthorized response differs")
    return {
        "live_streams_completed": live,
        **concurrent_measurement,
        "routing_models": len(models),
        "unauthorized_refusals": 1,
        "guest_credentials_only": True,
        "errors": 0,
    }


def record_packaged_measurements(result, evidence, identity, checks, log):
    for name in ("continuity", "recovery"):
        measurement = (
            {
                "transitions": result.get("transitions"),
                "provider": result.get("provider"),
            }
            if name == "continuity"
            else result.get(name)
        )
        validate_measurement(name, measurement)
        subjects = (
            sum(item["new_http_completed"] for item in measurement["transitions"])
            if name == "continuity"
            else measurement["recovered_requests"]
        )
        record_check(evidence, identity, checks, name, subjects, measurement, log)


def validate_signin_confirmation(confirmation, binding):
    require(
        isinstance(confirmation, dict)
        and confirmation.get("run_id") == binding["run_id"]
        and confirmation.get("namespace") == binding["namespace"]
        and confirmation.get("confirmed", True) is True
        and all(
            isinstance(confirmation.get(key), str) and confirmation[key].strip()
            for key in ("operator_quote", "operator_turn")
        ),
        "current-run independent sign-in confirmation required",
    )


def wait_signin_port(port, timeout):
    """Wait out closed fixture TCP state, never terminate an occupied listener."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", port))
            return
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            listener = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN"], capture_output=True, text=True, timeout=10)
            require(listener.returncode == 1 and not listener.stdout.strip() and not listener.stderr.strip(), "sign-in port has occupied listener or unknown owner")
            time.sleep(1)
    raise TimeoutError("recent fixture TCP state did not clear")


def interactive_baseline(tgz, binding, run_root, timeout):
    """Prepare only this run's private worker after deterministic jobs close."""
    for port in (20128, 21128, 21130):
        wait_signin_port(port, 120)
    scope_path, scope, manifest = prepare_scope(
        tgz, binding, run_id=safe_run_id(binding["run_id"] + "-live")
    )
    home = Path(scope["home"])
    require(not (home / ".9router/db/data.sqlite").exists(), "fresh sign-in state required")
    initialize_baseline(scope, manifest)
    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        controller = controller_module()
        controller.configure_qualification(scope_path)
        package = scope["packages"][binding["sha256"]]
        with controller.deployment_lock():
            require(not controller.STATE_FILE.exists(), "sign-in transaction occupied")
            controller.verify_enrollment_slots()
            controller.shared_environment(enrollment=True)
            entry = {
                "release": package["release"],
                "version": json.loads(regular(Path(package["release"]) / "package.json").read_text())["version"],
                "digest": binding["sha256"],
                "mode": "starting",
            }
            state = {
                "schema": 1, "active": "a", "slots": {"a": entry, "b": None},
                "pending": None, "enrollment": "preparing",
                "environment_sha256": controller.sha256(controller.STATE_DIR / "environment.json"),
            }
            controller.write_state(state)
            controller.prepare_enrollment(state, Path(entry["release"]), entry)
            controller.stop_enrollment_legacy_service(state)
            controller.start_worker("a", entry)
            status = controller.worker_status("a", controller.read_state()["slots"]["a"])
            require(status["mode"] == "ready", "pinned sign-in worker not ready")
    finally:
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home
    new_json(run_root / "signin-ready.json", {
        "run_id": binding["run_id"], "namespace": binding["namespace"],
        "sha256": binding["sha256"], "source_commit": binding["source_commit"],
        "scope": str(scope_path), "home": str(home), "remote_port": 21128,
        "worker_pid": status["appPid"], "worker_label": controller.job_label("a"),
        "provider_probes_run": False, "deadline_epoch": time.time() + timeout,
    })
    confirmation_path = run_root / "signin-confirmation.json"
    deadline = time.monotonic() + timeout
    while not confirmation_path.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    require(confirmation_path.exists(), "independent sign-in deadline expired")
    confirmation = private_json(confirmation_path)
    validate_signin_confirmation(confirmation, binding)
    with sqlite3.connect((home / ".9router/db/data.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        providers = dict(db.execute("SELECT provider,COUNT(*) FROM providerConnections WHERE isActive=1 AND authType='oauth' AND provider IN ('codex','claude') GROUP BY provider"))
        keys = db.execute("SELECT COUNT(*) FROM apiKeys WHERE isActive=1").fetchone()[0]
    require(all(providers.get(name, 0) > 0 for name in ("codex", "claude")) and keys > 0, "independent guest provider/key population missing")
    controller_call(scope_path, "reconcile")
    state = private_json(home / ".9router/hotswap/state.json")
    require(state.get("enrollment") == "complete" and state.get("pending") is None, "authenticated enrollment incomplete")
    new_json(run_root / "evidence/signin-provenance.json", {
        **confirmation, "credential_origin": "guest-owned-current-run",
        "active_provider_counts": providers, "active_api_key_count": keys,
        "scope": str(scope_path), "sha256": binding["sha256"],
        "snapshot_persistence_qualified": False,
    })
    return scope_path, scope


def live_scope_for_run(args, binding, run_root):
    if getattr(args, "interactive_signin", False):
        return interactive_baseline(Path(args.tgz), binding, run_root, args.signin_timeout)
    require(BASELINE.is_file() and not BASELINE.is_symlink(), "BLOCKED: existing guest-owned persistent baseline is absent; manual provider login needs separate authorization")
    baseline = private_json(BASELINE)
    require(baseline.get("credential_origin") == "guest-owned" and baseline.get("devbox_id") == binding["namespace"]["devbox_id"], "guest live baseline provenance missing")
    path, scope, _ = prepare_scope(Path(args.tgz), binding, run_id=safe_run_id(baseline["run_id"]))
    require(str(path) == baseline["scope"], "guest live baseline confinement differs")
    controller_call(path, "restore")
    return path, scope


def guest_failure_record(error):
    return {
        "type": type(error).__name__,
        "blocker": str(error)
        if isinstance(error, ValueError)
        else "guest execution failed",
        "diagnostic": str(error) if isinstance(error, GuestCheckFailure) else None,
        "errno": error.errno if isinstance(error, OSError) else None,
        "frames": [frame.name for frame in traceback.extract_tb(error.__traceback__)],
    }


def cmd_guest(args):
    guest_guard()
    os.umask(0o077)
    binding = private_json(Path(args.input))
    run_root = Path(args.input).parent
    require(
        run_root.resolve() == run_root
        and str(run_root).startswith(str(GUEST_ROOT) + "/"),
        "guest input is outside qualification storage",
    )
    source_binding(ROOT, binding)
    execution_file = run_root / "execution.json"
    deadline = time.monotonic() + 90
    while not execution_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    execution = private_json(execution_file)
    require(
        execution.get("run_id") == binding["run_id"]
        and isinstance(execution.get("execution_id"), str)
        and re.fullmatch(r"exec_[a-z0-9]+", execution["execution_id"]),
        "Namespace execution identity missing",
    )
    evidence = run_root / "evidence"
    evidence.mkdir(mode=0o700)
    checks = {}
    record = {
        **{
            key: binding[key]
            for key in (
                "protocol",
                "sha256",
                "runtime_sha256",
                "source_commit",
                "source_patch_sha256",
                "namespace",
                "proxy_version",
                "guest_binaries",
                "run_id",
            )
        },
        "execution_id": execution["execution_id"],
        "result": "incomplete",
        "guest_result": "unrun",
    }
    try:
        runtime_preflight(binding, run_root)
        locks, python, dependency_log = provision_dependencies(binding, run_root)
        scope_path, scope, manifest = prepare_scope(
            Path(args.tgz), binding, install=False
        )
        package = scope["packages"][binding["sha256"]]
        identity = {
            key: binding[key]
            for key in (
                "protocol",
                "sha256",
                "runtime_sha256",
                "source_commit",
                "source_patch_sha256",
                "namespace",
                "proxy_version",
                "guest_binaries",
                "run_id",
            )
        }
        require(
            guest_binary_bindings() == binding["guest_binaries"],
            "guest binaries changed before tests",
        )
        identity.update(
            execution_id=execution["execution_id"],
            manifest_sha256=package["manifest_sha256"],
            persistenceFingerprint=manifest["persistenceFingerprint"],
            package_files=package["package_files"],
            guest_versions={
                "macos": output(["sw_vers", "-productVersion"]),
                "architecture": output(["uname", "-m"]),
                "node": output(["node", "--version"]),
                "python": output([python, "--version"]),
                "caddy": output(["caddy", "version"]),
                "locked_dependencies": locks,
            },
            required_checks=sorted(CHECKS | PREREQUISITES),
        )
        new_json(evidence / "identity.json", identity)
        record.update(identity)
        record_check(
            evidence,
            identity,
            checks,
            "dependencies",
            4,
            {"locked_dependencies": locks},
            dependency_log,
        )
        prerequisite_checks(binding, run_root, evidence, identity, checks, python)
        fixture = regular(ROOT / "tests/mac/9router_packaged_hotswap.check.mjs")
        result_path = evidence / "packaged-result.json"
        env = isolated_environment(Path(scope["home"]), ROOT)
        log = guest_command(
            [
                "node",
                str(fixture),
                str(ROOT),
                package["tarball"],
                binding["sha256"],
                str(result_path),
                scope["home"],
                str(scope_path),
            ],
            ROOT,
            env,
            1800,
        )
        result = private_json(result_path)
        require(
            result.get("success") is True
            and result.get("tarballSha256") == binding["sha256"]
            and result.get("fixtureSessionsClosed") is True
            and result.get("managedJobsClosed") is True
            and all(
                result.get(key) == binding[key]
                for key in (
                    "source_commit",
                    "source_patch_sha256",
                    "namespace",
                    "runtime_sha256",
                    "run_id",
                )
            ),
            "packaged fixture failed or identity/cleanup differs",
        )
        record_packaged_measurements(result, evidence, identity, checks, log)
        live_scope_path, live_scope = live_scope_for_run(args, binding, run_root)
        current_state = private_json(
            Path(live_scope["home"]) / ".9router/hotswap/state.json"
        )
        active_entry = current_state["slots"][current_state["active"]]
        if active_entry.get("digest") != binding["sha256"]:
            controller_call(
                live_scope_path,
                "deploy-installed",
                live_scope["packages"][binding["sha256"]]["release"],
                "--digest",
                binding["sha256"],
            )
        authentication_scope = {
            **live_scope,
            "packages": {binding["sha256"]: live_scope["packages"][binding["sha256"]]},
        }
        authentication = live_authentication(
            live_scope_path, authentication_scope, args.models or QUALIFY_MODELS
        )
        record_check(
            evidence,
            identity,
            checks,
            "authentication",
            authentication["live_streams_completed"]
            + authentication["concurrent_streams_completed"],
            authentication,
            "Population: live guest-owned streams, four concurrent streams, "
            "and concurrent API polls; "
            "invalid-key refusal completed. API polls="
            + str(authentication["concurrent_api_polls"])
            + ", maximum latency seconds="
            + str(authentication["concurrent_api_max_latency_s"])
            + "\n",
        )
        source_binding(ROOT, binding)
        require(
            guest_binary_bindings() == binding["guest_binaries"]
            and controller_module().runtime_hashes() == binding["runtime_sha256"],
            "runtime bundle changed during qualification",
        )
        require(
            set(checks) == set(identity["required_checks"]),
            "required guest checks missing",
        )
        record["guest_result"] = "pass"
    except BaseException as error:
        record["failure"] = type(error).__name__
        record["guest_result"] = "fail"
        new_json(evidence / "failure.json", guest_failure_record(error))
    finally:
        record["checks"] = checks
        new_json(evidence / "checks.json", checks)
        new_json(evidence / "guest.json", record)
        if not (evidence / "identity.json").exists():
            new_json(
                evidence / "identity.json", {key: record.get(key) for key in BINDINGS}
            )
        identity = private_json(evidence / "identity.json")
        new_json(
            evidence / "manifest.json",
            {
                "identity": identity,
                "files": {
                    path.name: sha256(regular(path)) for path in evidence.iterdir()
                },
            },
        )
    return 0 if record["guest_result"] == "pass" else 1


def host_metadata(devbox_id, sdk_root):
    result = json.loads(
        output(["node", "-e", SDK_METADATA, str(sdk_root / "package.json"), devbox_id])
    )
    require(
        isinstance(result, dict)
        and result.get("os") == "macos"
        and result.get("architecture") == "arm64",
        "Namespace shape differs",
    )
    return result


def host_stage(tgz, args, stage):
    commit = output(["git", "rev-parse", "HEAD"], ROOT)
    require(re.fullmatch(r"[a-f0-9]{40}", commit), "source commit unreadable")
    tracked = output(["git", "ls-files", "-z"], ROOT).split("\x00")
    files = {
        name: sha256(regular(ROOT / name))
        for name in tracked
        if name and not name.startswith("docs/")
    }
    untracked = set(
        filter(
            None,
            output(
                ["git", "ls-files", "--others", "--exclude-standard", "-z"], ROOT
            ).split("\x00"),
        )
    )
    selected = set(args.source_file or [])
    require(
        selected <= untracked
        and all(
            not Path(name).is_absolute() and ".." not in Path(name).parts
            for name in selected
        ),
        "source additions must be explicit relative files",
    )
    runtime_untracked = {
        name
        for name in untracked
        if not name.startswith(("docs/", ".superpowers/"))
        and "__pycache__" not in Path(name).parts
        and not name.endswith(".pyc")
    }
    require(
        runtime_untracked <= selected, "uncommitted source additions need --source-file"
    )
    patch = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", "--no-textconv", commit],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    for name in sorted(selected):
        result = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", name],
            cwd=ROOT,
            capture_output=True,
        )
        require(result.returncode == 1, "source addition export failed")
        patch += result.stdout
        files[name] = sha256(regular(ROOT / name))
    for revision in (
        "5fd82262218f5f00b4f987587318908e548ad65a",
        "e08fa1a43d89caeefa880a612db445c68d578d01",
    ):
        require(
            output(["git", "rev-parse", revision + "^{commit}"], ROOT) == revision,
            "counterfactual source revision unavailable",
        )
    patch_path = stage / "qualification.patch"
    patch_path.write_bytes(patch)
    patch_path.chmod(0o600)
    bundle = stage / "source.bundle"
    references = output(
        ["git", "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"], ROOT
    ).splitlines()
    require(references, "source bundle references unavailable")
    output(
        ["git", "bundle", "create", str(bundle), "HEAD", *references], ROOT, timeout=120
    )
    shutil.copyfile(tgz, stage / "candidate.tgz")
    (stage / "candidate.tgz").chmod(0o600)
    return {
        "protocol": 1,
        "sha256": package_preflight(tgz),
        "source_commit": commit,
        "source_patch_sha256": sha256(patch_path),
        "source_files": files,
        "source_runtime_sha256": source_runtime_hashes(ROOT),
        "bundle_sha256": sha256(bundle),
    }


@contextmanager
def interruption_boundary():
    previous = {}

    def interrupt(_signal, _frame):
        raise KeyboardInterrupt

    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[number] = signal.signal(number, interrupt)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def wait_guest_manifest(args, active, remote, metadata, require_claim, cli, cli_env):
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        require_claim()
        observed = metadata(args.devbox_id)
        require(
            observed.get("id") == args.devbox_id
            and observed.get("name") == args.devbox_name
            and observed.get("instance_id") == active["instance_id"]
            and observed.get("state") == "running",
            "qualification instance changed",
        )
        result = subprocess.run(
            [
                cli,
                "exec",
                args.devbox_name,
                "--",
                "/bin/test",
                "-s",
                str(remote / "evidence/manifest.json"),
            ],
            env=cli_env,
            capture_output=True,
            timeout=30,
        )
        require(result.returncode in {0, 1}, "evidence observation failed")
        if result.returncode == 0:
            return
        time.sleep(5)
    raise TimeoutError("guest evidence deadline")


def host_dependency_hashes():
    locks = {}
    for name in (
        "package-lock.json",
        "cli/package-lock.json",
        "tests/package-lock.json",
    ):
        lock_path = ROOT / name
        if name == "tests/package-lock.json" and not lock_path.exists():
            lock_path = ROOT / TEST_LOCK
        locks[name] = sha256(regular(lock_path))
    return locks


def download_guest_evidence(args, remote, evidence, devbox):
    manifest = json.loads((evidence / "manifest.json").read_text())
    require(
        isinstance(manifest, dict)
        and isinstance(manifest.get("files"), dict)
        and {"identity.json", "checks.json", "guest.json"} <= manifest["files"].keys(),
        "guest export manifest missing mandatory evidence",
    )
    for name, expected in manifest["files"].items():
        evidence_name(name)
        require(
            digest(expected) and name not in {"manifest.json", "host.json"},
            "unsafe exported digest",
        )
        devbox(
            "download",
            args.devbox_name,
            str(remote / "evidence" / name),
            str(evidence / name),
        )
        require(
            sha256(regular(evidence / name)) == expected,
            "guest evidence export mismatch",
        )
        (evidence / name).chmod(0o600)


def host_export_expectations(evidence, binding, tgz, execution_id, manifest_hash):
    identity = private_json(evidence / "identity.json")
    require(
        identity.get("required_checks") == sorted(CHECKS | PREREQUISITES),
        "exported required check roster differs",
    )
    require(
        identity.get("guest_versions", {}).get("locked_dependencies")
        == host_dependency_hashes(),
        "exported locked dependency identities differ",
    )
    require(
        identity.get("guest_binaries") == binding["guest_binaries"]
        and all(
            identity["guest_versions"].get(name) == item["version"]
            for name, item in binding["guest_binaries"].items()
        ),
        "exported guest runtime differs from independent pre-test observation",
    )
    for key in (
        "protocol",
        "sha256",
        "runtime_sha256",
        "source_commit",
        "source_patch_sha256",
        "namespace",
        "proxy_version",
        "guest_binaries",
        "run_id",
    ):
        require(
            identity.get(key) == binding[key],
            "exported identity differs from staged inputs",
        )
    require(
        identity["execution_id"] == execution_id, "exported execution identity differs"
    )
    # Package expectations come from exact tarball bytes, not the guest's report.
    with tarfile.open(tgz, "r:gz") as archive:
        package_files = archive_package_hashes(archive)
        with archive.extractfile("package/app/hotswap-manifest.json") as stream:
            manifest_bytes = stream.read()
    package_manifest = json.loads(manifest_bytes)
    require(
        sha256(tgz) == binding["sha256"]
        and source_runtime_hashes(ROOT) == binding["source_runtime_sha256"],
        "host exact package or static runtime source changed",
    )
    for name, expected in binding["source_files"].items():
        require(
            sha256(regular(ROOT / name)) == expected,
            "host source changed during qualification",
        )
    return {
        **identity,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "persistenceFingerprint": package_manifest["persistenceFingerprint"],
        "package_files": package_files,
        "evidence_manifest_sha256": manifest_hash,
    }


def complete_host_qualification(
    args,
    binding,
    work,
    metadata,
    devbox,
    evidence,
    exported_expected,
    require_claim,
    claim_path,
    close=lambda: None,
):
    # Close any owned sign-in forwarding before the exact final stop.
    with interruption_boundary():
        record = qualification_lifecycle(
            args.devbox_name,
            args.devbox_id,
            work,
            metadata,
            close,
            stop=lambda name: devbox("stop", name, "--force"),
        )
    record["authorization"] = binding["authorization"]
    if record.get("cleanup", {}).get("verified"):
        require_claim()
        os.unlink(claim_path)
    if not record.get("failure") and record.get("guest_result") == "pass":
        try:
            record = finish(record, evidence, exported_expected)
            QUALIFIED.mkdir(parents=True, exist_ok=True, mode=0o700)
            new_json(QUALIFIED / (binding["sha256"] + ".json"), record)
        except (OSError, ValueError, KeyError, TypeError) as error:
            record["result"] = "fail"
            record["failure"] = "receipt:" + type(error).__name__
    new_json(evidence / "host.json", record)
    print(json.dumps({"result": record["result"], "evidence": str(evidence)}))
    return 0 if record["result"] == "pass" else 1


def validate_host_qualification(args):
    require(not in_vm(), "run is host orchestration only")
    require(
        args.acknowledge_namespace,
        "current-turn Namespace authorization acknowledgement required",
    )
    require(
        args.authorization_quote and args.authorization_turn,
        "verbatim current operator authorization and turn reference required",
    )
    require(
        args.devbox_id and args.devbox_name and args.sdk_root,
        "exact devbox identity and existing SDK root required",
    )
    require(re.fullmatch(r"[A-Za-z0-9_-]+", args.devbox_id), "unsafe devbox ID")
    require(
        type(args.timeout) is int and args.timeout > 0,
        "positive guest deadline required",
    )
    safe_run_id(args.devbox_name)
    if getattr(args, "interactive_signin", False):
        require(type(args.signin_timeout) is int and args.signin_timeout > 0, "positive sign-in deadline required")
        require(args.confirmation_file and not Path(args.confirmation_file).exists(), "new private sign-in confirmation path required")


def cmd_run(args):
    validate_host_qualification(args)
    sdk_root = Path(args.sdk_root).resolve()
    require(
        (sdk_root / "node_modules/@namespacelabs/sdk/package.json").is_file(),
        "existing Namespace SDK missing",
    )
    tgz = Path(args.tgz).resolve()
    package_preflight(tgz)
    run_id = "qualify-" + uuid.uuid4().hex
    stage = Path(tempfile.mkdtemp(prefix="9router-qualify-"))
    stage.chmod(0o700)
    binding = host_stage(tgz, args, stage)
    binding["run_id"] = run_id
    binding["authorization"] = {
        "quote": args.authorization_quote,
        "turn": args.authorization_turn,
    }
    remote = GUEST_ROOT / run_id
    evidence = Path(args.evidence).resolve()
    require(not evidence.exists(), "evidence destination must be new")
    evidence.mkdir(parents=True, mode=0o700)
    cli = shutil.which("devbox") or str(Path.home() / ".local/bin/devbox")

    def metadata(identity):
        return host_metadata(identity, sdk_root)

    cli_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"SONAR_TOKEN", "SONARQUBE_CLI_TOKEN", "NINEROUTER_PROBE_KEY"}
    }

    def devbox(*argv, timeout=120):
        require_claim()
        return output([cli, *argv], ROOT, cli_env, timeout)

    exported_expected = {}
    forwards = []

    def close():
        for child, log in forwards:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            log.close()

    def require_claim():
        held, live = os.fstat(claim), claim_path.lstat()
        require(
            stat.S_ISREG(live.st_mode)
            and live.st_uid == os.getuid()
            and (held.st_dev, held.st_ino) == (live.st_dev, live.st_ino),
            "exclusive Namespace lifecycle claim changed",
        )

    def work():
        require_claim()
        devbox("exec", args.devbox_name, "--", "/usr/bin/uname", "-m", timeout=180)
        active = metadata(args.devbox_id)
        require(
            active.get("id") == args.devbox_id
            and active.get("name") == args.devbox_name
            and active.get("state") == "running"
            and active.get("instance_id"),
            "activated devbox identity differs",
        )
        binding["namespace"] = {
            "devbox_id": args.devbox_id,
            "instance_id": active["instance_id"],
        }
        binding["tgz"] = str(remote / "candidate.tgz")
        devbox(
            "exec",
            args.devbox_name,
            "--",
            "/bin/mkdir",
            "-p",
            "-m",
            "700",
            str(GUEST_ROOT),
        )
        devbox("exec", args.devbox_name, "--", "/bin/mkdir", "-m", "700", str(remote))
        devbox(
            "upload",
            args.devbox_name,
            str(HERE / "9router_vm_qualify.py"),
            str(remote / "runtime-preflight.py"),
        )
        binaries = json.loads(
            devbox(
                "exec",
                args.devbox_name,
                "--",
                "python3",
                str(remote / "runtime-preflight.py"),
                "runtime",
                "--tools",
                str(GUEST_ROOT / "tools" / CADDY_ARCHIVE_SHA256),
                timeout=180,
            )
        )
        bind_guest_runtime(binding, binaries)
        capacity = json.loads(devbox("exec", args.devbox_name, "--", "python3", "-c", "import json,shutil; d=shutil.disk_usage('/Volumes/devbox'); print(json.dumps({'total_bytes':d.total,'free_bytes':d.free}))"))
        new_json(evidence / "guest-capacity.json", {"namespace": binding["namespace"], **capacity})
        new_json(stage / "input.json", binding)
        require(
            sha256(regular(HERE / "9router_vm_qualify.py"))
            == binding["source_files"]["scripts/mac/9router_vm_qualify.py"],
            "host runtime observer source changed",
        )
        for name in (
            "input.json",
            "source.bundle",
            "qualification.patch",
            "candidate.tgz",
        ):
            devbox("upload", args.devbox_name, str(stage / name), str(remote / name))
        setup = (
            "import hashlib,json,pathlib,subprocess,sys; r=pa"
            "thlib.Path(sys.argv[1]); b=json.loads((r/'input."
            "json').read_text()); assert hashlib.sha256((r/'s"
            "ource.bundle').read_bytes()).hexdigest()==b['bun"
            "dle_sha256']; subprocess.run(['git','clone','--n"
            "o-checkout',str(r/'source.bundle'),str(r/'source"
            "')],check=True); subprocess.run(['git','checkout"
            "','--detach',b['source_commit']],cwd=r/'source',"
            "check=True); p=r/'qualification.patch'; assert h"
            "ashlib.sha256(p.read_bytes()).hexdigest()==b['so"
            "urce_patch_sha256']; p.stat().st_size and subpro"
            "cess.run(['git','apply','--binary',str(p)],cwd=r"
            "/'source',check=True); (r/'source/qualification."
            "patch').write_bytes(p.read_bytes())"
        )
        devbox(
            "exec",
            args.devbox_name,
            "--",
            "python3",
            "-c",
            setup,
            str(remote),
            timeout=180,
        )
        guest_script = remote / "source/scripts/mac/9router_vm_qualify.py"
        detached = devbox(
            "exec",
            "--detach",
            args.devbox_name,
            "--",
            "python3",
            str(guest_script),
            "guest",
            str(remote / "candidate.tgz"),
            "--input",
            str(remote / "input.json"),
            *(["--interactive-signin", "--signin-timeout", str(args.signin_timeout)] if getattr(args, "interactive_signin", False) else []),
            *[
                argument
                for model in args.models or QUALIFY_MODELS
                for argument in ("--model", model)
            ],
        )
        match = re.search(r"\bexec_[a-z0-9]+\b", detached)
        require(match, "Namespace execution ID missing")
        new_json(stage / "execution.json", {"run_id": run_id, "execution_id": match[0]})
        devbox(
            "upload",
            args.devbox_name,
            str(stage / "execution.json"),
            str(remote / "execution.json"),
        )
        if getattr(args, "interactive_signin", False):
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                require_claim()
                observed = metadata(args.devbox_id)
                require(observed.get("instance_id") == active["instance_id"] and observed.get("state") == "running", "sign-in guest instance changed")
                ready = subprocess.run([cli, "exec", args.devbox_name, "--", "/bin/test", "-s", str(remote / "signin-ready.json")], env=cli_env, capture_output=True, timeout=30)
                require(ready.returncode in {0, 1}, "sign-in readiness observation failed")
                if ready.returncode == 0:
                    break
                finished = subprocess.run([cli, "exec", args.devbox_name, "--", "/bin/test", "-s", str(remote / "evidence/manifest.json")], env=cli_env, capture_output=True, timeout=30)
                require(finished.returncode in {0, 1}, "guest outcome observation failed")
                if finished.returncode == 0:
                    break
                time.sleep(5)
            if ready.returncode == 0:
                devbox("download", args.devbox_name, str(remote / "signin-ready.json"), str(evidence / "signin-ready.json"))
                handoff = private_json(evidence / "signin-ready.json")
                require(handoff["namespace"] == binding["namespace"] and handoff["run_id"] == run_id, "sign-in handoff differs")
                for port in (32128, 1455):
                    reservation = socket.socket()
                    try:
                        reservation.bind(("127.0.0.1", port))
                    finally:
                        reservation.close()
                log = (evidence / "signin-forward.log").open("x")
                child = subprocess.Popen([cli, "port-forward", args.devbox_name, "--ports", "32128:21128,1455:1455"], env=cli_env, stdout=log, stderr=subprocess.STDOUT)
                forwards.append((child, log))
                until = time.monotonic() + 60
                while time.monotonic() < until:
                    require(child.poll() is None, "owned sign-in forward exited")
                    listener = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(child.pid), "-iTCP:32128", "-sTCP:LISTEN", "-Fpn"], capture_output=True, text=True)
                    names = [line[1:] for line in listener.stdout.splitlines() if line.startswith("n")]
                    if names:
                        require(all(value == "127.0.0.1:32128" for value in names), "sign-in access is not loopback-only")
                        break
                    time.sleep(1)
                require(names, "owned sign-in forward unavailable")
                print(json.dumps({"phase": "independent-signin-required", "url": "http://localhost:32128/login", "home": handoff["home"], "run_id": run_id, "namespace": binding["namespace"], "confirmation_file": args.confirmation_file, "forward_pid": child.pid, "deadline_epoch": handoff["deadline_epoch"]}), flush=True)
                confirmation_path = Path(args.confirmation_file)
                until = time.monotonic() + args.signin_timeout
                while not confirmation_path.exists() and time.monotonic() < until:
                    require_claim()
                    require(child.poll() is None and metadata(args.devbox_id).get("instance_id") == active["instance_id"], "sign-in owner or instance changed")
                    time.sleep(1)
                require(confirmation_path.exists(), "manual sign-in deadline expired")
                confirmation = private_json(confirmation_path)
                validate_signin_confirmation(confirmation, binding)
                devbox("upload", args.devbox_name, str(confirmation_path), str(remote / "signin-confirmation.json"))
                close()
        wait_guest_manifest(args, active, remote, metadata, require_claim, cli, cli_env)
        manifest_hash = devbox(
            "exec",
            args.devbox_name,
            "--",
            "/usr/bin/shasum",
            "-a",
            "256",
            str(remote / "evidence/manifest.json"),
        ).split()[0]
        devbox(
            "download",
            args.devbox_name,
            str(remote / "evidence/manifest.json"),
            str(evidence / "manifest.json"),
        )
        require(
            digest(manifest_hash)
            and sha256(regular(evidence / "manifest.json")) == manifest_hash,
            "guest manifest export mismatch",
        )
        download_guest_evidence(args, remote, evidence, devbox)
        guest_record = private_json(evidence / "guest.json")
        if guest_record.get("guest_result") != "pass":
            # Preserve the measured guest failure independently of cleanup.
            return guest_record
        exported_expected.update(
            host_export_expectations(evidence, binding, tgz, match[0], manifest_hash)
        )
        return guest_record

    claim_path = Path(tempfile.gettempdir()) / (
        "namespace-owner-" + args.devbox_id + ".lock"
    )
    claim = os.open(claim_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        return complete_host_qualification(
            args,
            binding,
            work,
            metadata,
            devbox,
            evidence,
            exported_expected,
            require_claim,
            claim_path,
            close,
        )
    finally:
        os.close(claim)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    runtime = commands.add_parser(
        "runtime", help="observe/provision guest runtime before tests"
    )
    runtime.add_argument("--tools", required=True)
    runtime.set_defaults(function=cmd_runtime)
    for name, function in (
        ("run", cmd_run),
        ("guest", cmd_guest),
        ("baseline", cmd_baseline),
        ("restore", cmd_restore),
    ):
        command = commands.add_parser(name)
        if name != "restore":
            command.add_argument("tgz")
        if name in {"guest", "baseline"}:
            command.add_argument("--input", required=True)
        if name == "restore":
            command.add_argument("--scope", required=True)
        if name in {"run", "guest"}:
            command.add_argument("--model", dest="models", action="append")
            command.add_argument("--interactive-signin", action="store_true")
            command.add_argument("--signin-timeout", type=int, default=1200)
        if name == "run":
            command.add_argument("--confirmation-file")
        if name == "run":
            command.add_argument("--acknowledge-namespace", action="store_true")
            command.add_argument("--authorization-quote")
            command.add_argument("--authorization-turn")
            command.add_argument(
                "--devbox-id", default=os.environ.get("NINEROUTER_QUALIFY_DEVBOX_ID")
            )
            command.add_argument(
                "--devbox-name",
                default=os.environ.get("NINEROUTER_QUALIFY_DEVBOX_NAME"),
            )
            command.add_argument(
                "--sdk-root", default=os.environ.get("NINEROUTER_NAMESPACE_SDK_ROOT")
            )
            command.add_argument("--source-file", action="append")
            command.add_argument("--evidence", required=True)
            command.add_argument("--timeout", type=int, default=7200)
        command.set_defaults(function=function)
    args = parser.parse_args(argv)
    try:
        return args.function(args)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as error:
        print(
            "refused: qualification input: "
            + (str(error) if isinstance(error, ValueError) else type(error).__name__),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
