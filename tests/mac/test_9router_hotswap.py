"""Task 4 transaction contracts. Execute only in the isolated Namespace guest."""

from __future__ import annotations

import copy
import fcntl
import importlib.util
import io
import json
import os
import plistlib
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mac/9router_hotswap.py"
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


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def hs(tmp_path, monkeypatch):
    assert SCRIPT.is_file(), "Task 4 controller is missing"
    spec = importlib.util.spec_from_file_location("task4_hotswap", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "home/.9router"
    state = home / "hotswap"
    runtime = tmp_path / "runtime"
    releases = home / "releases"
    for directory in (home, state, runtime, releases, home / "qualified", home / "db"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    for name, value in {
        "HOME": home,
        "STATE_DIR": state,
        "STATE_FILE": state / "state.json",
        "LOCK_FILE": state / "deploy.lock",
        "RUNTIME": runtime,
        "RELEASES": releases,
        "QUALIFIED": home / "qualified",
        "DB": home / "db/data.sqlite",
        "LINK": tmp_path / "package-link",
    }.items():
        monkeypatch.setattr(mod, name, value)
    return mod


def fixture_control(hs, events, live, failures, slot, op):
    events.append(f"{op}:{slot}")
    if (slot, op) in failures:
        raise OSError("injected control failure")
    response = live.get(slot)
    if not isinstance(response, dict):
        return response
    if op == "stop":
        assert slot != os.readlink(hs.RUNTIME / "active.sock")[0]
        assert response["connections"] == 0
        assert response["appWork"]["unknown"] is False
        assert all(response["appWork"][key] == 0 for key in WORK_KEYS)
        response["mode"] = "stopped"
    elif op == "drain":
        response["mode"] = "draining"
    elif op == "resume":
        response["mode"] = "ready"
    return copy.deepcopy(response)


@pytest.fixture
def controller(hs, monkeypatch):
    events = []
    releases = {}
    for version in ("v1", "v2", "v3"):
        release = hs.RELEASES / version / "lib/node_modules/9router"
        release.mkdir(parents=True)
        private_json(release / "package.json", {"version": version})
        releases[version] = release
    slots = {
        "a": {
            "release": str(releases["v1"]),
            "version": "v1",
            "digest": "a" * 64,
            "mode": "active",
        },
        "b": None,
    }
    private_json(
        hs.STATE_DIR / "environment.json",
        {
            "DATA_DIR": str(hs.DB.parent.parent),
            "JWT_SECRET": "fixture-signing-value",
            "INITIAL_PASSWORD": "fixture-login-value",
            "NODE_ENV": "production",
        },
    )
    private_json(
        hs.STATE_FILE,
        {
            "schema": 1,
            "active": "a",
            "slots": slots,
            "pending": None,
            "environment_sha256": hs.sha256(hs.STATE_DIR / "environment.json"),
        },
    )
    hs.LINK.symlink_to(releases["v1"])
    sockets = {}
    for slot in ("a",):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(hs.RUNTIME / f"{slot}.sock"))
        sockets[slot] = listener
    (hs.RUNTIME / "active.sock").symlink_to("a.sock")
    live = {
        "a": {
            "slot": "a",
            "version": "v1",
            "mode": "active",
            "connections": 1,
            "appPid": 123,
            "appWork": {
                "initialized": True,
                "unknown": False,
                **dict.fromkeys(WORK_KEYS, 0),
            },
        },
    }
    config = {
        "slot": "a",
        "version": "v1",
        "release": str(releases["v1"]),
        "port": 21128,
        "runtime": str(hs.RUNTIME),
        "dataDir": str(hs.DB.parent.parent),
    }
    private_json(hs.STATE_DIR / "a.json", config)
    failures = set()

    def control(slot, op):
        return fixture_control(hs, events, live, failures, slot, op)

    def start_worker(slot, entry):
        if slot not in sockets:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(hs.RUNTIME / f"{slot}.sock"))
            sockets[slot] = listener
        events.append(f"start:{slot}")
        live[slot] = {
            "slot": slot,
            "version": entry["version"],
            "mode": "ready",
            "connections": 0,
            "appPid": 456,
            "appWork": {
                "initialized": True,
                "unknown": False,
                **dict.fromkeys(WORK_KEYS, 0),
            },
        }
        private_json(
            hs.STATE_DIR / f"{slot}.json",
            {
                **config,
                "slot": slot,
                "version": entry["version"],
                "release": entry["release"],
                "port": 21128 if slot == "a" else 21130,
            },
        )

    def verify_at(base, version):
        scope = (
            "public"
            if base.endswith(":20128")
            else "a"
            if base.endswith(":21128")
            else "b"
        )
        events.append(f"verified:{scope}:{version}")
        return (
            "injected authentication failure" if (scope, version) in failures else None
        )

    def replace_route(runtime, slot):
        events.append(f"route:{slot}")
        assert runtime == hs.RUNTIME
        temporary = runtime / "route.tmp"
        temporary.symlink_to(f"{slot}.sock")
        os.replace(temporary, runtime / "active.sock")

    def bootout_worker(slot):
        assert live[slot]["mode"] == "stopped", (
            "Controller must receive stopped acknowledgement before bootout"
        )
        events.append(f"bootout:{slot}")
        sockets.pop(slot).close()
        (hs.RUNTIME / f"{slot}.sock").unlink()
        del live[slot]

    lifecycle = {
        name: getattr(hs, name)
        for name in (
            "start_worker",
            "bootout_worker",
            "replace_route",
            "job_present",
            "verify_job",
        )
    }
    monkeypatch.setattr(hs, "control", control)
    monkeypatch.setattr(hs, "start_worker", start_worker)
    monkeypatch.setattr(hs, "bootout_worker", bootout_worker)
    monkeypatch.setattr(hs, "job_present", lambda slot: slot in live)
    monkeypatch.setattr(
        hs, "verify_job", lambda slot, entry: hs.verify_configuration(slot, entry)
    )
    managed_probe = hs.verify_at
    monkeypatch.setattr(hs, "verify_at", verify_at)
    monkeypatch.setattr(hs, "replace_route", replace_route)
    # Fake transaction qualification; keep independent refusal checks below.
    monkeypatch.setattr(
        hs, "validate_release", lambda dest, digest: {"version": dest.parents[2].name}
    )
    monkeypatch.setattr(hs, "snapshot_db", lambda dest: events.append("snapshot"))
    monkeypatch.setattr(hs, "stage_assets", lambda dest: events.append("assets"))
    yield SimpleNamespace(
        mod=hs,
        events=events,
        releases=releases,
        live=live,
        failures=failures,
        lifecycle=lifecycle,
        managed_probe=managed_probe,
    )
    for listener in sockets.values():
        listener.close()


def test_shared_environment_enrollment_preserves_literal_values(hs):
    source = hs.HOME / "env.sh"
    source.write_text(
        f'export DATA_DIR="{hs.DB.parent.parent}"\n'
        'export JWT_SECRET="fixture-signing-value"\n'
        'export API_KEY_SECRET="fixture-api-signing-value"\n'
        'export MACHINE_ID_SALT="fixture-identity-value"\n'
        'export INITIAL_PASSWORD="fixture-login-value"\n'
    )
    source.chmod(0o600)
    env = hs.shared_environment(enrollment=True)
    assert env == hs.shared_environment(enrollment=True)
    assert env == hs.read_json(hs.STATE_DIR / "environment.json")
    assert env["JWT_SECRET"] == "fixture-signing-value"
    assert env["API_KEY_SECRET"] == "fixture-api-signing-value"
    assert env["MACHINE_ID_SALT"] == "fixture-identity-value"
    assert env["INITIAL_PASSWORD"] == "fixture-login-value"
    assert (hs.STATE_DIR / "environment.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "invalid",
    [
        'export JWT_SECRET="$(touch unsafe)"',
        'export JWT_SECRET="$UNKNOWN"',
        'export JWT_SECRET="duplicate"',
        'export DATA_DIR="/wrong/root"',
        'export NODE_OPTIONS="--require /foreign/code"',
        "source /foreign/env.sh",
        r'export JWT_SECRET="a\"b"',
        r'export INITIAL_PASSWORD="a\\b"',
        "export DATA_DIR='$HOME/.9router'",
    ],
)
def test_shared_environment_refuses_unproven_continuity(hs, invalid):
    source = hs.HOME / "env.sh"
    source.write_text(
        f'export DATA_DIR="{hs.DB.parent.parent}"\n'
        'export JWT_SECRET="fixture-signing-value"\n'
        'export INITIAL_PASSWORD="fixture-login-value"\n'
        f"{invalid}\n"
    )
    source.chmod(0o600)
    with pytest.raises(ValueError):
        hs.shared_environment(enrollment=True)
    assert not (hs.STATE_DIR / "environment.json").exists()
    assert not hs.STATE_FILE.exists()


def test_worker_data_root_resolves_to_controller_database(controller):
    c = controller
    config = c.mod.slot_configuration("a", c.mod.read_state()["slots"]["a"])
    assert Path(config["dataDir"]) / "db/data.sqlite" == c.mod.DB
    # Bind to the application's persistence path, not a second controller helper.
    paths = SCRIPT.parents[2] / "src/lib/db/paths.js"
    source = paths.read_text()
    assert 'export const DB_DIR = path.join(DATA_DIR, "db");' in source
    assert 'export const DATA_FILE = path.join(DB_DIR, "data.sqlite");' in source


def active(c):
    return os.readlink(c.mod.RUNTIME / "active.sock")[0]


def deploy(c, version="v2"):
    return c.mod.deploy_release(c.releases[version], "b" * 64)


def test_failed_candidate_never_changes_route(controller):
    c = controller
    c.failures.add(("b", "v2"))
    assert deploy(c) != 0
    assert active(c) == "a" and "a" in c.live
    assert not any(event.startswith("route:") for event in c.events)
    assert "bootout:a" not in c.events


def test_error_sse_candidate_keeps_old_route_and_worker(controller, monkeypatch):
    c = controller
    module = c.mod.deployer()
    attempts = []

    def http(path, body=None, timeout=30, *, base=module.BASE):
        attempts.append((path, base))
        if path == "/api/version":
            return io.BytesIO(b'{"currentVersion":"v2"}')
        if path == "/v1/models":
            return io.BytesIO(b'{"data":[{"id":"fixture"}]}')
        return io.BytesIO(b'data: {"error":{"message":"failed"}}\n\ndata: [DONE]\n\n')

    monkeypatch.setattr(module, "http", http)
    monkeypatch.setattr(c.mod, "deployer", lambda: module)
    original_probe = c.mod.verify_at
    monkeypatch.setattr(
        c.mod,
        "verify_at",
        lambda base, version: (
            c.managed_probe(base, version)
            if version == "v2"
            else original_probe(base, version)
        ),
    )
    assert deploy(c) != 0
    assert active(c) == "a" and "a" in c.live
    assert sum(path == "/v1/chat/completions" for path, _ in attempts) == 1
    assert not any(event.startswith(("route:", "bootout:")) for event in c.events)


