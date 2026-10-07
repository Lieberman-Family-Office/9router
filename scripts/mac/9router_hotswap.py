#!/usr/bin/env python3
"""Two-slot, same-schema release transactions. Unknown work refuses mutation.

ponytail: one host and two slots. Schema migration and host failover need
separate workflows. Enrollment has a maintenance boundary. Its acknowledgement
flag is not operator authorization.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home() / ".9router"
STATE_DIR = HOME / "hotswap"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "deploy.lock"
RUNTIME = Path(f"/tmp/9r-{os.getuid()}")
RELEASES = HOME / "releases"
QUALIFIED = HOME / "qualified"
DB = HOME / "db/data.sqlite"
LINK = Path("/opt/homebrew/lib/node_modules/9router")
PORTS = {"a": 21128, "b": 21130}
WORK_KEYS = (
    "responses",
    "handlers",
    "upgrades",
    "cleanup",
    "persistence",
    "refresh",
    "background",
    "quota",
    "websocket",
)
MODES = {"starting", "ready", "active", "draining", "stopped", "failed"}
CHECKS = {"continuity", "authentication", "recovery"}
QUALIFICATION_SCOPE = None
QUALIFICATION_VOLUME = Path("/Volumes/devbox")
JOB_PREFIX = "com.lfenergy.9router"


def job_label(slot):
    return f"{JOB_PREFIX}-worker-{slot}" if slot in PORTS else slot


def deployer():
    spec = importlib.util.spec_from_file_location(
        "hotswap_deployer", HERE / "9router_deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HOME, module.RELEASES, module.DB = HOME, RELEASES, DB
    module.LOG, module.LINK = HOME / "deploys.log", LINK
    return module


EXPECTED_ERRORS = (
    OSError,
    ValueError,
    RuntimeError,
    KeyError,
    TypeError,
    AttributeError,
    IndexError,
    subprocess.SubprocessError,
)


def refusal(scope, error):
    # Only controller-authored ValueError messages may reach diagnostics.
    reason = (
        str(error)
        if isinstance(error, ValueError)
        else type(error).__name__
        if isinstance(error, BaseException)
        else error
    )
    print(f"refused: {scope}: {reason}", file=sys.stderr)
    return 1


def valid_version(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", value
    ):
        raise ValueError("invalid release version")
    return value


def private_directory(path):
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("unsafe private directory")
    for parent in path.parents:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != 0 or str(parent) not in {"/tmp", "/var"}:
                raise ValueError("unsafe private ancestor")
        elif (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or (
                info.st_mode & 0o022
                and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            )
        ):
            raise ValueError("unsafe private ancestor")


def private_file(path):
    private_directory(path.parent)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValueError("unsafe private file")


def read_json(path):
    private_file(path)
    return json.loads(path.read_text())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    private_directory(path.parent)
    if os.path.lexists(path):
        private_file(path)
    fd, temporary = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def write_state(state):
    atomic_json(STATE_FILE, state)


@contextmanager
def deployment_lock():
    private_directory(STATE_DIR)
    private_directory(RUNTIME)
    if os.path.lexists(LOCK_FILE):
        private_file(LOCK_FILE)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private_file(LOCK_FILE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("deployment busy") from None
        yield
    finally:
        os.close(fd)


def qualification_destinations(record, releases):
    """Accept one baseline path or exactly two slot-bound guest installations."""
    if "installations" in record:
        installations = record["installations"]
        if (
            "release" in record
            or not isinstance(installations, dict)
            or set(installations) != set(PORTS)
        ):
            raise ValueError("qualification requires two bound installations")
    else:
        installations = {None: record.get("release")}
    if any(not isinstance(value, str) or not value for value in installations.values()):
        raise ValueError("invalid qualification installation binding")
    destinations, versions = {}, set()
    for slot, value in installations.items():
        dest = Path(value)
        if releases not in dest.parents:
            raise ValueError("release must be concrete and confined")
        version = valid_version(dest.relative_to(releases).parts[0])
        expected = releases / version
        if slot is not None:
            expected /= slot
        if dest != expected / "lib/node_modules/9router":
            raise ValueError("release must be concrete and confined")
        destinations[slot] = dest
        versions.add(version)
    if len(versions) != 1:
        raise ValueError("qualification installation versions differ")
    return destinations


def require_release_entry(path, kind, reason):
    info = path.lstat()
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError(reason)


def concrete_release(dest, *, releases=None, scope=None):
    dest = Path(dest)
    if releases is None:
        releases, scope = RELEASES, QUALIFICATION_SCOPE
    if scope is None:
        version = valid_version(dest.parents[2].name)
        expected = releases / version / "lib/node_modules/9router"
        bound = dest == expected
    else:
        if (
            scope.get("phase") != "qualification"
            or "result" in scope
            or releases != Path(scope["home"]) / ".9router/releases"
            or releases not in dest.parents
        ):
            raise ValueError("release must be concrete and confined")
        version = valid_version(dest.relative_to(releases).parts[0])
        bound = any(
            dest in qualification_destinations(record, releases).values()
            for record in scope["packages"].values()
        )
    if not bound or dest.resolve() != dest:
        raise ValueError("release must be concrete and confined")
    require_release_entry(releases, stat.S_ISDIR, "unsafe release directory")
    cursor = releases
    for component in dest.relative_to(releases).parts:
        cursor /= component
        require_release_entry(cursor, stat.S_ISDIR, "unsafe release ancestor")
    require_release_entry(dest / "package.json", stat.S_ISREG, "unsafe release package")
    if json.loads((dest / "package.json").read_text()).get("version") != version:
        raise ValueError("release version mismatch")
    return version


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def executable(name):
    found = shutil.which(name)
    if not found:
        raise ValueError(f"missing runtime executable: {name}")
    return str(Path(found).resolve())


def runtime_hashes():
    files = {
        "controller": Path(__file__),
        "deployer": HERE / "9router_deploy.py",
        "worker": HERE / "9router_worker.cjs",
        "managed": HERE.parents[1] / "src/lib/db/managed.cjs",
        "caddy_template": HERE / "templates/9router.Caddyfile",
        "worker_template": HERE / "templates/com.lfenergy.9router-worker.plist",
        "proxy_template": HERE / "templates/com.lfenergy.9router-proxy.plist",
        "node": Path(executable("node")),
        "caddy": Path(executable("caddy")),
    }
    return {name: sha256(path) for name, path in files.items()}


def require_qualification_guest():
    if sys.platform != "darwin":
        raise ValueError("qualification requires a macOS guest")
    guest = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"],
        capture_output=True,
        text=True,
        check=False,
    )
    architecture = subprocess.run(
        ["/usr/bin/uname", "-m"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        guest.returncode
        or guest.stdout.strip() != "1"
        or architecture.returncode
        or architecture.stdout.strip() != "arm64"
    ):
        raise ValueError("qualification requires an ARM64 virtual machine")


def qualification_identity(path):
    scope = read_json(Path(path))
    if not isinstance(scope, dict):
        raise ValueError("invalid qualification scope")
    run_id = scope.get("run_id")
    if (
        type(scope.get("protocol")) is not int
        or scope["protocol"] != 1
        or scope.get("phase") != "qualification"
        or "result" in scope
        or not isinstance(run_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id)
    ):
        raise ValueError("invalid qualification scope")
    home = QUALIFICATION_VOLUME / "9router/qualification" / run_id / "home"
    if (
        scope.get("home") != str(home)
        or Path.home() != home
        or home.resolve() != home
        or Path(path).resolve() != home / "scope.json"
        or scope.get("source_root") != str(HERE.parents[1])
        or HERE.parents[1].resolve() != HERE.parents[1]
        or QUALIFICATION_VOLUME not in HERE.parents[1].parents
        or not re.fullmatch(r"[a-f0-9]{40}", scope.get("source_commit", ""))
        or not re.fullmatch(r"[a-f0-9]{64}", scope.get("source_patch_sha256", ""))
        or not isinstance(scope.get("namespace"), dict)
        or any(
            not isinstance(scope["namespace"].get(key), str)
            or not scope["namespace"][key]
            for key in ("devbox_id", "instance_id")
        )
    ):
        raise ValueError("qualification guest identity or confinement differs")
    private_directory(home)
    revision = subprocess.run(
        ["git", "-C", scope["source_root"], "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if revision.returncode or revision.stdout.strip() != scope["source_commit"]:
        raise ValueError("qualification source revision differs")
    runtime = Path("/tmp") / ("9rq-" + hashlib.sha256(run_id.encode()).hexdigest()[:12])
    if scope.get("runtime") != str(runtime):
        raise ValueError("qualification socket root differs")
    if scope.get("runtime_sha256") != runtime_hashes():
        raise ValueError("qualification runtime bytes differ")
    proxy = subprocess.run(
        [executable("caddy"), "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proxy.returncode or scope.get("proxy_version") != proxy.stdout.strip():
        raise ValueError("qualification proxy identity differs")
    return scope, home, runtime


def verify_qualification_package(scope, home, data, digest, record):
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or not isinstance(record, dict):
        raise ValueError("invalid qualification package binding")
    destinations = qualification_destinations(record, data / "releases")
    archive = Path(record.get("tarball", ""))
    if (
        not archive.is_absolute()
        or archive.resolve() != archive
        or home.parent not in archive.parents
    ):
        raise ValueError("qualification tarball confinement differs")
    private_file(archive)
    if sha256(archive) != digest:
        raise ValueError("qualification tarball bytes differ")
    for dest in destinations.values():
        concrete_release(dest, releases=data / "releases", scope=scope)
        if (
            record.get("manifest_sha256") != sha256(dest / "app/hotswap-manifest.json")
            or record.get("package_files") != installed_package_hashes(dest)
            or record.get("persistenceFingerprint")
            != json.loads((dest / "app/hotswap-manifest.json").read_text()).get(
                "persistenceFingerprint"
            )
        ):
            raise ValueError("qualification installed package differs")


def configure_qualification(path):
    """Bind a non-deployable test scope to an isolated Namespace guest runtime."""
    global HOME, STATE_DIR, STATE_FILE, LOCK_FILE, RUNTIME, RELEASES, QUALIFIED, DB
    global LINK, QUALIFICATION_SCOPE, JOB_PREFIX
    require_qualification_guest()
    scope, home, runtime = qualification_identity(path)
    packages = scope.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise ValueError("qualification package population missing")
    data = home / ".9router"
    private_directory(data)
    for digest, record in packages.items():
        verify_qualification_package(scope, home, data, digest, record)
    HOME = data
    STATE_DIR = data / "hotswap"
    STATE_FILE, LOCK_FILE = STATE_DIR / "state.json", STATE_DIR / "deploy.lock"
    RUNTIME, RELEASES = runtime, data / "releases"
    QUALIFIED, DB = data / "qualified", data / "db/data.sqlite"
    LINK = data / "package-link"
    JOB_PREFIX = "com.lfenergy.9router-qualify-" + runtime.name.removeprefix("9rq-")
    QUALIFICATION_SCOPE = scope


def scoped_qualification(digest):
    if QUALIFICATION_SCOPE["runtime_sha256"] != runtime_hashes():
        raise ValueError("qualification runtime changed")
    record = QUALIFICATION_SCOPE["packages"].get(digest)
    if record is None:
        raise ValueError("package absent from isolated qualification scope")
    if sha256(Path(record["tarball"])) != digest:
        raise ValueError("qualification tarball changed")
    return record  # Test-only authorization; never a passing deployment receipt.


def qualification(digest):
    if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
        raise ValueError("invalid tarball digest")
    if QUALIFICATION_SCOPE is not None:
        return scoped_qualification(digest)
    record = read_json(QUALIFIED / f"{digest}.json")
    if not isinstance(record, dict):
        raise ValueError("invalid qualification record")
    if (
        type(record.get("protocol")) is not int
        or record["protocol"] != 1
        or record.get("result") != "pass"
        or record.get("sha256") != digest
    ):
        raise ValueError("managed qualification missing or failed")
    if record.get("runtime_sha256") != runtime_hashes():
        raise ValueError("qualification runtime binding is stale")
    if (
        not record.get("proxy_version")
        or not record.get("source_commit")
        or not record.get("guest_versions")
    ):
        raise ValueError("qualification runtime identity incomplete")
    proxy = subprocess.run(
        [executable("caddy"), "version"], capture_output=True, text=True, check=False
    )
    if proxy.returncode or proxy.stdout.strip() != record["proxy_version"]:
        raise ValueError("qualification proxy version differs")
    namespace = record.get("namespace", {})
    if not namespace.get("devbox_id") or not namespace.get("instance_id"):
        raise ValueError("qualification Namespace identity incomplete")
    verify_qualification_evidence(record)
    return record


def verify_qualification_evidence(record):
    cleanup = record.get("cleanup", {})
    observations = cleanup.get("observations", [])
    if (
        cleanup.get("verified") is not True
        or not isinstance(observations, list)
        or len(observations) != 2
        or any(
            not isinstance(item, dict)
            or item.get("state") != "stopped"
            or "instance_id" not in item
            or item["instance_id"] is not None
            or type(item.get("at")) not in {int, float}
            or not math.isfinite(item["at"])
            for item in observations
        )
        or observations[1]["at"] - observations[0]["at"] < 60
    ):
        raise ValueError("qualification shutdown evidence incomplete")
    checks = record.get("checks", {})
    if (
        not isinstance(checks, dict)
        or not CHECKS.issubset(checks)
        or any(
            not isinstance(check, dict)
            or type(check.get("exit_code")) is not int
            or check["exit_code"] != 0
            or type(check.get("subjects")) is not int
            or check["subjects"] <= 0
            or not isinstance(check.get("evidence_sha256"), str)
            or not re.fullmatch("[a-f0-9]{64}", check["evidence_sha256"])
            for check in checks.values()
        )
    ):
        raise ValueError("qualification check evidence incomplete")


def node_managed(dest, operation, *arguments):
    # Use the same native verifier as workers; no parallel Python schema implementation.
    module = dest / "app/src/lib/db/managed.cjs"
    code = (
        "const m=require(process.argv[1]); const a=process.argv.slice(2); " + operation
    )
    result = subprocess.run(
        [executable("node"), "-e", code, str(module), *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("native managed persistence check refused")
    return result.stdout


def validate_package(dest, digest):
    version = concrete_release(dest)
    record = qualification(digest)
    if (
        QUALIFICATION_SCOPE is not None
        and dest not in qualification_destinations(record, RELEASES).values()
    ):
        raise ValueError("qualification installation is not bound to package")
    manifest = dest / "app/hotswap-manifest.json"
    if record.get("manifest_sha256") != sha256(manifest):
        raise ValueError("qualification manifest binding differs")
    value = json.loads(manifest.read_text())
    if record.get("persistenceFingerprint") != value.get("persistenceFingerprint"):
        raise ValueError("qualification persistence binding differs")
    files = record.get("package_files")
    if not isinstance(files, dict) or not files:
        raise ValueError("qualification installed package binding missing")
    if installed_package_hashes(dest) != files:
        raise ValueError("installed package differs from qualified bytes")
    return {"version": version}


def installed_package_hashes(dest):
    actual = {}

    def unreadable(error):
        raise error

    for root, directories, names in os.walk(
        dest, followlinks=False, onerror=unreadable
    ):
        for name in directories + names:
            path = Path(root) / name
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
            ):
                raise ValueError("unsafe installed package entry")
            if stat.S_ISREG(info.st_mode):
                actual[path.relative_to(dest).as_posix()] = sha256(path)
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("non-regular installed package entry")
    return actual


def validate_release(dest, digest):
    identity = validate_package(dest, digest)
    manifest = dest / "app/hotswap-manifest.json"
    node_managed(
        dest,
        "m.verifyManagedDatabase(...a);",
        DB,
        manifest,
        STATE_DIR / "manifest.json",
        STATE_DIR / "refresh.sqlite",
    )
    return identity


def snapshot_db(dest):
    if shutil.disk_usage(dest.parent).free < DB.stat().st_size * 2 + 16 * 1024 * 1024:
        raise ValueError("insufficient snapshot and transaction disk space")
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    deployer().snapshot_db(dest)
    with dest.open("rb") as stream:
        os.fsync(stream.fileno())
    sync_directory(dest.parent)


def stage_assets(dest):
    source = dest / "app/.next-cli-build/static"
    if not source.is_dir() or source.is_symlink():
        raise ValueError("missing immutable dashboard assets")
    target = STATE_DIR / "assets"
    target.mkdir(mode=0o700, exist_ok=True)
    private_directory(target)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("symlink dashboard asset refused")
        relative = path.relative_to(source)
        out = target / relative
        if path.is_dir():
            out.mkdir(mode=0o700, exist_ok=True)
            private_directory(out)
        elif path.is_file():
            publish_asset(path, out)
        else:
            raise ValueError("non-regular dashboard asset refused")


def publish_asset(path, out):
    if os.path.lexists(out):
        private_file(out)
        if sha256(path) != sha256(out):
            raise ValueError("immutable dashboard asset conflict")
        return
    fd, temporary = tempfile.mkstemp(prefix=".asset-", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as stream, path.open("rb") as incoming:
            shutil.copyfileobj(incoming, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # The deployment lock protects names; publish complete bytes only.
        if os.path.lexists(out):
            raise ValueError("immutable dashboard asset name appeared")
        os.replace(temporary, out)
        sync_directory(out.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def route_slot():
    private_directory(RUNTIME)
    path = RUNTIME / "active.sock"
    info = path.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unknown public socket route")
    target = Path(os.path.abspath(RUNTIME / os.readlink(path)))
    for slot in PORTS:
        if target == RUNTIME / f"{slot}.sock":
            return slot
    raise ValueError("foreign public socket route")


def replace_route(runtime, slot):
    if slot not in PORTS or runtime != RUNTIME:
        raise ValueError("invalid route replacement")
    private_directory(runtime)
    info = (runtime / f"{slot}.sock").lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("candidate bridge identity unknown")
    temporary = runtime / f".route-{os.getpid()}"
    try:
        temporary.symlink_to(f"{slot}.sock")
        os.replace(temporary, runtime / "active.sock")
        sync_directory(runtime)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def control(slot, op):
    if slot not in PORTS or op not in {"status", "drain", "resume", "stop"}:
        raise ValueError("invalid control operation")
    private_directory(RUNTIME)
    path = RUNTIME / f"{slot}.ctl"
    info = path.lstat()
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe worker control socket")
    if op != "stop":
        bridge = RUNTIME / f"{slot}.sock"
        # Stopped workers retain control until controller bootout.
        if os.path.lexists(bridge):
            info = bridge.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("unsafe worker bridge socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        # Stop awaits acknowledged cleanup; timeout never permits bootout.
        connection.settimeout(120 if op == "stop" else 10)
        connection.connect(str(path))
        connection.sendall(json.dumps({"op": op}).encode() + b"\n")
        return read_control_response(connection, slot)


def read_control_response(connection, slot):
    data = b""
    while b"\n" not in data:
        block = connection.recv(4097 - len(data))
        if not block:
            raise ValueError("incomplete worker control response")
        data += block
        if len(data) > 4096:
            raise ValueError("oversized worker control response")
    if data.count(b"\n") != 1 or not data.endswith(b"\n"):
        raise ValueError("malformed worker control response")
    response = json.loads(data)
    if not isinstance(response, dict) or response.get("error"):
        raise ValueError("worker operation refused")
    if response.get("mode") != "stopped":
        info = (RUNTIME / f"{slot}.sock").lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("live worker bridge identity unknown")
    return response


def verify_at(base, version):
    module = deployer()
    # An uncertain upstream completion must not be replayed by a managed probe.
    module.PROBE_ATTEMPTS = 1
    return module.verify(version, base=base)


def slot_base(slot):
    return f"http://127.0.0.1:{PORTS[slot]}"


def slot_configuration(slot, entry):
    return {
        "slot": slot,
        "version": entry["version"],
        "release": entry["release"],
        "port": PORTS[slot],
        "runtime": str(RUNTIME),
        "dataDir": str(DB.parent.parent),
    }


def verify_configuration(slot, entry):
    if read_json(STATE_DIR / f"{slot}.json") != slot_configuration(slot, entry):
        raise ValueError("slot configuration identity differs")
    concrete_release(Path(entry["release"]))


def worker_status(slot, entry):
    verify_configuration(slot, entry)
    response = control(slot, "status")
    if (
        not isinstance(response, dict)
        or response.get("error")
        or response.get("slot") != slot
        or response.get("version") != entry["version"]
    ):
        raise ValueError("unknown worker identity")
    if (
        response.get("mode") not in {"ready", "active", "draining", "stopped"}
        or type(response.get("connections")) is not int
        or response["connections"] < 0
    ):
        raise ValueError("unknown worker connection status")
    if type(response.get("appPid")) is not int or response["appPid"] <= 0:
        raise ValueError("unknown worker process identity")
    if response["mode"] == "stopped" and response["connections"] != 0:
        raise ValueError("stopped worker still reports connections")
    if response["mode"] != "stopped":
        work = response.get("appWork")
        if (
            not isinstance(work, dict)
            or work.get("initialized") is not True
            or work.get("unknown") is not False
            or any(type(work.get(key)) is not int or work[key] < 0 for key in WORK_KEYS)
        ):
            raise ValueError("unknown worker application work")
    return response


def require_probe(slot, entry):
    if verify_at(slot_base(slot), entry["version"]) is not None:
        raise ValueError("private authenticated readiness probe failed")


def read_state():
    state = read_json(STATE_FILE)
    if (
        not isinstance(state, dict)
        or type(state.get("schema")) is not int
        or state["schema"] != 1
        or state.get("active") not in PORTS
        or set(state.get("slots", {})) != set(PORTS)
    ):
        raise ValueError("corrupted deployment journal")
    for entry in state["slots"].values():
        if entry is not None:
            verify_slot_journal(entry)
    pending = state.get("pending")
    if pending is not None:
        if (
            not isinstance(pending, dict)
            or pending.get("phase") not in {"prepared", "verified", "switched"}
            or pending.get("old") not in PORTS
            or pending.get("new") not in PORTS
            or pending["old"] == pending["new"]
        ):
            raise ValueError("corrupted pending transaction")
    if state.get("enrollment") not in {
        None,
        "preparing",
        "maintenance-prepared",
        "legacy-stopped",
        "routed",
        "complete",
    }:
        raise ValueError("corrupted enrollment checkpoint")
    return state


def verify_slot_journal(entry):
    if (
        not isinstance(entry, dict)
        or entry.get("mode") not in MODES
        or not re.fullmatch("[a-f0-9]{64}", entry.get("digest", ""))
    ):
        raise ValueError("corrupted slot journal")
    if concrete_release(Path(entry["release"])) != valid_version(entry["version"]):
        raise ValueError("journal release identity differs")
    if entry.get("lifecycle") not in {
        None,
        "config-pending",
        "bootstrap-pending",
        "started",
        "stopping",
        "stopped",
        "cleanup",
        "startup-cleanup",
    }:
        raise ValueError("corrupted lifecycle checkpoint")


def job_present(slot):
    # Enumerate the exact GUI domain: a failed per-job lookup is not absence proof.
    # ponytail: strict launchctl layout; qualify guests before adding layouts.
    domain = f"gui/{os.getuid()}"
    result = subprocess.run(
        ["launchctl", "print", domain],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    blocks = re.findall(
        r"^([ \t]*)services = \{\n(.*?)^\1\}",
        result.stdout,
        re.MULTILINE | re.DOTALL,
    )
    if (
        result.returncode
        or not result.stdout.startswith(f"{domain} = {{\n")
        or len(blocks) != 1
    ):
        raise ValueError("launchd job population unreadable")
    labels = []
    for line in blocks[0][1].splitlines():
        fields = line.split()
        if (
            len(fields) != 3
            or not re.fullmatch(r"-|[0-9]+", fields[0])
            or not re.fullmatch(r"-|-?[0-9]+", fields[1])
        ):
            raise ValueError("launchd job population malformed")
        labels.append(fields[2])
    if len(labels) != len(set(labels)):
        raise ValueError("launchd job population ambiguous")
    label = job_label(slot)
    return label in labels


def verify_job(slot, entry):
    verify_configuration(slot, entry)
    path = STATE_DIR / f"{slot}.plist"
    private_file(path)
    if sha256(path) != entry.get("job_sha256"):
        raise ValueError("owned launchd configuration differs")
    with path.open("rb") as stream:
        plist = plistlib.load(stream)
    arguments = [
        executable("node"),
        str(HERE / "9router_worker.cjs"),
        str(STATE_DIR / f"{slot}.json"),
    ]
    if (
        plist.get("Label") != job_label(slot)
        or plist.get("ProgramArguments") != arguments
    ):
        raise ValueError("owned launchd arguments differ")
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{plist['Label']}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("loaded launchd identity unreadable")
    # Bind the loaded job, not just the on-disk plist, to the exact concrete arguments.
    program = re.findall(r"^\s*program = (.+)$", result.stdout, re.MULTILINE)
    blocks = re.findall(
        r"^\s*arguments = \{\n(.*?)^\s*\}", result.stdout, re.MULTILINE | re.DOTALL
    )
    paths = re.findall(r"^\s*path = (.+)$", result.stdout, re.MULTILINE)
    if (
        paths != [str(path)]
        or program != [arguments[0]]
        or len(blocks) != 1
        or [line.strip() for line in blocks[0].splitlines()] != arguments
    ):
        raise ValueError("loaded launchd identity differs")


def lifecycle_checkpoint(slot, entry, phase):
    state = read_state()
    recorded = state["slots"][slot]
    if recorded is None or any(
        recorded.get(key) != entry.get(key) for key in ("release", "version", "digest")
    ):
        raise ValueError("lifecycle journal identity differs")
    entry["lifecycle"] = phase
    state["slots"][slot] = entry
    write_state(state)


def cleanup_slot(slot, entry):
    if slot == route_slot() or job_present(slot):
        raise ValueError("slot cleanup owner or route changed")
    identities = entry.get("runtime_identity", {})
    for suffix in ("sock", "ctl"):
        path = RUNTIME / f"{slot}.{suffix}"
        if os.path.lexists(path):
            info = path.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or identities.get(suffix) != [info.st_dev, info.st_ino]
            ):
                raise ValueError("stopped slot path identity unknown")
            path.unlink()
    sync_directory(RUNTIME)
    config = STATE_DIR / f"{slot}.json"
    if os.path.lexists(config):
        verify_configuration(slot, entry)
        config.unlink()
    sync_directory(STATE_DIR)


def literal_environment():
    source = HOME / "env.sh"
    private_file(source)
    env = {}
    # ponytail: literal exports only; richer launchers need a validated env file.
    for line in source.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(
            r"\s*export ([A-Z][A-Z0-9_]*)="
            r"""(?:"([^"\\$`\r\n]*)"|'([^'\\$`\r\n]*)'|([A-Za-z0-9_./:-]+))\s*""",
            line,
        )
        if match is None:
            raise ValueError("shared environment requires absolute literal exports")
        key = match[1]
        value = next(value for value in match.groups()[1:] if value is not None)
        if key in env:
            raise ValueError("shared environment has duplicate keys")
        env[key] = value
    return env