def test_success_switches_only_after_verification(controller):
    c = controller
    assert deploy(c) == 0
    assert c.events.index("verified:b:v2") < c.events.index("route:b")
    assert c.events.index("route:b") < c.events.index("drain:a")
    assert active(c) == "b" and "a" in c.live
    assert c.mod.LINK.resolve() == c.releases["v2"]
    assert json.loads(c.mod.STATE_FILE.read_text())["slots"]["a"]["mode"] == "draining"
    assert "proxy-restart" not in c.events and "bootout:a" not in c.events


@pytest.mark.parametrize("boundary", ["snapshot", "assets"])
def test_prejournal_crash_preserves_backup_and_allows_new_version(
    controller, monkeypatch, boundary
):
    c = controller
    original_assets = c.mod.stage_assets

    def snapshot(path):
        path.write_bytes(b"retained backup")
        path.chmod(0o600)
        if boundary == "snapshot":
            raise SimulatedCrash("snapshot created before slot journal")

    def assets(path):
        original_assets(path)
        raise SimulatedCrash("assets published before slot journal")

    with monkeypatch.context() as patch:
        patch.setattr(c.mod, "snapshot_db", snapshot)
        if boundary == "assets":
            patch.setattr(c.mod, "stage_assets", assets)
        with pytest.raises(SimulatedCrash):
            deploy(c)
    backups = list(c.mod.STATE_DIR.glob("pre-deploy-*.sqlite"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"retained backup"
    assert c.mod.reconcile() == 0
    assert active(c) == "a" and set(c.live) == {"a"}
    assert c.mod.read_state()["pending"] is None
    assert deploy(c) != 0  # Never overwrite an unbound pre-journal backup.
    assert deploy(c, "v3") == 0
    assert backups[0].read_bytes() == b"retained backup"


def assert_deployment_commit_reconciled(c, before):
    state = c.mod.read_state()
    assert state["active"] == "b" and state["pending"] is None
    assert c.mod.LINK.resolve() == c.releases["v2"]
    assert c.live["a"]["mode"] == "draining" and set(c.live) == {"a", "b"}
    assert state["slots"]["a"]["release"] == before["slots"]["a"]["release"]
    assert not any(event.startswith(("stop:", "bootout:")) for event in c.events)


@pytest.mark.parametrize("boundary", ["public", "drain", "pointer", "log"])
def test_deployment_commit_crash_boundaries_reconcile(
    controller, monkeypatch, boundary
):
    c = controller
    before = c.mod.read_state()
    original_probe = c.mod.verify_at
    original_control = c.mod.control
    original_pointer = c.mod.convenience_pointer
    original_log = c.mod.journal_log

    def probe(base, version):
        result = original_probe(base, version)
        if (boundary, base, version) == ("public", "http://127.0.0.1:20128", "v2"):
            raise SimulatedCrash("public probe completed")
        return result

    def control(slot, op):
        result = original_control(slot, op)
        if (boundary, slot, op) == ("drain", "a", "drain"):
            raise SimulatedCrash("drain acknowledged")
        return result

    def pointer(entry):
        original_pointer(entry)
        if boundary == "pointer":
            raise SimulatedCrash("convenience pointer replaced")

    def log(state, action):
        original_log(state, action)
        if boundary == "log":
            raise SimulatedCrash("audit log published")

    with monkeypatch.context() as patch:
        patch.setattr(c.mod, "verify_at", probe)
        patch.setattr(c.mod, "control", control)
        patch.setattr(c.mod, "convenience_pointer", pointer)
        patch.setattr(c.mod, "journal_log", log)
        with pytest.raises(SimulatedCrash):
            deploy(c)
    assert active(c) == "b"
    assert c.mod.read_state()["pending"] is not None
    assert c.mod.reconcile() == 0
    assert_deployment_commit_reconciled(c, before)


@pytest.mark.parametrize("boundary", ["candidate-ready", "route-published"])
def test_worker_disappearance_preserves_route_without_blind_cleanup(
    controller, monkeypatch, boundary
):
    c = controller
    original_probe = c.mod.verify_at
    original_route = c.mod.replace_route

    def probe(base, version):
        result = original_probe(base, version)
        if boundary == "candidate-ready" and version == "v2":
            c.live.pop("b", None)
        return result

    def route(runtime, slot):
        original_route(runtime, slot)
        c.live.pop("b", None)
        raise SimulatedCrash("worker vanished after route publication")

    with monkeypatch.context() as patch:
        if boundary == "candidate-ready":
            patch.setattr(c.mod, "verify_at", probe)
            assert deploy(c) != 0
        else:
            patch.setattr(c.mod, "replace_route", route)
            with pytest.raises(SimulatedCrash):
                deploy(c)
    expected = "a" if boundary == "candidate-ready" else "b"
    assert active(c) == expected
    c.events.clear()
    assert c.mod.reconcile() != 0
    assert active(c) == expected and "a" in c.live
    assert not any(
        event.startswith(("route:", "stop:", "bootout:")) for event in c.events
    )


def test_busy_inactive_slot_refuses_next_deploy(controller):
    c = controller
    assert deploy(c) == 0
    c.events.clear()
    assert deploy(c, "v3") != 0
    assert active(c) == "b" and "a" in c.live
    assert "start:a" not in c.events and "bootout:a" not in c.events


@pytest.mark.parametrize("key", WORK_KEYS)
def test_positive_app_work_preserves_inactive_worker(controller, key):
    c = controller
    assert deploy(c) == 0
    c.live["a"]["connections"] = 0
    c.live["a"]["appWork"][key] = 1
    assert deploy(c, "v3") != 0
    assert "a" in c.live and "bootout:a" not in c.events


@pytest.mark.parametrize("response", [None, {}, {"slot": "a", "error": "unavailable"}])
def test_missing_or_malformed_status_refuses_mutation(controller, response):
    c = controller
    c.live["a"] = response
    assert deploy(c) != 0
    assert active(c) == "a"
    assert not any(
        event.startswith(("route:", "start:", "stop:", "bootout:"))
        for event in c.events
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("connections", True),
        ("connections", -1),
        ("connections", None),
        ("appWork", None),
        ("version", "foreign"),
        ("slot", "b"),
        ("appPid", None),
        ("mode", "failed"),
    ],
)
def test_unknown_worker_identity_preserves_both_workers(controller, field, value):
    c = controller
    c.live["a"][field] = value
    assert deploy(c) != 0
    assert active(c) == "a"
    assert not any(
        event.startswith(("route:", "start:", "stop:", "bootout:"))
        for event in c.events
    )


def test_controller_bootouts_only_after_worker_acknowledges_stopped(controller):
    c = controller
    assert deploy(c) == 0
    c.live["a"]["connections"] = 0
    c.events.clear()
    assert deploy(c, "v3") == 0
    assert (
        c.events.index("stop:a")
        < c.events.index("bootout:a")
        < c.events.index("start:a")
    )


def test_stop_refusal_preserves_slot_and_route(controller):
    c = controller
    assert deploy(c) == 0
    c.live["a"]["connections"] = 0
    c.failures.add(("a", "stop"))
    assert deploy(c, "v3") != 0
    assert active(c) == "b" and "a" in c.live
    assert "bootout:a" not in c.events


def test_public_failure_resumes_old_slot_before_switching_back(controller):
    c = controller
    c.failures.add(("public", "v2"))
    assert deploy(c) != 0
    rollback_events = c.events[c.events.index("verified:public:v2") + 1 :]
    assert (
        rollback_events.index("resume:a")
        < rollback_events.index("verified:a:v1")
        < rollback_events.index("route:a")
    )
    assert rollback_events.index("route:a") < rollback_events.index(
        "verified:public:v1"
    )
    assert active(c) == "a" and "b" in c.live
    assert "drain:b" in c.events and "bootout:b" not in c.events


def test_failed_rollback_keeps_last_serving_route(controller):
    c = controller
    c.failures.update({("public", "v2"), ("a", "resume")})
    assert deploy(c) != 0
    assert active(c) == "b" and set(c.live) == {"a", "b"}
    assert "route:a" not in c.events


def test_manual_rollback_reuses_draining_worker_without_restart(controller):
    c = controller
    assert deploy(c) == 0
    c.events.clear()
    assert c.mod.rollback_release("v1") == 0
    assert active(c) == "a" and set(c.live) == {"a", "b"}
    assert (
        c.events.index("resume:a")
        < c.events.index("route:a")
        < c.events.index("drain:b")
    )
    assert not any(event.startswith(("start:", "bootout:")) for event in c.events)


def test_flock_refuses_concurrent_deployment(controller):
    c = controller
    with c.mod.LOCK_FILE.open("a") as lock:
        c.mod.LOCK_FILE.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert deploy(c) != 0
    assert active(c) == "a" and c.events == []


def test_lock_covers_retirement_and_every_route_mutation(controller, monkeypatch):
    c = controller
    assert deploy(c) == 0
    c.live["a"]["connections"] = 0
    for name in ("control", "replace_route"):
        original = getattr(c.mod, name)

        def guarded(*args, original=original):
            with c.mod.LOCK_FILE.open("a") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(*args)

        monkeypatch.setattr(c.mod, name, guarded)
    assert deploy(c, "v3") == 0


def test_corrupted_journal_refuses_without_worker_mutations(controller):
    c = controller
    c.mod.STATE_FILE.write_text("{incomplete")
    assert c.mod.reconcile() != 0
    assert active(c) == "a" and c.events == []


def test_status_remains_diagnostic_when_worker_probe_fails(controller):
    c = controller
    c.failures.add(("a", "status"))
    result = c.mod.status()
    assert isinstance(result, dict)
    assert result.get("errors"), "Unknown status must not render as a healthy verdict"
    assert active(c) == "a"


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "checkpoint", ["prepared", "verified", "switched", "committed"]
)
def test_crash_recovery_uses_socket_route_authority(
    controller, monkeypatch, checkpoint
):
    c = controller
    original = c.mod.write_state

    def crash_after(state):
        original(state)
        phase = (
            (state.get("pending") or {}).get("phase")
            if state.get("pending")
            else "committed"
        )
        if phase == checkpoint:
            raise SimulatedCrash(checkpoint)

    monkeypatch.setattr(c.mod, "write_state", crash_after)
    with pytest.raises(SimulatedCrash):
        deploy(c)
    route = active(c)
    monkeypatch.setattr(c.mod, "write_state", original)
    assert c.mod.reconcile() == 0
    state = json.loads(c.mod.STATE_FILE.read_text())
    assert state["active"] == route == active(c)
    assert c.mod.LINK.resolve() == c.releases["v1" if route == "a" else "v2"]
    assert "bootout:a" not in c.events


def test_crash_between_route_rename_and_state_commit_recovers_new_route(
    controller, monkeypatch
):
    c = controller
    original = c.mod.replace_route

    def crash_after(runtime, slot):
        original(runtime, slot)
        raise SimulatedCrash("route replacement")

    monkeypatch.setattr(c.mod, "replace_route", crash_after)
    with pytest.raises(SimulatedCrash):
        deploy(c)
    monkeypatch.setattr(c.mod, "replace_route", original)
    assert active(c) == "b"
    assert c.mod.reconcile() == 0
    assert json.loads(c.mod.STATE_FILE.read_text())["active"] == "b"
    assert "drain:a" in c.events and "bootout:a" not in c.events