def shared_environment(*, enrollment=False):
    path = STATE_DIR / "environment.json"
    if not enrollment:
        env = read_json(path)
        if sha256(path) != read_state().get("environment_sha256"):
            raise ValueError("enrolled shared environment changed")
    else:
        env = literal_environment()
    verify_shared_environment(env)
    if enrollment:
        if os.path.lexists(path):
            if read_json(path) != env:
                raise ValueError("prepared shared environment differs")
        else:
            atomic_json(path, env)
    return env


def verify_shared_environment(env):
    allowed = {
        "DATA_DIR",
        "JWT_SECRET",
        "API_KEY_SECRET",
        "MACHINE_ID_SALT",
        "INITIAL_PASSWORD",
        "ENABLE_REQUEST_LOGS",
        "NODE_ENV",
        "NINEROUTER_DBG_CHUNKS",
        "NINEROUTER_NO_TRAY",
        "REQUEST_DETAILS_MODE",
    }
    if (
        not isinstance(env, dict)
        or not set(env) <= allowed
        or any(
            not isinstance(value, str) or any(char in value for char in "$`\x00\r\n")
            for value in env.values()
        )
    ):
        raise ValueError("shared environment is not a supported literal configuration")
    if (
        env.get("DATA_DIR") != str(DB.parent.parent)
        or not env.get("JWT_SECRET")
        or not env.get("INITIAL_PASSWORD")
    ):
        raise ValueError("legacy database and dashboard continuity unproven")