def test_recovery_with_unknown_new_identity_preserves_workers(controller, monkeypatch):
    c = controller
    original = c.mod.replace_route

    def crash_after(runtime, slot):
        original(runtime, slot)
        raise SimulatedCrash("route replacement")

    monkeypatch.setattr(c.mod, "replace_route", crash_after)
    with pytest.raises(SimulatedCrash):
        deploy(c)
    monkeypatch.setattr(c.mod, "replace_route", original)
    c.live["b"]["version"] = "foreign"
    c.events.clear()
    assert c.mod.reconcile() != 0
    assert active(c) == "b" and set(c.live) == {"a", "b"}
    assert not any(
        event.startswith(("route:", "stop:", "bootout:", "drain:"))
        for event in c.events
    )


@pytest.mark.parametrize("boundary", ["replace_route", "write_state"])
def test_route_or_state_io_failure_never_bootouts_healthy_worker(
    controller, monkeypatch, boundary
):
    c = controller

    def fail(*args):
        raise OSError("injected rename or fsync failure")

    monkeypatch.setattr(c.mod, boundary, fail)
    assert deploy(c) != 0
    assert "a" in c.live and "bootout:a" not in c.events


def test_legacy_qualification_receipt_cannot_enroll_managed_release(hs):
    digest = "b" * 64
    private_json(
        hs.QUALIFIED / f"{digest}.json", {"result": "pass", "deploy_sha256": "c" * 64}
    )
    with pytest.raises(ValueError, match="managed qualification"):
        hs.qualification(digest)


def test_explicit_runtime_binding_does_not_fall_back(hs, monkeypatch, tmp_path):
    executable = tmp_path / "pinned-node"
    executable.write_bytes(b"fixture executable bytes")
    executable.chmod(0o700)
    monkeypatch.setenv("NINEROUTER_NODE_BIN", str(executable))
    monkeypatch.setattr(hs.shutil, "which", lambda name: "/wrong/global/node")
    assert hs.executable("node") == str(executable)
    executable.unlink()
    with pytest.raises(ValueError, match="missing runtime executable"):
        hs.executable("node")


def test_runtime_binding_mismatch_refuses_before_native_checks(hs, monkeypatch):
    digest = "b" * 64
    private_json(
        hs.QUALIFIED / f"{digest}.json",
        {
            "protocol": 1,
            "result": "pass",
            "sha256": digest,
            "runtime_sha256": {"worker": "old"},
        },
    )
    monkeypatch.setattr(hs, "runtime_hashes", lambda: {"worker": "new"})
    with pytest.raises(ValueError, match="runtime binding"):
        hs.qualification(digest)


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), True])
def test_qualification_refuses_invalid_shutdown_timestamp(hs, monkeypatch, timestamp):
    digest = "b" * 64
    private_json(
        hs.QUALIFIED / f"{digest}.json",
        {
            "protocol": 1,
            "result": "pass",
            "sha256": digest,
            "runtime_sha256": {},
            "proxy_version": "fixture",
            "source_commit": "fixture",
            "guest_versions": {"python": "fixture"},
            "namespace": {"devbox_id": "fixture", "instance_id": "fixture"},
            "cleanup": {
                "verified": True,
                "observations": [
                    {"state": "stopped", "instance_id": None, "at": timestamp},
                    {"state": "stopped", "instance_id": None, "at": 120},
                ],
            },
        },
    )
    monkeypatch.setattr(hs, "runtime_hashes", lambda: {})
    monkeypatch.setattr(hs, "executable", lambda name: name)
    monkeypatch.setattr(
        hs.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="fixture"),
    )
    with pytest.raises(ValueError, match="shutdown evidence"):
        hs.qualification(digest)


def test_release_validation_does_not_require_snapshot_space(hs, monkeypatch):
    monkeypatch.setattr(hs, "validate_package", lambda *args: {"version": "v2"})
    monkeypatch.setattr(hs, "node_managed", lambda *args: None)
    monkeypatch.setattr(
        hs.shutil,
        "disk_usage",
        lambda *args: pytest.fail("Rollback/recovery must not demand snapshot space"),
    )
    release = hs.RELEASES / "v2/lib/node_modules/9router"
    assert hs.validate_release(release, "b" * 64) == {"version": "v2"}


def test_snapshot_refuses_low_space_before_creating_file(hs, monkeypatch):
    hs.DB.write_bytes(b"database")
    monkeypatch.setattr(hs.shutil, "disk_usage", lambda *args: SimpleNamespace(free=0))
    target = hs.STATE_DIR / "new.sqlite"
    with pytest.raises(ValueError, match="insufficient snapshot"):
        hs.snapshot_db(target)
    assert not target.exists()


def test_snapshot_sqlite_failure_is_a_controlled_refusal(hs, monkeypatch):
    hs.DB.write_bytes(b"database")
    monkeypatch.setattr(
        hs.shutil, "disk_usage", lambda *args: SimpleNamespace(free=10**9)
    )

    def fail_snapshot(_destination):
        raise hs.sqlite3.OperationalError("fixture unreadable source")

    monkeypatch.setattr(
        hs, "deployer", lambda: SimpleNamespace(snapshot_db=fail_snapshot)
    )
    with pytest.raises(ValueError, match="database snapshot refused"):
        hs.snapshot_db(hs.STATE_DIR / "failed.sqlite")
    assert hs.DB.read_bytes() == b"database"


def test_route_slot_refuses_dangling_and_nonsocket_targets(hs):
    route = hs.RUNTIME / "active.sock"
    route.symlink_to("a.sock")
    with pytest.raises(FileNotFoundError):
        hs.route_slot()
    (hs.RUNTIME / "a.sock").write_bytes(b"not a socket")
    with pytest.raises(ValueError, match="serving bridge identity"):
        hs.route_slot()


def test_real_validation_requires_receipt_even_in_guest(hs, tmp_path, monkeypatch):
    release = hs.RELEASES / "v2/lib/node_modules/9router"
    release.mkdir(parents=True)
    private_json(release / "package.json", {"version": "v2"})
    monkeypatch.setattr(hs, "concrete_release", lambda destination: "v2")
    with pytest.raises(FileNotFoundError) as missing:
        hs.deploy_locked(release, "b" * 64, {})
    assert Path(missing.value.filename) == hs.QUALIFIED / f"{'b' * 64}.json"
    assert not hs.STATE_FILE.exists()


@pytest.mark.parametrize(
    "version", ["../escape", "/absolute", "v2/child", "", ".", ".."]
)
def test_invalid_versions_refuse_before_side_effects(hs, version):
    assert hs.rollback_release(version) != 0
    assert not hs.STATE_FILE.exists()


def test_successful_restore_preserves_the_actual_provider_database(
    controller, monkeypatch
):
    c = controller
    c.mod.DB.write_bytes(b"original-provider-credentials")
    state = c.mod.read_state()
    state["enrollment"] = "complete"
    c.mod.write_state(state)
    monkeypatch.setattr(c.mod, "QUALIFICATION_SCOPE", {"phase": "qualification"})
    monkeypatch.setattr(
        c.mod, "concrete_release", lambda destination: destination.parents[2].name
    )
    monkeypatch.setattr(c.mod, "restore_qualification_runtime", lambda: None)
    monkeypatch.setattr(c.mod, "proxy_enrollment", lambda: None)
    monkeypatch.setattr(c.mod, "require_probe", lambda *args: None)
    assert c.mod.restore_qualification() == 0
    assert c.mod.DB.read_bytes() == b"original-provider-credentials"


def test_managed_install_uses_exact_private_copy_without_package_hooks(
    controller, monkeypatch
):
    c = controller
    tgz = c.mod.HOME / "input.tgz"
    tgz.write_bytes(b"qualified bytes")
    c.mod.DB.write_bytes(b"fixture database")
    digest = c.mod.sha256(tgz)
    monkeypatch.setattr(
        c.mod,
        "qualification",
        lambda value: {} if value == digest else pytest.fail("Digest changed"),
    )
    monkeypatch.setattr(
        c.mod,
        "deployer",
        lambda: SimpleNamespace(
            tgz_version=lambda path: "v4",
            release_dir=lambda version: (
                c.mod.RELEASES / version / "lib/node_modules/9router"
            ),
        ),
    )
    calls = []

    def install(argv, **kwargs):
        assert argv[:4] == ["npm", "install", "-g", "--ignore-scripts"]
        installed = Path(argv[-1])
        assert installed != tgz
        assert installed.read_bytes() == b"qualified bytes"
        assert c.mod.sha256(installed) == digest
        calls.append(argv)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(c.mod.subprocess, "run", install)
    assert c.mod.deploy_tarball(tgz) != 0
    assert len(calls) == 1
    assert active(c) == "a" and "bootout:a" not in c.events


def test_malformed_stop_acknowledgement_never_grants_bootout(controller, monkeypatch):
    c = controller
    assert deploy(c) == 0
    c.live["a"]["connections"] = 0
    original = c.mod.control

    def control(slot, op):
        response = original(slot, op)
        if op == "stop":
            response["connections"] = False
        return response

    monkeypatch.setattr(c.mod, "control", control)
    assert deploy(c, "v3") != 0
    assert "bootout:a" not in c.events
    assert active(c) == "b"


def test_enrollment_rejects_package_before_executing_its_helper(hs, monkeypatch):
    calls = []

    def validate_package(*args):
        calls.append("package")
        raise ValueError("installed package differs from qualified bytes")

    monkeypatch.setattr(hs, "validate_package", validate_package)
    monkeypatch.setattr(
        hs, "node_managed", lambda *args: pytest.fail("Unqualified helper executed")
    )
    assert (
        hs.main(
            [
                "enroll",
                "--acknowledge-maintenance",
                "--release",
                str(hs.RELEASES / "v2"),
                "--digest",
                "b" * 64,
            ]
        )
        != 0
    )
    assert calls == ["package"]
    assert not hs.STATE_FILE.exists()


def test_managed_probe_does_not_retry_uncertain_completion(hs, monkeypatch):
    module = SimpleNamespace(PROBE_ATTEMPTS=3)
    seen = []
    module.verify = lambda version, *, base: seen.append(
        (version, base, module.PROBE_ATTEMPTS)
    )
    monkeypatch.setattr(hs, "deployer", lambda: module)
    hs.verify_at("http://127.0.0.1:21130", "v2")
    assert seen == [("v2", "http://127.0.0.1:21130", 1)]


def test_recovery_repairs_convenience_pointer_without_route_change(controller):
    c = controller
    c.mod.LINK.unlink()
    c.mod.LINK.symlink_to(c.releases["v2"])
    assert c.mod.reconcile() == 0
    assert c.mod.LINK.resolve() == c.releases["v1"]
    assert active(c) == "a"
    assert not any(
        event.startswith(("route:", "start:", "bootout:")) for event in c.events
    )


def test_recovery_resumes_authoritative_draining_worker(controller):
    c = controller
    assert deploy(c) == 0
    c.live["b"]["mode"] = "draining"
    c.events.clear()
    assert c.mod.reconcile() == 0
    assert "resume:b" in c.events
    assert c.live["b"]["mode"] == "ready"
    assert active(c) == "b"
    assert not any(
        event.startswith(("start:", "bootout:", "route:")) for event in c.events
    )


def test_candidate_stopped_after_probe_never_receives_public_route(
    controller, monkeypatch
):
    c = controller
    original = c.mod.verify_at

    def verify_at(base, version):
        result = original(base, version)
        if base.endswith(":21130"):
            c.live["b"]["mode"] = "stopped"
        return result

    monkeypatch.setattr(c.mod, "verify_at", verify_at)
    assert deploy(c) != 0
    assert active(c) == "a"
    assert "route:b" not in c.events


def test_asset_copy_failure_never_publishes_partial_bytes(hs, monkeypatch):
    release = hs.RELEASES / "v2/lib/node_modules/9router"
    source = release / "app/.next-cli-build/static"
    source.mkdir(parents=True)
    (source / "chunk.js").write_bytes(b"complete bytes")

    def fail(incoming, outgoing):
        outgoing.write(b"partial")
        raise OSError("injected copy failure")

    with monkeypatch.context() as patch:
        patch.setattr(hs.shutil, "copyfileobj", fail)
        with pytest.raises(OSError):
            hs.stage_assets(release)
    assert not (hs.STATE_DIR / "assets/chunk.js").exists()
    hs.stage_assets(release)
    assert (hs.STATE_DIR / "assets/chunk.js").read_bytes() == b"complete bytes"


def test_asset_seed_is_confined_nonempty_and_conflict_checked(hs, tmp_path):
    source = tmp_path / "legacy-assets"
    source.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="empty"):
        hs.stage_asset_directory(source)
    (source / "old.js").write_bytes(b"old dashboard chunk")
    hs.stage_asset_directory(source)
    target = hs.STATE_DIR / "assets" / "old.js"
    assert target.read_bytes() == b"old dashboard chunk"
    (source / "old.js").write_bytes(b"conflicting immutable bytes")
    with pytest.raises(ValueError, match="conflict"):
        hs.stage_asset_directory(source)
    assert target.read_bytes() == b"old dashboard chunk"
    (source / "old.js").unlink()
    (source / "credential.sqlite").write_bytes(b"not an immutable asset")
    with pytest.raises(ValueError, match="unsupported"):
        hs.stage_asset_directory(source)
    (source / "credential.sqlite").unlink()
    (source / "outside.js").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        hs.stage_asset_directory(source)
    link = tmp_path / "linked-assets"
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="concrete"):
        hs.stage_asset_directory(link)
    with pytest.raises(ValueError, match="concrete"):
        hs.stage_asset_directory(source / ".." / "legacy-assets")
    (source / "outside.js").unlink()
    (source / "old.js").write_bytes(b"old dashboard chunk")
    with pytest.raises(ValueError, match="overlap"):
        hs.stage_asset_directory(source, target=source)
    with pytest.raises(ValueError, match="concrete"):
        hs.stage_asset_directory(source, target=link)


def test_enrollment_requires_explicit_maintenance_acknowledgement(hs):
    assert hs.main(["enroll"]) != 0
    assert not hs.STATE_FILE.exists()


def test_enrollment_acknowledgement_is_not_a_qualification_bypass(hs):
    assert hs.main(["enroll", "--acknowledge-maintenance"]) != 0
    assert not hs.STATE_FILE.exists()


def test_rollback_does_not_require_broken_candidate_readiness(controller):
    c = controller
    assert deploy(c) == 0
    c.failures.update({("b", "v2"), ("public", "v2")})
    c.events.clear()
    assert c.mod.rollback_release("v1") == 0
    assert active(c) == "a" and set(c.live) == {"a", "b"}
    assert "verified:b:v2" not in c.events
    assert "drain:b" in c.events and "bootout:b" not in c.events


def test_rollback_with_unknown_candidate_identity_preserves_serving_route(controller):
    c = controller
    assert deploy(c) == 0
    c.live["b"]["version"] = "foreign"
    c.events.clear()
    assert c.mod.rollback_release("v1") != 0
    assert active(c) == "b" and set(c.live) == {"a", "b"}
    assert not any(
        event.startswith(("resume:", "route:", "drain:", "bootout:"))
        for event in c.events
    )


def test_failed_private_candidate_can_reconcile_without_becoming_public(controller):
    c = controller
    c.failures.add(("b", "v2"))
    assert deploy(c) != 0
    assert c.mod.reconcile() == 0
    assert active(c) == "a" and c.live["b"]["mode"] == "draining"
    assert "bootout:b" not in c.events


def prepare_fixture_jobs(c, jobs):
    state = c.mod.read_state()
    for slot, entry in state["slots"].items():
        if entry:
            path = c.mod.STATE_DIR / f"{slot}.plist"
            plist = {
                "Label": f"com.lfenergy.9router-worker-{slot}",
                "ProgramArguments": [
                    "/fixture/node",
                    str(c.mod.HERE / "9router_worker.cjs"),
                    str(c.mod.STATE_DIR / f"{slot}.json"),
                ],
                "EnvironmentVariables": c.mod.shared_environment(),
            }
            path.write_bytes(plistlib.dumps(plist))
            path.chmod(0o600)
            entry["job_sha256"] = c.mod.sha256(path)
            jobs[slot] = plist
    c.mod.write_state(state)


def fixture_bootstrap(c, jobs, bridge, slot, path):
    if "bootstrap" in c.os_failures:
        return SimpleNamespace(returncode=1)
    assert slot not in jobs
    config = c.mod.read_json(c.mod.STATE_DIR / f"{slot}.json")
    jobs[slot] = plistlib.loads(Path(path).read_bytes())
    bridge(slot)
    c.live[slot] = {
        "slot": slot,
        "version": config["version"],
        "mode": "ready",
        "connections": 0,
        "appPid": 456,
        "appWork": {
            "initialized": True,
            "unknown": False,
            **dict.fromkeys(WORK_KEYS, 0),
        },
    }
    c.events.append(f"start:{slot}")
    return SimpleNamespace(
        returncode=1 if "bootstrap-failed-loaded" in c.os_failures else 0
    )


def fixture_bootout(c, jobs, slot):
    assert c.live[slot]["mode"] == "stopped"
    if "bootout-refused" in c.os_failures:
        return SimpleNamespace(returncode=1)
    del jobs[slot]
    del c.live[slot]
    c.events.append(f"bootout:{slot}")
    if "after-bootout" in c.os_failures:
        raise SimulatedCrash("after bootout")
    return SimpleNamespace(returncode=0)


@pytest.mark.parametrize("status", ["(pe)", "(jt)", "-", "0", "-9", "27", "opaque"])
@pytest.mark.parametrize("pid, spacing", [("-", " "), ("0", "\t"), ("123", "  \t ")])
def test_job_present_reads_complete_population(hs, monkeypatch, status, pid, spacing):
    domain = f"gui/{os.getuid()}"
    label = hs.job_label("a")
    rows = (
        f"\t\t{pid}{spacing}{status}{spacing}{label}\n"
        f"\t\t-\t(pe)\t{label}.other\n"
        "\t\t0  (jt)  unrelated.service\n"
    )

    def run(argv, **kwargs):
        assert argv == ["launchctl", "print", domain]
        return SimpleNamespace(
            returncode=0, stdout=f"{domain} = {{\n\tservices = {{\n{rows}\t}}\n}}\n"
        )

    monkeypatch.setattr(hs.subprocess, "run", run)
    assert hs.job_present("a") is True
    assert hs.job_present("b") is False


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-domain",
        "unreadable",
        "duplicate-label",
        "duplicate-block",
        "missing-field",
        "extra-field",
        "bad-pid",
        "bad-label",
        "truncated-row",
        "truncated-block",
        "truncated-domain",
        "bad-block-end",
    ],
)
def test_job_present_refuses_unknown_population(hs, monkeypatch, fault):
    domain = f"gui/{os.getuid()}"
    row = f"\t\t0 (pe) {hs.job_label('a')}\n"
    block = f"\tservices = {{\n{row}\t}}\n"
    text = f"{domain} = {{\n{block}}}\n"
    variants = {
        "wrong-domain": text.replace(domain, "system", 1),
        "unreadable": text,
        "duplicate-label": text.replace(row, row + row),
        "duplicate-block": text.replace(block, block + block),
        "missing-field": text.replace(row, "\t\t0 label\n"),
        "extra-field": text.replace(row, "\t\t0 (pe) label extra\n"),
        "bad-pid": text.replace(row, "\t\t-1 (pe) label\n"),
        "bad-label": text.replace(row, "\t\t0 (pe) {\n"),
        "truncated-row": text.replace(row, "\t\t0 (pe)\n"),
        "truncated-block": f"{domain} = {{\n\tservices = {{\n{row}",
        "truncated-domain": text[:-2],
        "bad-block-end": text.replace("\t}\n", "\t} junk\n"),
    }

    def run(argv, **kwargs):
        assert argv == ["launchctl", "print", domain]
        return SimpleNamespace(
            returncode=int(fault == "unreadable"), stdout=variants[fault]
        )

    monkeypatch.setattr(hs.subprocess, "run", run)
    for slot in ("a", "b"):
        with pytest.raises(ValueError, match="launchd job population"):
            hs.job_present(slot)


def test_job_present_does_not_turn_failed_lookup_into_absence(hs, monkeypatch):
    def run(argv, **kwargs):
        assert argv == ["launchctl", "print", f"gui/{os.getuid()}"]
        raise OSError("launchctl unavailable")

    monkeypatch.setattr(hs.subprocess, "run", run)
    with pytest.raises(OSError, match="launchctl unavailable"):
        hs.job_present("b")


def fixture_launchctl(c, jobs, bridge, argv):
    assert argv[0] == "launchctl", "Only the fake launchd boundary may execute"
    domain = f"gui/{os.getuid()}"
    if argv == ["launchctl", "print", domain]:
        if "metadata" in c.os_failures:
            return SimpleNamespace(returncode=1, stdout="")
        rows = "".join(
            f"\t\t{c.live.get(slot, {}).get('appPid', '-')} 0 "
            f"com.lfenergy.9router-worker-{slot}\n"
            for slot in jobs
        )
        return SimpleNamespace(
            returncode=0,
            stdout=f"{domain} = {{\n\tservices = {{\n{rows}\t}}\n}}\n",
        )
    slot = argv[-1][-1] if argv[1] in {"print", "bootout"} else Path(argv[-1]).stem
    if argv[1] == "print":
        plist = jobs[slot]
        arguments = "\n".join(f"\t\t{value}" for value in plist["ProgramArguments"])
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"job = {{\n\tpath = {c.mod.STATE_DIR / f'{slot}.plist'}\n"
                f"\tprogram = {plist['ProgramArguments'][0]}\n"
                f"\targuments = {{\n{arguments}\n\t}}\n}}\n"
            ),
        )
    if argv[1] == "bootstrap":
        return fixture_bootstrap(c, jobs, bridge, slot, argv[-1])
    assert argv[1] == "bootout"
    return fixture_bootout(c, jobs, slot)