def start_worker(slot, entry):
    if slot not in PORTS:
        raise ValueError("invalid worker slot")
    if any(
        len(os.fsencode(RUNTIME / name)) > 103
        for name in ("active.sock", f"{slot}.sock", f"{slot}.ctl")
    ):
        raise ValueError("runtime socket pathname too long")
    for suffix in ("sock", "ctl", "failed.json"):
        if os.path.lexists(RUNTIME / f"{slot}.{suffix}"):
            raise ValueError("slot runtime path occupied")
    if job_present(slot):
        raise ValueError("slot launchd job already occupied")
    lifecycle_checkpoint(slot, entry, "config-pending")
    # Reservation is only a preflight. The worker also refuses bind collisions.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", PORTS[slot]))
    config = {
        "slot": slot,
        "version": entry["version"],
        "release": entry["release"],
        "port": PORTS[slot],
        "runtime": str(RUNTIME),
        "dataDir": str(DB.parent.parent),
    }
    config_path = STATE_DIR / f"{slot}.json"
    if os.path.lexists(config_path):
        verify_configuration(slot, entry)
    else:
        atomic_json(config_path, config)
    env = {
        **shared_environment(),
        "HOME": str(Path.home()),
        "NINEROUTER_HOTSWAP_MANIFEST": str(
            Path(entry["release"]) / "app/hotswap-manifest.json"
        ),
        "NINEROUTER_HOTSWAP_ENROLLED_MANIFEST": str(STATE_DIR / "manifest.json"),
        "NINEROUTER_HOTSWAP_REFRESH_DB": str(STATE_DIR / "refresh.sqlite"),
    }
    label = job_label(slot)
    plist = {
        "Label": label,
        "ProgramArguments": [
            executable("node"),
            str(HERE / "9router_worker.cjs"),
            str(config_path),
        ],
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "EnvironmentVariables": env,
        "StandardOutPath": str(STATE_DIR / f"{slot}.stdout.log"),
        "StandardErrorPath": str(STATE_DIR / f"{slot}.stderr.log"),
    }
    plist_path = STATE_DIR / f"{slot}.plist"
    if os.path.lexists(plist_path):
        private_file(plist_path)
    if read_state().get("enrollment") not in {None, "complete"}:
        publish_enrollment_file(plist_path, plistlib.dumps(plist))
    else:
        fd = os.open(
            plist_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(plist, stream)
            stream.flush()
            os.fsync(stream.fileno())
    for name in ("stdout", "stderr"):
        log_path = STATE_DIR / f"{slot}.{name}.log"
        if os.path.lexists(log_path):
            private_file(log_path)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    sync_directory(STATE_DIR)
    entry["job_sha256"] = sha256(plist_path)
    lifecycle_checkpoint(slot, entry, "bootstrap-pending")
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("slot launchd bootstrap refused")
    verify_job(slot, entry)
    lifecycle_checkpoint(slot, entry, "started")
    await_worker_readiness(slot, entry)


def await_worker_readiness(slot, entry):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            response = worker_status(slot, entry)
            if response["mode"] in {"ready", "active"}:
                return
        except (OSError, ValueError):
            # Retry transient status errors after the normal delay, until the deadline.
            pass
        time.sleep(0.2)
    raise ValueError("slot startup readiness unproven")


def bootout_worker(slot):
    result = subprocess.run(
        [
            "launchctl",
            "bootout",
            f"gui/{os.getuid()}/{job_label(slot)}",
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise ValueError("exact stopped slot bootout refused")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not job_present(slot):
            return
        time.sleep(0.1)
    raise ValueError("stopped launchd job remains loaded")


def convenience_pointer(entry):
    temporary = LINK.with_name(f".9router-pointer-{os.getpid()}")
    if os.path.lexists(LINK) and not LINK.is_symlink():
        raise ValueError("convenience pointer is not a symlink")
    try:
        temporary.symlink_to(entry["release"])
        os.replace(temporary, LINK)
        sync_directory(LINK.parent)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def journal_log(state, action):
    path = STATE_DIR / "deploys.log"
    if os.path.lexists(path):
        private_file(path)
    value = {
        "ts": deployer().now(),
        "action": action,
        "version": state["slots"][state["active"]]["version"],
        "result": "ok",
        "active_slot": state["active"],
        "slots": {
            slot: entry["mode"] if entry else None
            for slot, entry in state["slots"].items()
        },
    }
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(STATE_DIR)
    print(json.dumps(value))


def finish_route(state, active, action):
    old = "b" if active == "a" else "a"
    state["active"] = active
    state["slots"][active]["mode"] = "active"
    if state["slots"][old] is not None and state["slots"][old]["mode"] != "stopped":
        response = control(old, "drain")
        if (
            not isinstance(response, dict)
            or response.get("slot") != old
            or response.get("version") != state["slots"][old]["version"]
            or response.get("mode") != "draining"
        ):
            raise ValueError("old slot drain acknowledgement unproven")
        state["slots"][old]["mode"] = "draining"
    if state.get("pending") is None:
        state["pending"] = {
            "phase": "switched",
            "old": old,
            "new": active,
            "action": action,
        }
    # The switched journal survives pointer/log faults for reconciliation.
    write_state(state)
    convenience_pointer(state["slots"][active])
    journal_log(state, action)
    state["pending"] = None
    write_state(state)


def verify_unloaded_slot(slot, entry, phase):
    config = STATE_DIR / f"{slot}.json"
    if os.path.lexists(config):
        verify_configuration(slot, entry)
    if phase in {"config-pending", "bootstrap-pending", "startup-cleanup"} and any(
        os.path.lexists(RUNTIME / f"{slot}.{suffix}")
        for suffix in ("sock", "ctl", "failed.json")
    ):
        raise ValueError("unstarted slot has unknown runtime owner")
    if (
        phase in {"stopped", "cleanup"}
        and type(entry.get("stopped_app_pid")) is not int
    ):
        raise ValueError("retirement acknowledgement missing")


def verify_retirement_recovery(slot, entry, active, phase, response):
    if slot == active or response["appPid"] != entry.get("stopped_app_pid"):
        raise ValueError("retirement worker identity differs")
    if response["mode"] != "stopped" and (
        phase != "stopping"
        or response["mode"] != "draining"
        or response["connections"]
        or any(response["appWork"][key] for key in WORK_KEYS)
    ):
        raise ValueError("retirement stopped state unproven")
    verify_job(slot, entry)


def slot_needs_recovery(slot, entry, active):
    validate_release(Path(entry["release"]), entry["digest"])
    phase = entry.get("lifecycle")
    if slot != active and phase in {
        "config-pending",
        "bootstrap-pending",
        "startup-cleanup",
        "stopped",
        "cleanup",
    }:
        present = job_present(slot)
        if not present:
            verify_unloaded_slot(slot, entry, phase)
            return True
        if phase in {"config-pending", "startup-cleanup", "cleanup"}:
            raise ValueError("unexpected loaded slot during lifecycle recovery")
        verify_job(slot, entry)
    elif (
        entry["mode"] == "starting"
        and slot != active
        and phase is None
        and not os.path.lexists(STATE_DIR / f"{slot}.json")
    ):
        if job_present(slot) or any(
            os.path.lexists(RUNTIME / f"{slot}.{suffix}")
            for suffix in ("sock", "ctl", "failed.json")
        ):
            raise ValueError("prepared slot has unknown owner")
        return True
    return inspect_recovery_worker(slot, entry, active, phase)


def inspect_recovery_worker(slot, entry, active, phase):
    response = worker_status(slot, entry)
    if phase in {"stopping", "stopped"}:
        verify_retirement_recovery(slot, entry, active, phase, response)
        return True
    if response["mode"] == "stopped":
        if slot == active:
            raise ValueError("public route targets a stopped worker")
        entry["mode"] = "stopped"
    return False


def recover_slots(state, recover):
    for slot in recover:
        entry = state["slots"][slot]
        if entry.get("lifecycle") in {"stopping", "stopped"} and job_present(slot):
            retire_slot(state, slot)
        else:
            entry["lifecycle"] = (
                "cleanup"
                if entry.get("lifecycle") in {"stopped", "cleanup"}
                else "startup-cleanup"
            )
            write_state(state)
            cleanup_slot(slot, entry)
            state["slots"][slot] = None
            write_state(state)


def resume_routed_worker(state, active):
    # Rollback's rename can leave the authoritative worker draining.
    # Both identities must be proven before resuming without a restart.
    routed = worker_status(active, state["slots"][active])
    if routed["mode"] == "stopped":
        raise ValueError("public worker stopped during reconciliation")
    resumed = routed["mode"] == "draining"
    if resumed:
        response = control(active, "resume")
        if (
            not isinstance(response, dict)
            or response.get("slot") != active
            or response.get("version") != state["slots"][active]["version"]
            or response.get("mode") not in {"ready", "active"}
        ):
            raise ValueError("recovery resume acknowledgement unproven")
        require_probe(active, state["slots"][active])
    return resumed


def reconcile_locked(*, rollback=False):
    state = read_state()
    if state.get("enrollment") not in {None, "complete"}:
        if rollback:
            raise ValueError("maintenance enrollment requires reconciliation")
        return resume_enrollment(state)
    active = route_slot()
    if state["slots"][active] is None:
        raise ValueError("route has no known slot identity")
    recover = []
    # Prove every occupied identity before ANY cleanup, drain or route mutation.
    for slot, entry in state["slots"].items():
        if entry is not None and slot_needs_recovery(slot, entry, active):
            recover.append(slot)
    if rollback:
        # A failed candidate probe cannot prevent escape to a healthy target.
        if recover:
            raise ValueError(
                "rollback alternate identity needs lifecycle reconciliation"
            )
        return state
    try:
        require_probe(active, state["slots"][active])
    except EXPECTED_ERRORS:
        recover_failed_deployment(state, active)
        return state
    recover_slots(state, recover)
    return reconcile_serving_route(state, active)


def reconcile_serving_route(state, active):
    resumed = resume_routed_worker(state, active)
    pending = state.get("pending")
    pointer_differs = not LINK.is_symlink() or LINK.resolve() != Path(
        state["slots"][active]["release"]
    )
    if pending or state["active"] != active or resumed or pointer_differs:
        if (
            verify_at("http://127.0.0.1:20128", state["slots"][active]["version"])
            is not None
        ):
            recover_failed_deployment(state, active)
            return state
        finish_route(state, active, "reconcile")
    return state


def recover_failed_deployment(state, active):
    pending = state.get("pending")
    if not pending or pending.get("action") != "deploy" or pending["new"] != active:
        raise ValueError("recovery readiness failed; serving route preserved")
    rollback_locked(state, pending["old"], "automatic-rollback")


def capture_retirement_sockets(slot, entry):
    entry["runtime_identity"] = {}
    for suffix in ("sock", "ctl"):
        path = RUNTIME / f"{slot}.{suffix}"
        if os.path.lexists(path):
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("retirement socket identity unknown")
            entry["runtime_identity"][suffix] = [info.st_dev, info.st_ino]


def verify_stopped_acknowledgement(slot, entry, app_pid, response):
    if (
        not isinstance(response, dict)
        or response.get("slot") != slot
        or response.get("version") != entry["version"]
        or response.get("mode") != "stopped"
        or type(response.get("connections")) is not int
        or response["connections"] != 0
        or type(response.get("appPid")) is not int
        or response["appPid"] != app_pid
    ):
        raise ValueError("worker stopped acknowledgement unproven")


def retire_slot(state, slot):
    entry = state["slots"][slot]
    if entry is None:
        if (
            job_present(slot)
            or os.path.lexists(STATE_DIR / f"{slot}.json")
            or any(
                os.path.lexists(RUNTIME / f"{slot}.{suffix}")
                for suffix in ("sock", "ctl", "failed.json")
            )
        ):
            raise ValueError("unallocated slot has unknown runtime identity")
        return
    if slot == route_slot():
        raise ValueError("cannot retire the serving slot")
    verify_job(slot, entry)
    response = worker_status(slot, entry)
    if response["mode"] != "stopped" or "runtime_identity" not in entry:
        capture_retirement_sockets(slot, entry)
    if response["mode"] != "stopped":
        work = response["appWork"]
        if (
            response["mode"] != "draining"
            or response["connections"] != 0
            or any(work[key] != 0 for key in WORK_KEYS)
        ):
            raise ValueError("inactive slot still has accepted work")
        app_pid = response["appPid"]
        entry["stopped_app_pid"] = app_pid
        entry["lifecycle"] = "stopping"
        write_state(state)
        response = control(slot, "stop")
        verify_stopped_acknowledgement(slot, entry, app_pid, response)
    elif entry.get("stopped_app_pid", response["appPid"]) != response["appPid"]:
        raise ValueError("stopped worker identity differs")
    entry["stopped_app_pid"] = response["appPid"]
    entry["mode"], entry["lifecycle"] = "stopped", "stopped"
    write_state(state)
    bootout_worker(slot)
    entry["lifecycle"] = "cleanup"
    write_state(state)
    cleanup_slot(slot, entry)
    state["slots"][slot] = None
    write_state(state)


def rollback_locked(state, target, action="rollback"):
    current = route_slot()
    if target == current or state["slots"].get(target) is None:
        raise ValueError("rollback target is not an occupied alternate slot")
    for slot in (current, target):
        response = worker_status(slot, state["slots"][slot])
        if response["mode"] == "stopped":
            raise ValueError("rollback worker stopped")
    state["pending"] = {
        "phase": "prepared",
        "old": current,
        "new": target,
        "action": action,
    }
    write_state(state)
    response = control(target, "resume")
    if (
        not isinstance(response, dict)
        or response.get("slot") != target
        or response.get("version") != state["slots"][target]["version"]
        or response.get("mode") not in {"ready", "active"}
    ):
        raise ValueError("rollback resume acknowledgement unproven")
    require_probe(target, state["slots"][target])
    if worker_status(target, state["slots"][target])["mode"] not in {"ready", "active"}:
        raise ValueError("rollback target is not ready")
    state["pending"] = {
        "phase": "verified",
        "old": current,
        "new": target,
        "action": action,
    }
    write_state(state)
    replace_route(RUNTIME, target)
    state["pending"]["phase"] = "switched"
    write_state(state)
    if (
        verify_at("http://127.0.0.1:20128", state["slots"][target]["version"])
        is not None
    ):
        raise ValueError("rollback public readiness failed; route retained")
    finish_route(state, target, action)


def deploy_locked(dest, digest, state):
    manifest = validate_release(dest, digest)
    old = route_slot()
    new = "b" if old == "a" else "a"
    retire_slot(state, new)
    snapshot = STATE_DIR / f"pre-deploy-{manifest['version']}-{digest}.sqlite"
    # ponytail: pre-journal artifacts are retained, never guessed disposable.
    # Retry with a new release version; maintenance may inspect old artifacts.
    if os.path.lexists(snapshot):
        raise ValueError("deployment snapshot already exists; bump version")
    snapshot_db(snapshot)
    stage_assets(dest)
    entry = {
        "release": str(dest),
        "version": manifest["version"],
        "digest": digest,
        "mode": "starting",
    }
    state["slots"][new] = entry
    state["pending"] = {"phase": "prepared", "old": old, "new": new, "action": "deploy"}
    write_state(state)
    start_worker(new, entry)
    require_probe(new, entry)
    entry["mode"] = "ready"
    state["pending"]["phase"] = "verified"
    write_state(state)
    for slot in (old, new):
        require_probe(slot, state["slots"][slot])
        response = worker_status(slot, state["slots"][slot])
        if response["mode"] not in {"ready", "active"}:
            raise ValueError("pre-switch worker is not ready")
    replace_route(RUNTIME, new)
    state["pending"]["phase"] = "switched"
    write_state(state)
    if verify_at("http://127.0.0.1:20128", entry["version"]) is not None:
        try:
            rollback_locked(state, old, "automatic-rollback")
        except EXPECTED_ERRORS:
            return refusal(
                "automatic rollback",
                "last serving route retained; both slots preserved",
            )
        return refusal(
            "candidate public readiness",
            "rolled back; candidate sessions drain naturally",
        )
    finish_route(state, new, "deploy")
    return 0


def deploy_release(dest: Path, digest: str) -> int:
    try:
        with deployment_lock():
            return deploy_locked(Path(dest), digest, reconcile_locked())
    except EXPECTED_ERRORS as error:
        return refusal("release transaction", error)


def deploy_tarball(tgz: Path) -> int:
    try:
        with deployment_lock():
            state = reconcile_locked()
            digest = sha256(tgz)
            qualification(digest)  # No guest bypass for managed releases.
            module = deployer()
            version = valid_version(module.tgz_version(tgz))
            root = RELEASES / version
            if os.path.lexists(root):
                raise ValueError("release directory exists; bump version")
            inactive = "b" if route_slot() == "a" else "a"
            # Refuse busy slots before creating the immutable release tree.
            entry = state["slots"][inactive]
            if entry:
                response = worker_status(inactive, entry)
                if response["mode"] != "stopped" and (
                    response["mode"] != "draining"
                    or response["connections"]
                    or any(response["appWork"][key] for key in WORK_KEYS)
                ):
                    raise ValueError("inactive slot still busy")
            private_directory(RELEASES)
            if (
                shutil.disk_usage(RELEASES).free
                < DB.stat().st_size * 2 + tgz.stat().st_size + 16 * 1024 * 1024
            ):
                raise ValueError("insufficient install and snapshot disk space")
            root.mkdir(mode=0o700)
            # npm consumes a private, hash-checked copy, not a mutable caller pathname.
            staged = root / "qualified-package.tgz"
            fd = os.open(
                staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as outgoing, tgz.open("rb") as incoming:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            sync_directory(root)
            if sha256(staged) != digest:
                raise ValueError("tarball changed after qualification check")
            snapshot_db(root / "pre-deploy-data.sqlite")
            # Native SQLite startup needs no hooks that mutate shared runtimes.
            result = subprocess.run(
                [
                    "npm",
                    "install",
                    "-g",
                    "--ignore-scripts",
                    "--prefix",
                    str(root),
                    str(staged),
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise ValueError("candidate install failed; old route untouched")
            return deploy_locked(module.release_dir(version), digest, state)
    except EXPECTED_ERRORS as error:
        return refusal("tarball transaction", error)


def rollback_release(version: str | None) -> int:
    try:
        if version is not None:
            valid_version(version)
        with deployment_lock():
            state = reconcile_locked(rollback=True)
            target = "b" if route_slot() == "a" else "a"
            entry = state["slots"][target]
            if entry is None or (version is not None and entry["version"] != version):
                raise ValueError("rollback needs the retained alternate release")
            validate_release(Path(entry["release"]), entry["digest"])
            rollback_locked(state, target)
            return 0
    except EXPECTED_ERRORS as error:
        return refusal("rollback", error)


def reconcile() -> int:
    try:
        with deployment_lock():
            reconcile_locked()
            return 0
    except EXPECTED_ERRORS as error:
        return refusal("reconciliation", error)


def status() -> dict:
    result = {"route": None, "state": None, "workers": {}, "errors": []}
    try:
        result["route"] = route_slot()
        result["state"] = read_state()
    except EXPECTED_ERRORS:
        result["errors"].append("route or journal unreadable")
    if result["state"]:
        for slot, entry in result["state"]["slots"].items():
            if entry:
                diagnose_worker(result, slot, entry)
    return result


def diagnose_worker(result, slot, entry):
    try:
        result["workers"][slot] = worker_status(slot, entry)
        if slot == result["route"] and result["workers"][slot]["mode"] == "stopped":
            result["errors"].append("public route targets a stopped worker")
    except EXPECTED_ERRORS:
        result["errors"].append(f"slot {slot} identity or work unknown")


def verify_enrollment_slots():
    for slot in PORTS:
        if os.path.lexists(STATE_DIR / f"{slot}.json") or any(
            os.path.lexists(RUNTIME / f"{slot}.{suffix}")
            for suffix in ("sock", "ctl", "failed.json")
        ):
            raise ValueError("enrollment slot has unknown owner")


def enroll(args):
    if not args.acknowledge_maintenance:
        return refusal("enrollment", "explicit maintenance acknowledgement required")
    if not args.release or not args.digest:
        return refusal(
            "enrollment", "new qualified protocol-1 release and digest required"
        )
    try:
        dest = Path(args.release)
        # Verify all installed bytes before enrollment executes any package helper.
        version = validate_package(dest, args.digest)["version"]
        if os.path.lexists(STATE_FILE):
            raise ValueError("already enrolled or enrollment journal unreadable")
        for directory in (HOME, STATE_DIR, RUNTIME):
            if not os.path.lexists(directory):
                directory.mkdir(mode=0o700)
            private_directory(directory)
        with deployment_lock():
            if os.path.lexists(STATE_FILE) or os.path.lexists(RUNTIME / "active.sock"):
                raise ValueError("enrollment state or route already exists")
            verify_enrollment_slots()
            shared_environment(enrollment=True)
            entry = {
                "release": str(dest),
                "version": version,
                "digest": args.digest,
                "mode": "starting",
            }
            state = {
                "schema": 1,
                "active": "a",
                "slots": {"a": entry, "b": None},
                "pending": None,
                "enrollment": "preparing",
                "environment_sha256": sha256(STATE_DIR / "environment.json"),
            }
            write_state(state)
            resume_enrollment(state)
            return 0
    except EXPECTED_ERRORS as error:
        return refusal("enrollment", error)


def publish_enrollment_file(path, data):
    if os.path.lexists(path):
        private_file(path)
        if path.read_bytes() != data:
            raise ValueError("enrollment artifact identity differs")
        sync_directory(path.parent)
        return
    fd, temporary = tempfile.mkstemp(prefix=".enrollment-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def prepare_enrollment_refresh_store(dest):
    refresh = STATE_DIR / "refresh.sqlite"
    # ponytail: incomplete pre-journal stores refuse. Recover through separate
    # maintenance; never reset generations.
    if os.path.lexists(refresh):
        node_managed(dest, "m.openRefreshStore(a[0]).close();", refresh)
    else:
        fd, temporary = tempfile.mkstemp(prefix=".refresh-", dir=STATE_DIR)
        os.close(fd)
        os.unlink(temporary)
        try:
            node_managed(
                dest,
                "m.enrollRefreshStore(a[0]); const r=m.openRefreshStore(a[0]); "
                "r.exec('PRAGMA wal_checkpoint(TRUNCATE)'); r.close();",
                temporary,
            )
            for suffix in ("-wal", "-shm", "-journal"):
                if os.path.lexists(temporary + suffix):
                    raise ValueError("refresh store publication has live sidecars")
            private_file(Path(temporary))
            with Path(temporary).open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, refresh)
            sync_directory(STATE_DIR)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)


def prepare_enrollment(state, dest, entry):
    manifest = json.loads((dest / "app/hotswap-manifest.json").read_text())
    path = STATE_DIR / "manifest.json"
    if os.path.lexists(path):
        if read_json(path) != manifest:
            raise ValueError("enrollment manifest identity differs")
    else:
        atomic_json(path, manifest)
    prepare_enrollment_refresh_store(dest)
    validate_release(dest, entry["digest"])
    snapshot = STATE_DIR / "pre-enrollment.sqlite"
    if os.path.lexists(snapshot):
        private_file(snapshot)
        # ponytail: unbound historical snapshots refuse. Verify them through
        # separate maintenance before adoption.
        if sha256(snapshot) != state.get("enrollment_snapshot_sha256"):
            raise ValueError("enrollment snapshot identity unproven")
    else:
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=STATE_DIR)
        os.close(fd)
        os.unlink(temporary)
        try:
            snapshot_db(Path(temporary))
            state["enrollment_snapshot_sha256"] = sha256(Path(temporary))
            write_state(state)
            os.replace(temporary, snapshot)
            sync_directory(STATE_DIR)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    stage_assets(dest)
    state["enrollment"] = "maintenance-prepared"
    write_state(state)


def stop_enrollment_legacy_service(state):
    if QUALIFICATION_SCOPE is not None:
        state["enrollment"] = "legacy-stopped"
        write_state(state)
        return  # Isolated test jobs never alter a pre-existing guest baseline job.
    if job_present("com.lfenergy.9router"):
        result = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/com.lfenergy.9router"],
            capture_output=True,
            check=False,
        )
        if result.returncode or job_present("com.lfenergy.9router"):
            raise ValueError("legacy service maintenance bootout unproven")
    state["enrollment"] = "legacy-stopped"
    write_state(state)


def resume_enrollment(state):
    entry = state["slots"]["a"]
    if state["active"] != "a" or entry is None or state["slots"]["b"] is not None:
        raise ValueError("enrollment journal identity differs")
    dest = Path(entry["release"])
    validate_package(dest, entry["digest"])
    shared_environment()
    if state["enrollment"] == "preparing":
        prepare_enrollment(state, dest, entry)
    if state["enrollment"] == "maintenance-prepared":
        stop_enrollment_legacy_service(state)
    if state["enrollment"] not in {"legacy-stopped", "routed"}:
        raise ValueError("unknown enrollment checkpoint")
    validate_release(dest, entry["digest"])
    if job_present("a"):
        verify_job("a", entry)
        await_worker_readiness("a", entry)
    else:
        start_worker("a", entry)
    state = read_state()
    entry = state["slots"]["a"]
    if worker_status("a", entry)["mode"] not in {"ready", "active"}:
        raise ValueError("enrollment worker readiness unproven")
    require_probe("a", entry)
    if os.path.lexists(RUNTIME / "active.sock"):
        if route_slot() != "a":
            raise ValueError("enrollment route identity differs")
    else:
        replace_route(RUNTIME, "a")
    state["enrollment"] = "routed"
    write_state(state)
    proxy_enrollment()
    reason = verify_at("http://127.0.0.1:20128", entry["version"])
    if reason is not None:
        raise ValueError(f"enrollment public readiness failed: {reason}")
    entry["mode"] = "active"
    convenience_pointer(entry)
    # ponytail: recovery may repeat audit lines. Add transaction IDs when
    # exactly-once logs are required.
    journal_log(state, "enroll")
    state["enrollment"] = "complete"
    write_state(state)
    return state


def proxy_enrollment():
    config = (
        (HERE / "templates/9router.Caddyfile")
        .read_text()
        .replace("__STATE__", str(STATE_DIR))
        .replace("__RUNTIME__", str(RUNTIME))
    )
    path = STATE_DIR / "Caddyfile"
    publish_enrollment_file(path, config.encode())
    plist = {
        "Label": f"{JOB_PREFIX}-proxy",
        "ProgramArguments": [
            executable("caddy"),
            "run",
            "--config",
            str(path),
            "--adapter",
            "caddyfile",
        ],
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(STATE_DIR / "proxy.stdout.log"),
        "StandardErrorPath": str(STATE_DIR / "proxy.stderr.log"),
    }
    path = STATE_DIR / "proxy.plist"
    publish_enrollment_file(path, plistlib.dumps(plist))
    for name in ("stdout", "stderr"):
        log_path = STATE_DIR / f"proxy.{name}.log"
        if os.path.lexists(log_path):
            private_file(log_path)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    sync_directory(STATE_DIR)
    label = plist["Label"]
    if not job_present(label):
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise ValueError("stable proxy enrollment bootstrap refused")
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    arguments = plist["ProgramArguments"]
    blocks = re.findall(
        r"^\s*arguments = \{\n(.*?)^\s*\}", result.stdout, re.MULTILINE | re.DOTALL
    )
    if (
        result.returncode
        or re.findall(r"^\s*path = (.+)$", result.stdout, re.MULTILINE) != [str(path)]
        or re.findall(r"^\s*program = (.+)$", result.stdout, re.MULTILINE)
        != [arguments[0]]
        or len(blocks) != 1
        or [line.strip() for line in blocks[0].splitlines()] != arguments
    ):
        raise ValueError("loaded proxy identity differs")


def restore_qualification_runtime():
    state = read_state()
    if state.get("enrollment") != "complete" or state.get("pending") is not None:
        raise ValueError("persistent qualification transaction incomplete")
    if job_present(f"{JOB_PREFIX}-proxy") or any(job_present(slot) for slot in PORTS):
        raise ValueError("qualification jobs already loaded; use reconciliation")
    if os.path.lexists(RUNTIME):
        private_directory(RUNTIME)
        if any(RUNTIME.iterdir()):
            raise ValueError("runtime artifacts remain; restoration ownership unknown")
    else:
        RUNTIME.mkdir(mode=0o700)


def validate_restorable_slots(state, active):
    for slot, recorded in state["slots"].items():
        if recorded is None:
            continue
        allowed = {"active"} if slot == active else {"draining", "stopped"}
        if recorded["mode"] not in allowed:
            raise ValueError("persistent slot lifecycle is not restorable")
        validate_release(Path(recorded["release"]), recorded["digest"])
        if recorded["mode"] != "stopped":
            verify_configuration(slot, recorded)


def start_restored_slots(state):
    for slot, recorded in state["slots"].items():
        if recorded is not None and recorded["mode"] != "stopped":
            start_worker(slot, recorded)


def drain_restored_slots():
    for slot, recorded in read_state()["slots"].items():
        if recorded is not None and recorded["mode"] == "draining":
            control(slot, "drain")


def restore_qualification():
    """Restore only this isolated guest's jobs after a VM activation."""
    if QUALIFICATION_SCOPE is None:
        return refusal("restore", "isolated qualification scope required")
    try:
        restore_qualification_runtime()
        with deployment_lock():
            state = read_state()
            if (
                state.get("enrollment") != "complete"
                or state.get("pending") is not None
            ):
                raise ValueError("persistent qualification transaction incomplete")
            active = state["active"]
            entry = state["slots"][active]
            if entry is None or entry["mode"] != "active":
                raise ValueError("persistent active slot is not restorable")
            validate_restorable_slots(state, active)
            start_restored_slots(state)
            entry = read_state()["slots"][active]
            require_probe(active, entry)
            replace_route(RUNTIME, active)
            drain_restored_slots()
            proxy_enrollment()
            if verify_at("http://127.0.0.1:20128", entry["version"]) is not None:
                raise ValueError("restored public readiness failed")
        return 0
    except EXPECTED_ERRORS as error:
        return refusal("qualification restore", error)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--qualification-scope", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    enrollment = sub.add_parser("enroll")
    enrollment.add_argument("--acknowledge-maintenance", action="store_true")
    enrollment.add_argument("--release")
    enrollment.add_argument("--digest")
    deployment = sub.add_parser("deploy")
    deployment.add_argument("tgz", type=Path)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("version", nargs="?")
    installed = sub.add_parser("deploy-installed")
    installed.add_argument("release", type=Path)
    installed.add_argument("--digest", required=True)
    sub.add_parser("restore")
    sub.add_parser("status")
    sub.add_parser("reconcile")
    args = parser.parse_args(argv)
    if args.qualification_scope is not None:
        try:
            configure_qualification(args.qualification_scope)
        except EXPECTED_ERRORS as error:
            return refusal("qualification scope", error)
    if args.command == "deploy-installed":
        if QUALIFICATION_SCOPE is None:
            return refusal(
                "installed test deployment", "isolated qualification scope required"
            )
        return deploy_release(args.release, args.digest)
    if args.command == "restore":
        return restore_qualification()
    if args.command == "enroll":
        return enroll(args)
    if args.command == "deploy":
        return deploy_tarball(args.tgz.resolve())
    if args.command == "rollback":
        return rollback_release(args.version)
    if args.command == "reconcile":
        return reconcile()
    result = status()
    print(json.dumps(result))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