@pytest.fixture
def lifecycle_controller(controller, monkeypatch):
    """Keep lifecycle/config/journal code real; fake only OS and worker boundaries."""
    c = controller
    for name, implementation in c.lifecycle.items():
        monkeypatch.setattr(c.mod, name, implementation)
    # Short Unix socket paths; this directory exists only in the guest.
    with tempfile.TemporaryDirectory(prefix="9r-t4-") as directory:
        runtime = Path(directory)
        runtime.chmod(0o700)
        monkeypatch.setattr(c.mod, "RUNTIME", runtime)
        runtime.joinpath("active.sock").symlink_to("a.sock")
        private_json(
            c.mod.STATE_DIR / "a.json",
            c.mod.slot_configuration("a", c.mod.read_state()["slots"]["a"]),
        )
        sockets = []
        jobs = {}
        c.os_failures = set()

        def bridge(slot):
            endpoint = socket.socket(socket.AF_UNIX)
            endpoint.bind(str(runtime / f"{slot}.sock"))
            sockets.append(endpoint)

        bridge("a")
        monkeypatch.setattr(c.mod, "executable", lambda name: f"/fixture/{name}")
        prepare_fixture_jobs(c, jobs)

        def run(argv, **kwargs):
            return fixture_launchctl(c, jobs, bridge, argv)

        monkeypatch.setattr(c.mod.subprocess, "run", run)
        # Port reservation is an OS boundary, not the lifecycle under review.
        native_socket = socket.socket

        class Reservation:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def bind(self, address):
                assert address[0] == "127.0.0.1"

        monkeypatch.setattr(
            c.mod.socket,
            "socket",
            lambda *args, **kwargs: (
                native_socket(*args, **kwargs) if args or kwargs else Reservation()
            ),
        )
        c.jobs = jobs
        try:
            yield c
        finally:
            for endpoint in sockets:
                endpoint.close()


def test_occupied_port_refuses_real_start_worker(lifecycle_controller, monkeypatch):
    c = lifecycle_controller
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setitem(c.mod.PORTS, "b", occupied.getsockname()[1])
        monkeypatch.setattr(c.mod.socket, "socket", type(occupied))
        assert deploy(c) != 0
    assert active(c) == "a" and "a" in c.live
    assert "b" not in c.jobs
    assert c.mod.reconcile() == 0
    assert c.mod.read_state()["slots"]["b"] is None


@pytest.mark.parametrize(
    "boundary",
    [
        "config-published",
        "config-fsync",
        "bootstrap-failed",
        "bootstrap-failed-loaded",
        "bootstrap-loaded",
        "foreign-job",
        "loaded-without-sockets",
        "metadata-unreadable",
    ],
)
def test_startup_boundaries_preserve_route_and_recover_only_owned_candidates(
    lifecycle_controller, monkeypatch, boundary
):
    c = lifecycle_controller
    original_json = c.mod.atomic_json
    original_sync = c.mod.sync_directory
    original_checkpoint = c.mod.lifecycle_checkpoint

    def atomic_json(path, value):
        original_json(path, value)
        if path == c.mod.STATE_DIR / "b.json" and boundary == "config-published":
            raise SimulatedCrash("config published")

    def sync_directory(path):
        if (
            boundary == "config-fsync"
            and path == c.mod.STATE_DIR
            and (path / "b.json").exists()
        ):
            raise OSError("post-config-rename directory fsync")
        original_sync(path)

    def checkpoint(slot, entry, phase):
        startup_checkpoint_boundary(boundary, phase)
        original_checkpoint(slot, entry, phase)

    with monkeypatch.context() as patch:
        patch.setattr(c.mod, "atomic_json", atomic_json)
        patch.setattr(c.mod, "sync_directory", sync_directory)
        patch.setattr(c.mod, "lifecycle_checkpoint", checkpoint)
        if boundary in {"bootstrap-failed", "bootstrap-failed-loaded"}:
            c.os_failures.add(
                "bootstrap" if boundary == "bootstrap-failed" else boundary
            )
            assert deploy(c) != 0
        elif boundary in {
            "foreign-job",
            "loaded-without-sockets",
            "metadata-unreadable",
        }:
            c.os_failures.add("bootstrap")
            assert deploy(c) != 0
        elif boundary == "config-fsync":
            assert deploy(c) != 0
        else:
            with pytest.raises(SimulatedCrash):
                deploy(c)
    assert_startup_recovery(c, boundary)


def startup_checkpoint_boundary(boundary, phase):
    if boundary == "bootstrap-loaded" and phase == "started":
        raise SimulatedCrash("bootstrap loaded before checkpoint")


def assert_startup_recovery(c, boundary):
    c.os_failures.clear()
    assert active(c) == "a"
    if boundary == "foreign-job":
        c.jobs["b"] = {
            "Label": "com.lfenergy.9router-worker-b",
            "ProgramArguments": ["/foreign/node"],
        }
    if boundary == "loaded-without-sockets":
        c.jobs["b"] = plistlib.loads((c.mod.STATE_DIR / "b.plist").read_bytes())
    if boundary == "metadata-unreadable":
        c.os_failures.add("metadata")
    if boundary in {"foreign-job", "loaded-without-sockets", "metadata-unreadable"}:
        assert c.mod.reconcile() != 0
        assert c.mod.read_state()["slots"]["b"] is not None
    else:
        assert c.mod.reconcile() == 0
        if boundary in {"bootstrap-loaded", "bootstrap-failed-loaded"}:
            assert c.live["b"]["mode"] == "draining"
        else:
            assert c.mod.read_state()["slots"]["b"] is None
            assert not (c.mod.STATE_DIR / "b.json").exists()
    assert active(c) == "a" and "a" in c.live
    assert "bootout:a" not in c.events


def retirement_stop_boundary(boundary, slot, op):
    if slot == "a" and op == "stop" and boundary == "stop-acknowledged":
        raise SimulatedCrash("stop acknowledged before checkpoint")


def retirement_journal_boundary(boundary, state):
    entry = state["slots"]["a"]
    phase = entry.get("lifecycle") if entry else None
    if (boundary, phase) in {
        ("stopping-checkpoint", "stopping"),
        ("stopped-checkpoint", "stopped"),
        ("cleanup-checkpoint", "cleanup"),
    }:
        raise SimulatedCrash(boundary)


def retirement_unlink_boundary(c, boundary, path):
    if (boundary == "config-unlinked" and path == c.mod.STATE_DIR / "a.json") or (
        boundary == "socket-unlinked" and path == c.mod.RUNTIME / "a.sock"
    ):
        raise SimulatedCrash("owned path unlinked")


def retirement_fsync_boundary(c, boundary, path):
    if path == c.mod.STATE_DIR and boundary == "stopped-fsync":
        entry = json.loads(c.mod.STATE_FILE.read_text())["slots"]["a"]
        if entry and entry.get("lifecycle") == "stopped":
            raise OSError("stopped checkpoint renamed before directory fsync")
    if (
        path == c.mod.STATE_DIR
        and boundary in {"cleanup-fsync", "deallocation-fsync"}
        and not (path / "a.json").exists()
    ):
        entry = json.loads(c.mod.STATE_FILE.read_text())["slots"]["a"]
        if (boundary == "cleanup-fsync" and entry is not None) or (
            boundary == "deallocation-fsync" and entry is None
        ):
            raise OSError("post-cleanup directory fsync")


@pytest.mark.parametrize(
    "boundary",
    [
        "stopping-checkpoint",
        "stop-acknowledged",
        "stopped-checkpoint",
        "stopped-fsync",
        "bootout",
        "bootout-refused",
        "cleanup-checkpoint",
        "socket-unlinked",
        "config-unlinked",
        "cleanup-fsync",
        "deallocation-fsync",
    ],
)
def test_retirement_boundaries_recover_without_touching_serving_worker(
    lifecycle_controller, monkeypatch, boundary
):
    c = lifecycle_controller
    assert deploy(c) == 0
    c.live["a"]["connections"] = 0
    original_control = c.mod.control
    original_write = c.mod.write_state
    original_unlink = Path.unlink
    original_sync = c.mod.sync_directory

    def control(slot, op):
        response = original_control(slot, op)
        retirement_stop_boundary(boundary, slot, op)
        return response

    def write_state(state):
        original_write(state)
        retirement_journal_boundary(boundary, state)

    def unlink(path, *args, **kwargs):
        original_unlink(path, *args, **kwargs)
        retirement_unlink_boundary(c, boundary, path)

    def sync_directory(path):
        retirement_fsync_boundary(c, boundary, path)
        original_sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(c.mod, "control", control)
        patch.setattr(c.mod, "write_state", write_state)
        patch.setattr(Path, "unlink", unlink)
        patch.setattr(c.mod, "sync_directory", sync_directory)
        if boundary == "bootout":
            c.os_failures.add("after-bootout")
        if boundary == "bootout-refused":
            c.os_failures.add("bootout-refused")
        if boundary.endswith("fsync") or boundary == "bootout-refused":
            assert deploy(c, "v3") != 0
        else:
            with pytest.raises(SimulatedCrash):
                deploy(c, "v3")
    c.os_failures.clear()
    assert c.mod.reconcile() == 0
    assert active(c) == "b" and "b" in c.live
    assert c.mod.read_state()["slots"]["a"] is None
    assert not (c.mod.STATE_DIR / "a.json").exists()
    assert "bootout:b" not in c.events


def test_launch_config_preserves_shared_secrets_across_swap_and_rollback(
    lifecycle_controller,
):
    c = lifecycle_controller
    shared = c.mod.shared_environment()
    assert deploy(c) == 0
    candidate = c.jobs["b"]
    assert candidate["KeepAlive"] is True
    assert candidate["ThrottleInterval"] == 5
    assert candidate["Label"] == c.mod.job_label("b")
    assert candidate["StandardOutPath"] != candidate["StandardErrorPath"]
    assert candidate["EnvironmentVariables"][
        "NINEROUTER_HOTSWAP_ENROLLED_MANIFEST"
    ] == str(c.mod.STATE_DIR / "manifest.json")
    assert candidate["EnvironmentVariables"]["NINEROUTER_HOTSWAP_REFRESH_DB"] == str(
        c.mod.STATE_DIR / "refresh.sqlite"
    )
    assert all(
        candidate["EnvironmentVariables"][key] == value for key, value in shared.items()
    )
    assert not any(
        shared[key] in argument
        for key in ("JWT_SECRET", "INITIAL_PASSWORD")
        for argument in candidate["ProgramArguments"]
    )
    assert c.mod.rollback_release("v1") == 0
    assert (
        c.jobs["a"]["EnvironmentVariables"]["JWT_SECRET"]
        == c.jobs["b"]["EnvironmentVariables"]["JWT_SECRET"]
        == shared["JWT_SECRET"]
    )
    assert c.mod.shared_environment() == shared


def test_changed_shared_environment_refuses_candidate_start(lifecycle_controller):
    c = lifecycle_controller
    env = c.mod.read_json(c.mod.STATE_DIR / "environment.json")
    env["JWT_SECRET"] = "changed-fixture-value"
    private_json(c.mod.STATE_DIR / "environment.json", env)
    assert deploy(c) != 0
    assert active(c) == "a" and "b" not in c.jobs
    assert "bootout:a" not in c.events


def test_retirement_retry_refreshes_reopened_bridge_identity(
    lifecycle_controller, monkeypatch
):
    c = lifecycle_controller
    assert deploy(c) == 0
    c.live["a"]["connections"] = 0
    original_control = c.mod.control
    first = True
    with socket.socket(socket.AF_UNIX) as reopened:

        def control(slot, op):
            nonlocal first
            if slot == "a" and op == "stop" and first:
                first = False
                path = c.mod.RUNTIME / "a.sock"
                path.unlink()
                reopened.bind(str(path))
                raise ValueError("fixture stop refused after bridge reopened")
            return original_control(slot, op)

        monkeypatch.setattr(c.mod, "control", control)
        assert deploy(c, "v3") != 0
        assert active(c) == "b" and "a" in c.jobs
        assert c.mod.reconcile() == 0
        assert c.mod.read_state()["slots"]["a"] is None
        assert "a" not in c.jobs and active(c) == "b"


def test_retirement_recovery_refuses_replaced_socket(lifecycle_controller):
    c = lifecycle_controller
    assert deploy(c) == 0
    c.live["a"]["connections"] = 0
    c.os_failures.add("after-bootout")
    with pytest.raises(SimulatedCrash):
        deploy(c, "v3")
    c.os_failures.clear()
    path = c.mod.RUNTIME / "a.sock"
    path.unlink()
    with socket.socket(socket.AF_UNIX) as foreign:
        foreign.bind(str(path))
        assert c.mod.reconcile() != 0
        assert path.exists()
        assert c.mod.read_state()["slots"]["a"] is not None
    assert active(c) == "b" and "b" in c.live


@pytest.mark.parametrize("boundary", ["route", "journal"])
def test_post_rename_directory_fsync_failure_recovers_published_bytes(
    lifecycle_controller, monkeypatch, boundary
):
    c = lifecycle_controller
    original_sync = c.mod.sync_directory

    def sync_directory(path):
        if boundary == "route" and path == c.mod.RUNTIME and active(c) == "b":
            raise OSError("route renamed before directory fsync")
        if boundary == "journal" and path == c.mod.STATE_DIR:
            state = json.loads(c.mod.STATE_FILE.read_text())
            if (state.get("pending") or {}).get("phase") == "switched":
                raise OSError("journal renamed before directory fsync")
        original_sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(c.mod, "sync_directory", sync_directory)
        assert deploy(c) != 0
    assert active(c) == "b"
    assert c.mod.reconcile() == 0
    assert c.mod.read_state()["active"] == "b"
    assert set(c.live) == {"a", "b"}
    assert "bootout:a" not in c.events and "bootout:b" not in c.events


@pytest.mark.parametrize("boundary", ["resume", "probe"])
def test_rollback_intent_recovers_before_route_switch(
    controller, monkeypatch, boundary
):
    c = controller
    assert deploy(c) == 0
    original = c.mod.control

    def control(slot, op):
        response = original(slot, op)
        if slot == "a" and op == "resume" and boundary == "resume":
            raise SimulatedCrash("resumed before probe")
        return response

    with monkeypatch.context() as patch:
        patch.setattr(c.mod, "control", control)
        if boundary == "probe":
            c.failures.add(("a", "v1"))
            assert c.mod.main(["rollback", "v1"]) != 0
        else:
            with pytest.raises(SimulatedCrash):
                c.mod.main(["rollback", "v1"])
    assert c.mod.read_state()["pending"]["phase"] == "prepared"
    c.failures.clear()
    assert c.mod.main(["reconcile"]) == 0
    assert active(c) == "b" and c.live["a"]["mode"] == "draining"
    assert set(c.live) == {"a", "b"}


@pytest.mark.parametrize(
    "failure", ["private", "public", "both-private", "old-resume", "old-public"]
)
def test_interrupted_deployment_recovers_or_preserves_both_targets(
    controller, monkeypatch, failure
):
    c = controller
    original = c.mod.replace_route

    def crash(runtime, slot):
        original(runtime, slot)
        raise SimulatedCrash("published candidate route")

    with monkeypatch.context() as patch:
        patch.setattr(c.mod, "replace_route", crash)
        with pytest.raises(SimulatedCrash):
            deploy(c)
    c.failures.add(("public" if failure == "public" else "b", "v2"))
    if failure == "both-private":
        c.failures.add(("a", "v1"))
    if failure == "old-resume":
        c.failures.add(("a", "resume"))
    if failure == "old-public":
        c.failures.add(("public", "v1"))
    c.events.clear()
    result = c.mod.main(["reconcile"])
    if failure in {"both-private", "old-resume"}:
        assert result != 0 and active(c) == "b"
        assert not any(event.startswith("route:") for event in c.events)
    elif failure == "old-public":
        assert result != 0 and active(c) == "a"
        assert c.mod.read_state()["pending"] is not None
    else:
        assert result == 0 and active(c) == "a"
        assert c.live["b"]["mode"] == "draining"
    assert set(c.live) == {"a", "b"}
    assert not any(event.startswith(("stop:", "bootout:")) for event in c.events)


def enrollment_launchctl(c, original_run, argv, **kwargs):
    domain = f"gui/{os.getuid()}"
    if argv == ["launchctl", "print", domain]:
        result = original_run(argv, **kwargs)
        extra = "\t\t123 0 com.lfenergy.9router\n" if c.legacy else ""
        if c.proxy is not None:
            extra += "\t\t789 0 com.lfenergy.9router-proxy\n"
        result.stdout = result.stdout.replace(
            "\tservices = {\n", "\tservices = {\n" + extra
        )
        return result
    if argv == ["launchctl", "bootout", f"{domain}/com.lfenergy.9router"]:
        c.legacy = False
        c.events.append("legacy-stopped")
        return SimpleNamespace(returncode=0)
    if argv == [
        "launchctl",
        "bootstrap",
        domain,
        str(c.mod.STATE_DIR / "proxy.plist"),
    ]:
        c.proxy = plistlib.loads(Path(argv[-1]).read_bytes())
        c.events.append("proxy-started")
        return SimpleNamespace(returncode=0)
    if argv == ["launchctl", "print", f"{domain}/com.lfenergy.9router-proxy"]:
        arguments = c.proxy["ProgramArguments"]
        block = "\n".join(f"\t\t{argument}" for argument in arguments)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"job = {{\n\tpath = {c.mod.STATE_DIR / 'proxy.plist'}\n"
                f"\tprogram = {arguments[0]}\n"
                f"\targuments = {{\n{block}\n\t}}\n}}\n"
            ),
        )
    return original_run(argv, **kwargs)


@pytest.fixture
def enrollment_controller(lifecycle_controller, monkeypatch):
    c = lifecycle_controller
    c.mod.STATE_FILE.unlink()
    (c.mod.STATE_DIR / "a.json").unlink()
    (c.mod.STATE_DIR / "a.plist").unlink()
    (c.mod.RUNTIME / "active.sock").unlink()
    (c.mod.RUNTIME / "a.sock").unlink()
    c.jobs.clear()
    c.live.clear()
    source = c.mod.HOME / "env.sh"
    source.write_text(
        f'export DATA_DIR="{c.mod.DB.parent.parent}"\n'
        'export JWT_SECRET="fixture-signing-value"\n'
        'export INITIAL_PASSWORD="fixture-login-value"\n'
        'export NODE_ENV="production"\n'
    )
    source.chmod(0o600)
    dest = c.releases["v1"]
    (dest / "app").mkdir()
    private_json(dest / "app/hotswap-manifest.json", {"protocol": 1})
    c.mod.DB.write_bytes(b"fixture live database")
    c.legacy = True
    c.proxy = None
    original_run = c.mod.subprocess.run

    def run(argv, **kwargs):
        return enrollment_launchctl(c, original_run, argv, **kwargs)

    def native(dest, operation, *arguments):
        assert dest == c.releases["v1"]
        path = Path(arguments[0])
        if "enrollRefreshStore" in operation:
            assert not path.exists()
            path.write_bytes(b"fixture valid refresh store")
            path.chmod(0o600)
        else:
            c.mod.private_file(path)
            if path.read_bytes() != b"fixture valid refresh store":
                raise ValueError("invalid fixture refresh store")

    monkeypatch.setattr(c.mod.subprocess, "run", run)
    monkeypatch.setattr(c.mod, "node_managed", native)
    monkeypatch.setattr(
        c.mod, "validate_package", lambda dest, digest: {"version": "v1"}
    )

    def snapshot(path):
        path.write_bytes(b"fixture snapshot")
        path.chmod(0o600)

    monkeypatch.setattr(c.mod, "snapshot_db", snapshot)
    monkeypatch.setattr(c.mod, "stage_assets", lambda dest: None)
    c.enroll_args = [
        "enroll",
        "--acknowledge-maintenance",
        "--release",
        str(dest),
        "--digest",
        "a" * 64,
    ]
    return c


def test_enrollment_public_failure_preserves_failed_subcheck(
    enrollment_controller, monkeypatch, capsys
):
    c = enrollment_controller
    original = c.mod.verify_at
    monkeypatch.setattr(
        c.mod,
        "verify_at",
        lambda base, version: (
            "models: HTTP Error 401: Unauthorized"
            if base.endswith(":20128")
            else original(base, version)
        ),
    )
    assert c.mod.main(c.enroll_args) == 1
    assert "enrollment public readiness failed: models: HTTP Error 401" in (
        capsys.readouterr().err
    )
    assert c.mod.read_state()["enrollment"] == "routed"


def enrollment_crash_boundary(boundary, expected):
    if boundary == expected:
        raise SimulatedCrash(boundary)


def enrollment_sync_boundary(c, boundary, path):
    targets = {
        "refresh": "refresh.sqlite",
        "snapshot": "pre-enrollment.sqlite",
        "proxy-config": "Caddyfile",
        "proxy-plist": "proxy.plist",
        "worker-plist": "a.plist",
    }
    if (
        boundary in targets
        and path == c.mod.STATE_DIR
        and (path / targets[boundary]).exists()
    ):
        raise SimulatedCrash(boundary)


def enrollment_run_boundary(boundary, original_run, argv, **kwargs):
    if argv[1] == "bootstrap" and (
        (boundary == "worker-bootstrap-refused" and Path(argv[-1]).stem == "a")
        or (boundary == "proxy-bootstrap-refused" and Path(argv[-1]).stem == "proxy")
    ):
        return SimpleNamespace(returncode=1)
    result = original_run(argv, **kwargs)
    if (
        (boundary == "legacy-stop" and argv[1] == "bootout")
        or (
            boundary == "worker-bootstrap"
            and argv[1] == "bootstrap"
            and Path(argv[-1]).stem == "a"
        )
        or (
            boundary == "proxy-bootstrap"
            and argv[1] == "bootstrap"
            and Path(argv[-1]).stem == "proxy"
        )
    ):
        raise SimulatedCrash(boundary)
    return result


def enrollment_dispatch_boundary(c, boundary):
    if boundary == "public":
        c.failures.add(("public", "v1"))
        assert c.mod.main(c.enroll_args) != 0
    elif boundary.endswith("refused"):
        assert c.mod.main(c.enroll_args) != 0
    else:
        with pytest.raises(SimulatedCrash):
            c.mod.main(c.enroll_args)


@pytest.mark.parametrize(
    "boundary",
    [
        "journal",
        "refresh-created",
        "refresh",
        "snapshot-created",
        "snapshot",
        "assets",
        "legacy-stop",
        "worker-config",
        "worker-plist",
        "worker-bootstrap",
        "route",
        "proxy-config",
        "proxy-plist",
        "proxy-bootstrap",
        "public",
        "worker-bootstrap-refused",
        "proxy-bootstrap-refused",
        "bookkeeping",
    ],
)
def test_enrollment_dispatch_resumes_every_maintenance_boundary(
    enrollment_controller, monkeypatch, boundary
):
    c = enrollment_controller
    original_sync = c.mod.sync_directory
    original_run = c.mod.subprocess.run
    original_json = c.mod.atomic_json
    original_route = c.mod.replace_route
    original_write = c.mod.write_state
    original_native = c.mod.node_managed
    original_snapshot = c.mod.snapshot_db

    def native(*args):
        result = original_native(*args)
        enrollment_crash_boundary(boundary, "refresh-created")
        return result

    def snapshot(path):
        original_snapshot(path)
        enrollment_crash_boundary(boundary, "snapshot-created")

    def sync(path):
        original_sync(path)
        enrollment_sync_boundary(c, boundary, path)

    def run(argv, **kwargs):
        return enrollment_run_boundary(boundary, original_run, argv, **kwargs)

    def atomic(path, value):
        original_json(path, value)
        if boundary == "worker-config" and path == c.mod.STATE_DIR / "a.json":
            raise SimulatedCrash(boundary)

    def route(runtime, slot):
        original_route(runtime, slot)
        enrollment_crash_boundary(boundary, "route")

    def write(state):
        original_write(state)
        if boundary == "journal" and state.get("enrollment") == "preparing":
            raise SimulatedCrash(boundary)

    with monkeypatch.context() as patch:
        patch.setattr(c.mod, "sync_directory", sync)
        patch.setattr(c.mod.subprocess, "run", run)
        patch.setattr(c.mod, "atomic_json", atomic)
        patch.setattr(c.mod, "replace_route", route)
        patch.setattr(c.mod, "write_state", write)
        patch.setattr(c.mod, "node_managed", native)
        patch.setattr(c.mod, "snapshot_db", snapshot)
        if boundary == "assets":
            patch.setattr(
                c.mod,
                "stage_assets",
                lambda dest: (_ for _ in ()).throw(SimulatedCrash(boundary)),
            )
        if boundary == "bookkeeping":
            patch.setattr(
                c.mod,
                "convenience_pointer",
                lambda entry: (_ for _ in ()).throw(SimulatedCrash(boundary)),
            )
        enrollment_dispatch_boundary(c, boundary)
    assert_enrollment_recovery(c)


def assert_enrollment_recovery(c):
    refresh = c.mod.STATE_DIR / "refresh.sqlite"
    refresh_before = refresh.read_bytes() if refresh.exists() else None
    snapshot_path = c.mod.STATE_DIR / "pre-enrollment.sqlite"
    snapshot_before = snapshot_path.read_bytes() if snapshot_path.exists() else None
    c.failures.clear()
    assert c.mod.main(["reconcile"]) == 0
    assert c.mod.read_state()["enrollment"] == "complete"
    assert active(c) == "a" and not c.legacy and c.proxy is not None
    assert set(c.jobs) == {"a"} and set(c.live) == {"a"}
    if refresh_before is not None:
        assert refresh.read_bytes() == refresh_before
    if snapshot_before is not None:
        assert snapshot_path.read_bytes() == snapshot_before
    assert c.events.count("legacy-stopped") == 1
    assert c.events.count("proxy-started") == 1


@pytest.mark.parametrize(
    "artifact", ["refresh.sqlite", "pre-enrollment.sqlite", "Caddyfile", "proxy-job"]
)
def test_enrollment_recovery_refuses_foreign_artifact(
    enrollment_controller, monkeypatch, artifact
):
    c = enrollment_controller
    original = c.mod.proxy_enrollment
    with monkeypatch.context() as patch:
        patch.setattr(
            c.mod,
            "proxy_enrollment",
            lambda: (_ for _ in ()).throw(SimulatedCrash("proxy")),
        )
        with pytest.raises(SimulatedCrash):
            c.mod.main(c.enroll_args)
    if artifact in {"refresh.sqlite", "pre-enrollment.sqlite"}:
        # Re-enter preparation to verify existing preparation artifacts.
        state = c.mod.read_state()
        state["enrollment"] = "preparing"
        c.mod.write_state(state)
        (c.mod.STATE_DIR / artifact).write_bytes(b"foreign preparation artifact")
    elif artifact == "Caddyfile":
        (c.mod.STATE_DIR / artifact).write_bytes(b"foreign proxy config")
        (c.mod.STATE_DIR / artifact).chmod(0o600)
    else:
        original()
        c.proxy["ProgramArguments"] = ["/foreign/caddy"]
    assert c.mod.main(["reconcile"]) != 0
    assert active(c) == "a" and set(c.jobs) == {"a"}
    assert not any(event.startswith(("stop:", "bootout:")) for event in c.events)


def test_qualification_commands_never_use_production_defaults(hs, monkeypatch):
    calls = []
    monkeypatch.setattr(hs, "deploy_release", lambda *args: calls.append(args))
    monkeypatch.setattr(hs, "read_state", lambda: calls.append("read"))
    assert hs.main(["deploy-installed", "/foreign/release", "--digest", "a" * 64]) == 1
    assert hs.main(["restore"]) == 1
    assert calls == []


def test_qualification_scope_is_refused_on_the_laptop(hs, monkeypatch):
    monkeypatch.setattr(hs.sys, "platform", "darwin")
    monkeypatch.setattr(
        hs.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="0\n" if Path(command[0]).name == "sysctl" else "arm64\n",
        ),
    )
    with pytest.raises(ValueError, match="virtual machine"):
        hs.configure_qualification(hs.HOME / "scope.json")
    assert hs.QUALIFICATION_SCOPE is None
    assert hs.JOB_PREFIX == "com.lfenergy.9router"


def qualification_scope_fixture(hs, monkeypatch, tmp_path, *, paired=False):
    import hashlib

    volume = tmp_path / "volume"
    source = volume / "source"
    home = volume / "9router/qualification/fixture-run/home"
    releases = home / ".9router/releases"
    installations = (
        {slot: releases / "v1" / slot / "lib/node_modules/9router" for slot in hs.PORTS}
        if paired
        else {None: releases / "v1/lib/node_modules/9router"}
    )
    release = next(iter(installations.values()))
    for path in (source / "scripts/mac", home, home / ".9router", releases):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for dest in installations.values():
        (dest / "app").mkdir(parents=True, mode=0o700)
        private_json(dest / "package.json", {"version": "v1"})
        private_json(
            dest / "app/hotswap-manifest.json",
            {"protocol": 1, "persistenceFingerprint": "fixture-native-sqlite"},
        )
    archive = home / "candidate.tgz"
    archive.write_bytes(b"fixture archive binding; not release acceptance")
    archive.chmod(0o600)
    runtime = {"worker": "a" * 64}
    digest = hs.sha256(archive)
    record = {
        "tarball": str(archive),
        "manifest_sha256": hs.sha256(release / "app/hotswap-manifest.json"),
        "persistenceFingerprint": "fixture-native-sqlite",
        "package_files": hs.installed_package_hashes(release),
    }
    if paired:
        record["installations"] = {
            slot: str(dest) for slot, dest in installations.items()
        }
    else:
        record["release"] = str(release)
    scope = {
        "protocol": 1,
        "phase": "qualification",
        "run_id": "fixture-run",
        "home": str(home),
        "source_root": str(source),
        "source_commit": "b" * 40,
        "source_patch_sha256": "c" * 64,
        "namespace": {"devbox_id": "fixture-box", "instance_id": "fixture-instance"},
        "runtime": "/tmp/9rq-" + hashlib.sha256(b"fixture-run").hexdigest()[:12],
        "runtime_sha256": runtime,
        "proxy_version": "fixture-caddy",
        "packages": {digest: record},
    }
    path = home / "scope.json"
    private_json(path, scope)
    monkeypatch.setattr(hs, "QUALIFICATION_VOLUME", volume)
    monkeypatch.setattr(hs, "HERE", source / "scripts/mac")
    monkeypatch.setattr(hs.sys, "platform", "darwin")
    monkeypatch.setattr(hs.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(hs, "runtime_hashes", lambda: runtime)

    def run(command, **kwargs):
        output = {
            "sysctl": "1",
            "uname": "arm64",
            "git": "b" * 40,
            "caddy": "fixture-caddy",
        }
        return SimpleNamespace(
            returncode=0, stdout=output[Path(command[0]).name] + "\n"
        )

    monkeypatch.setattr(hs, "executable", lambda name: name)
    monkeypatch.setattr(hs.subprocess, "run", run)
    return path, scope, release, digest


def test_guest_scope_binds_real_bytes_without_a_passing_receipt(
    hs, monkeypatch, tmp_path
):
    path, scope, release, digest = qualification_scope_fixture(
        hs, monkeypatch, tmp_path
    )
    hs.configure_qualification(path)
    assert hs.validate_package(release, digest) == {"version": "v1"}
    assert "result" not in hs.qualification(digest)
    assert not hs.QUALIFIED.exists()
    assert hs.LINK == Path(scope["home"]) / ".9router/package-link"
    assert hs.job_label("a").startswith("com.lfenergy.9router-qualify-")
    (release / "package.json").write_text('{"version":"v1","changed":true}')
    with pytest.raises(ValueError, match="differs"):
        hs.validate_package(release, digest)


@pytest.mark.parametrize("slot", ["a", "b"])
def test_paired_scope_enters_installed_dispatch_with_bound_journal(
    hs, monkeypatch, tmp_path, slot
):
    from contextlib import nullcontext

    path, scope, _, digest = qualification_scope_fixture(
        hs, monkeypatch, tmp_path, paired=True
    )
    release = Path(scope["packages"][digest]["installations"][slot])
    calls = []

    def deploy_locked(dest, package_digest, state):
        calls.append((dest, package_digest, state))
        assert hs.validate_release(dest, package_digest) == {"version": "v1"}
        entry = {
            "release": str(dest),
            "version": "v1",
            "digest": digest,
            "mode": "active",
        }
        hs.verify_slot_journal(entry)
        private_json(hs.STATE_DIR / f"{slot}.json", hs.slot_configuration(slot, entry))
        hs.verify_configuration(slot, entry)
        (dest / "package.json").write_text('{"version":"v1","changed":true}')
        with pytest.raises(ValueError, match="differs"):
            hs.validate_package(dest, package_digest)
        return 0

    monkeypatch.setattr(hs, "deployment_lock", nullcontext)
    monkeypatch.setattr(hs, "reconcile_locked", lambda: {"fixture": True})
    monkeypatch.setattr(hs, "deploy_locked", deploy_locked)
    monkeypatch.setattr(hs, "node_managed", lambda *args: "")
    Path(scope["home"], ".9router/hotswap").mkdir(mode=0o700)
    assert (
        hs.main(
            [
                "--qualification-scope",
                str(path),
                "deploy-installed",
                str(release),
                "--digest",
                digest,
            ]
        )
        == 0
    )
    assert calls == [(release, digest, {"fixture": True})]
    assert "result" not in hs.qualification(digest)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "swapped",
        "foreign",
        "version",
        "release",
        "symlink",
        "files",
        "manifest",
        "fingerprint",
        "archive",
        "private",
    ],
)
def test_paired_scope_refuses_unbound_or_changed_installation(
    hs, monkeypatch, tmp_path, mutation
):
    path, scope, _, digest = qualification_scope_fixture(
        hs, monkeypatch, tmp_path, paired=True
    )
    record = scope["packages"][digest]
    installations = record["installations"]
    second = Path(installations["b"])
    if mutation == "missing":
        installations.pop("b")
    elif mutation == "duplicate":
        installations["b"] = installations["a"]
    elif mutation == "swapped":
        installations["a"], installations["b"] = installations["b"], installations["a"]
    elif mutation == "foreign":
        installations["b"] = str(tmp_path / "foreign/lib/node_modules/9router")
    elif mutation == "version":
        installations["b"] = installations["b"].replace("/v1/b/", "/v2/b/")
    elif mutation == "release":
        record["release"] = installations["a"]
    elif mutation == "symlink":
        alias = second.parent / "alias"
        alias.symlink_to(second, target_is_directory=True)
        installations["b"] = str(alias)
    elif mutation == "files":
        (second / "package.json").write_text('{"version":"v1","changed":true}')
    elif mutation == "manifest":
        (second / "app/hotswap-manifest.json").write_text("{}")
    elif mutation == "fingerprint":
        record["persistenceFingerprint"] = "different"
    elif mutation == "archive":
        Path(record["tarball"]).write_bytes(b"changed")
    else:
        second.chmod(0o777)
    private_json(path, scope)
    assert (
        hs.main(
            [
                "--qualification-scope",
                str(path),
                "deploy-installed",
                installations["a"],
                "--digest",
                digest,
            ]
        )
        == 1
    )
    assert hs.QUALIFICATION_SCOPE is None
    assert hs.JOB_PREFIX == "com.lfenergy.9router"


@pytest.mark.parametrize("paired", [False, True])
def test_guest_scope_rejects_undeclared_copy_and_production_slot_layout(
    hs, monkeypatch, tmp_path, paired
):
    path, scope, release, digest = qualification_scope_fixture(
        hs, monkeypatch, tmp_path, paired=paired
    )
    releases = Path(scope["home"]) / ".9router/releases"
    unbound = releases / "v1" / ("c" if paired else "b") / "lib/node_modules/9router"
    for relative in scope["packages"][digest]["package_files"]:
        target = unbound / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes((release / relative).read_bytes())
        target.chmod(0o600)
    with pytest.raises(ValueError, match="confined"):
        hs.concrete_release(unbound, releases=releases)
    if paired:
        with pytest.raises(ValueError, match="confined"):
            hs.concrete_release(release, releases=releases)
    hs.configure_qualification(path)
    assert hs.validate_package(release, digest) == {"version": "v1"}
    with pytest.raises(ValueError, match="confined"):
        hs.validate_package(unbound, digest)


@pytest.mark.parametrize("paired", [False, True])
def test_digest_cannot_authorize_another_records_identical_installation(
    hs, monkeypatch, tmp_path, paired
):
    path, scope, release, digest = qualification_scope_fixture(
        hs, monkeypatch, tmp_path, paired=paired
    )
    releases = Path(scope["home"]) / ".9router/releases"
    record = copy.deepcopy(scope["packages"][digest])
    if paired:
        record.pop("installations")
        record["release"] = str(releases / "v1/lib/node_modules/9router")
        destinations = [Path(record["release"])]
    else:
        record.pop("release")
        record["installations"] = {
            slot: str(releases / "v1" / slot / "lib/node_modules/9router")
            for slot in hs.PORTS
        }
        destinations = [Path(value) for value in record["installations"].values()]
    for dest in destinations:
        for relative in record["package_files"]:
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes((release / relative).read_bytes())
            target.chmod(0o600)
    archive = Path(scope["home"]) / "other.tgz"
    archive.write_bytes(b"another exact archive binding")
    archive.chmod(0o600)
    other_digest = hs.sha256(archive)
    record["tarball"] = str(archive)
    scope["packages"][other_digest] = record
    private_json(path, scope)
    hs.configure_qualification(path)
    other_dest = destinations[-1]
    assert hs.validate_package(other_dest, other_digest) == {"version": "v1"}
    with pytest.raises(ValueError, match="not bound"):
        hs.validate_package(other_dest, digest)


@pytest.mark.parametrize(
    "mutation", ["pass", "home", "runtime", "namespace", "archive", "files"]
)
def test_guest_scope_refuses_forged_or_stale_inputs(
    hs, monkeypatch, tmp_path, mutation
):
    path, scope, release, digest = qualification_scope_fixture(
        hs, monkeypatch, tmp_path
    )
    if mutation == "pass":
        scope["result"] = "pass"
    elif mutation == "home":
        scope["home"] = str(tmp_path / "foreign")
    elif mutation == "runtime":
        scope["runtime"] = "/tmp/foreign"
    elif mutation == "namespace":
        scope["namespace"] = {}
    elif mutation == "archive":
        Path(scope["packages"][digest]["tarball"]).write_bytes(b"changed")
    else:
        (release / "package.json").write_text('{"version":"v1","changed":true}')
    private_json(path, scope)
    with pytest.raises(ValueError):
        hs.configure_qualification(path)
    assert hs.QUALIFICATION_SCOPE is None
    assert hs.JOB_PREFIX == "com.lfenergy.9router"


def test_qualification_scope_refuses_failed_runtime_identity(hs, monkeypatch):
    monkeypatch.setattr(
        hs,
        "QUALIFICATION_SCOPE",
        {"runtime_sha256": {"worker": "a" * 64}, "packages": {"b" * 64: {}}},
    )
    monkeypatch.setattr(hs, "runtime_hashes", lambda: {"worker": "c" * 64})
    with pytest.raises(ValueError, match="runtime changed"):
        hs.qualification("b" * 64)


@pytest.mark.parametrize(
    "mutation",
    [
        "mode",
        "digest",
        "configuration",
        "stopped-digest",
        "active-missing",
        "active-stopped",
        "active-draining",
        "second-active",
    ],
)
def test_restore_validates_every_slot_before_starting_jobs(hs, monkeypatch, mutation):
    from contextlib import nullcontext

    state = {
        "enrollment": "complete",
        "pending": None,
        "active": "a",
        "slots": {
            "a": {"mode": "active", "release": "/fixture/a", "digest": "a" * 64},
            "b": {"mode": "draining", "release": "/fixture/b", "digest": "b" * 64},
        },
    }
    if mutation == "mode":
        state["slots"]["b"]["mode"] = "ready"
    elif mutation == "stopped-digest":
        state["slots"]["b"]["mode"] = "stopped"
    elif mutation == "active-missing":
        state["slots"]["a"] = None
    elif mutation.startswith("active-"):
        state["slots"]["a"]["mode"] = mutation.removeprefix("active-")
    elif mutation == "second-active":
        state["slots"]["b"]["mode"] = "active"
    events = []

    def validate(release, digest):
        events.append("validate:" + release.name)
        if release.name == "b" and mutation in {"digest", "stopped-digest"}:
            raise ValueError("fixture release digest mismatch")

    def verify(slot, entry):
        events.append("verify:" + slot)
        if slot == "b" and mutation == "configuration":
            raise ValueError("fixture configuration mismatch")

    monkeypatch.setattr(hs, "QUALIFICATION_SCOPE", {"phase": "qualification"})
    monkeypatch.setattr(hs, "read_state", lambda: copy.deepcopy(state))
    monkeypatch.setattr(hs, "job_present", lambda _: False)
    monkeypatch.setattr(hs, "deployment_lock", nullcontext)
    monkeypatch.setattr(hs, "validate_release", validate)
    monkeypatch.setattr(hs, "verify_configuration", verify)
    for name in (
        "start_worker",
        "require_probe",
        "replace_route",
        "control",
        "proxy_enrollment",
    ):
        monkeypatch.setattr(hs, name, lambda *args, name=name: events.append(name))
    assert hs.main(["restore"]) == 1
    assert not set(events) & {
        "start_worker",
        "require_probe",
        "replace_route",
        "control",
        "proxy_enrollment",
    }


def test_restore_starts_jobs_only_after_complete_validation(hs, monkeypatch):
    from contextlib import nullcontext

    state = {
        "enrollment": "complete",
        "pending": None,
        "active": "a",
        "slots": {
            "a": {
                "mode": "active",
                "release": "/fixture/a",
                "digest": "a" * 64,
                "version": "v1",
            },
            "b": {
                "mode": "draining",
                "release": "/fixture/b",
                "digest": "b" * 64,
                "version": "v2",
            },
        },
    }
    events = []
    monkeypatch.setattr(hs, "QUALIFICATION_SCOPE", {"phase": "qualification"})
    monkeypatch.setattr(hs, "read_state", lambda: copy.deepcopy(state))
    monkeypatch.setattr(hs, "job_present", lambda _: False)
    monkeypatch.setattr(hs, "deployment_lock", nullcontext)
    monkeypatch.setattr(
        hs,
        "validate_release",
        lambda release, digest: events.append("validate:" + release.name),
    )
    monkeypatch.setattr(
        hs, "verify_configuration", lambda slot, entry: events.append("verify:" + slot)
    )
    monkeypatch.setattr(
        hs, "start_worker", lambda slot, entry: events.append("start:" + slot)
    )
    monkeypatch.setattr(hs, "require_probe", lambda *args: events.append("probe"))
    monkeypatch.setattr(hs, "replace_route", lambda *args: events.append("route"))
    monkeypatch.setattr(hs, "control", lambda *args: events.append("drain"))
    monkeypatch.setattr(hs, "proxy_enrollment", lambda: events.append("proxy"))
    monkeypatch.setattr(hs, "verify_at", lambda *args: None)
    assert hs.main(["restore"]) == 0
    assert events == [
        "validate:a",
        "verify:a",
        "validate:b",
        "verify:b",
        "start:a",
        "start:b",
        "probe",
        "route",
        "drain",
        "proxy",
    ]


@pytest.mark.parametrize("outcome", ["delayed", "timeout", "unreadable"])
def test_bootout_waits_for_positive_job_absence(hs, monkeypatch, outcome):
    clock = [0.0]
    observations = []
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return SimpleNamespace(returncode=0)

    def present(slot):
        observations.append(slot)
        if outcome == "unreadable":
            raise ValueError("launchd job population unreadable")
        return outcome == "timeout" or len(observations) < 3

    monkeypatch.setattr(hs.subprocess, "run", run)
    monkeypatch.setattr(hs, "job_present", present)
    monkeypatch.setattr(hs.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        hs.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    if outcome == "delayed":
        hs.bootout_worker("b")
        assert observations == ["b"] * 3
    else:
        with pytest.raises(ValueError, match="remains loaded|population unreadable"):
            hs.bootout_worker("b")
    assert calls == [
        (["launchctl", "bootout", f"gui/{os.getuid()}/{hs.job_label('b')}"], 10)
    ]


def test_isolated_scope_never_stops_legacy_guest_baseline(hs, monkeypatch):
    monkeypatch.setattr(hs, "QUALIFICATION_SCOPE", {"phase": "qualification"})
    monkeypatch.setattr(
        hs, "job_present", lambda _: pytest.fail("legacy job was inspected")
    )
    saved = []
    monkeypatch.setattr(
        hs, "write_state", lambda value: saved.append(copy.deepcopy(value))
    )
    state = {"enrollment": "maintenance-prepared"}
    hs.stop_enrollment_legacy_service(state)
    assert saved == [{"enrollment": "legacy-stopped"}]
