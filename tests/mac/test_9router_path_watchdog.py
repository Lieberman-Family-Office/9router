"""Unit tests for 9router_path_watchdog kick decisions."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "mac" / "9router_path_watchdog.py"
)


def _load():
    # Register before exec so @dataclass can resolve cls.__module__.
    name = "og_9router_path_watchdog"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wd():
    return _load()


def test_no_kick_on_first_failure(wd):
    state = wd.WatchState()
    results = [
        wd.ProbeResult("local_9router", False, "timeout", ("9router",)),
        wd.ProbeResult("hairpin_ts", True, "http_200", ("tailscale", "9router")),
        wd.ProbeResult("helper", True, "http_200", ("helper",)),
        wd.ProbeResult("ts_backend", True, "BackendState='Running'", ("tailscale",)),
    ]
    kicks, notes = wd.decide_kicks(
        results, state, now_mono=100.0, fail_threshold=2, cooldown_s=180
    )
    assert kicks == []
    assert state.fail_streaks["local_9router"] == 1
    assert any("local_9router:fail:1" in n for n in notes)


def test_second_failure_kicks_targets(wd):
    state = wd.WatchState()
    bad = [
        wd.ProbeResult("local_9router", False, "timeout", ("9router",)),
        wd.ProbeResult("hairpin_ts", False, "timeout", ("tailscale", "9router")),
        wd.ProbeResult("helper", True, "http_200", ("helper",)),
        wd.ProbeResult("ts_backend", True, "BackendState='Running'", ("tailscale",)),
    ]
    kicks1, _ = wd.decide_kicks(
        bad, state, now_mono=100.0, fail_threshold=2, cooldown_s=180
    )
    assert kicks1 == []
    kicks2, _ = wd.decide_kicks(
        bad, state, now_mono=130.0, fail_threshold=2, cooldown_s=180
    )
    assert kicks2 == ["9router", "tailscale"]


def test_cooldown_suppresses_repeat_kick(wd):
    state = wd.WatchState()
    state.fail_streaks["ts_backend"] = 2
    state.last_kick_mono["tailscale"] = 100.0
    results = [
        wd.ProbeResult("local_9router", True, "http_200", ("9router",)),
        wd.ProbeResult("hairpin_ts", True, "http_200", ("tailscale", "9router")),
        wd.ProbeResult("helper", True, "http_200", ("helper",)),
        wd.ProbeResult("ts_backend", False, "BackendState='Stopped'", ("tailscale",)),
    ]
    # streak becomes 3 (>=2), but cooldown active
    kicks, notes = wd.decide_kicks(
        results, state, now_mono=150.0, fail_threshold=2, cooldown_s=180
    )
    assert kicks == []
    assert any("tailscale:cooldown" in n for n in notes)


def test_success_resets_streak(wd):
    state = wd.WatchState()
    state.fail_streaks["helper"] = 1
    results = [
        wd.ProbeResult("local_9router", True, "http_200", ("9router",)),
        wd.ProbeResult("hairpin_ts", True, "http_200", ("tailscale", "9router")),
        wd.ProbeResult("helper", True, "http_200", ("helper",)),
        wd.ProbeResult("ts_backend", True, "BackendState='Running'", ("tailscale",)),
    ]
    kicks, _ = wd.decide_kicks(
        results, state, now_mono=10.0, fail_threshold=2, cooldown_s=180
    )
    assert kicks == []
    assert state.fail_streaks["helper"] == 0


def test_kickstart_command_shapes(wd):
    cmds = wd.kickstart_commands(uid=501, targets=["tailscale", "9router", "helper"])
    assert cmds[0] == [
        "/bin/launchctl",
        "kickstart",
        "-k",
        "system/com.lfenergy.tailscaled",
    ]
    assert cmds[1] == [
        "/bin/launchctl",
        "kickstart",
        "-k",
        "gui/501/com.lfenergy.9router",
    ]
    assert cmds[2] == [
        "/bin/launchctl",
        "kickstart",
        "-k",
        "gui/501/com.lfenergy.9router-combo-helper",
    ]


def test_dry_run_apply_does_not_mutate_last_kick(wd):
    state = wd.WatchState()
    actions = wd.apply_kicks(
        ["9router"],
        uid=501,
        state=state,
        now_mono=999.0,
        dry_run=True,
        runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    assert actions == [
        "dry_run:/bin/launchctl kickstart -k gui/501/com.lfenergy.9router"
    ]
    assert state.last_kick_mono == {}


def _managed_status(*, active="b", active_mode="active"):
    return {
        "route": active,
        "state": {
            "active": active,
            "enrollment": "complete",
            "pending": None,
            "slots": {"a": {"mode": "draining"}, "b": {"mode": active_mode}},
        },
        "workers": {
            "a": {"mode": "draining", "connections": 1},
            "b": {"mode": active_mode, "connections": 0},
        },
        "errors": [],
    }


def _path_results(wd, *, local=True, hairpin=True):
    return [
        wd.ProbeResult("local_9router", local, "http", ("9router",)),
        wd.ProbeResult("hairpin_ts", hairpin, "http", ("tailscale", "9router")),
        wd.ProbeResult("helper", True, "http", ("helper",)),
        wd.ProbeResult("ts_backend", True, "Running", ("tailscale",)),
    ]


def test_watchdog_does_not_restart_healthy_draining_worker(wd, monkeypatch):
    monkeypatch.setattr(wd, "http_probe", lambda *a, **k: (True, "http_200"))
    results = wd.managed_probes(_path_results(wd), _managed_status(), timeout_s=1)
    state = wd.WatchState()
    for now in (100, 130):
        kicks, _ = wd.decide_kicks(
            results, state, now_mono=now, fail_threshold=2, cooldown_s=180
        )
        assert kicks == []
    assert not any("worker-a" in result.targets for result in results)


def test_failed_proxy_targets_only_proxy(wd, monkeypatch):
    monkeypatch.setattr(wd, "http_probe", lambda *a, **k: (True, "http_200"))
    results = wd.managed_probes(
        _path_results(wd, local=False, hairpin=False),
        _managed_status(),
        timeout_s=1,
    )
    state = wd.WatchState()
    for now in (100, 130):
        kicks, _ = wd.decide_kicks(
            results, state, now_mono=now, fail_threshold=2, cooldown_s=180
        )
    assert kicks == ["proxy"]


def test_confirmed_active_failure_does_not_restart_tailscale(wd, monkeypatch):
    monkeypatch.setattr(wd, "http_probe", lambda *a, **k: (False, "timeout"))
    results = wd.managed_probes(
        _path_results(wd, local=False, hairpin=False),
        _managed_status(),
        timeout_s=1,
    )
    state = wd.WatchState()
    for now in (100, 130):
        kicks, _ = wd.decide_kicks(
            results, state, now_mono=now, fail_threshold=2, cooldown_s=180
        )
    assert kicks == ["worker-b"]


@pytest.mark.parametrize(
    "status",
    [
        {},
        {**_managed_status(), "errors": ["slot b identity or work unknown"]},
        {**_managed_status(), "route": "a"},
        {**_managed_status(), "workers": {"b": {"mode": "draining"}}},
    ],
)
def test_unknown_managed_control_alarms_without_worker_kick(wd, status):
    results = wd.managed_probes(
        _path_results(wd, local=False, hairpin=False), status, timeout_s=1
    )
    state = wd.WatchState()
    for now in (100, 130):
        kicks, _ = wd.decide_kicks(
            results, state, now_mono=now, fail_threshold=2, cooldown_s=180
        )
    assert kicks == []
    assert any(result.name == "managed_control" and not result.ok for result in results)


def test_managed_kickstart_labels_are_concrete(wd):
    assert wd.kickstart_commands(
        uid=501, targets=["proxy", "worker-a", "worker-b"]
    ) == [
        ["/bin/launchctl", "kickstart", "-k", "gui/501/com.lfenergy.9router-proxy"],
        ["/bin/launchctl", "kickstart", "-k", "gui/501/com.lfenergy.9router-worker-a"],
        ["/bin/launchctl", "kickstart", "-k", "gui/501/com.lfenergy.9router-worker-b"],
    ]


def test_unenrolled_watchdog_retains_legacy_probe_targets(wd, tmp_path):
    assert wd.managed_status(home=tmp_path, uid=os.getuid()) is None


def test_enrollment_directory_without_journal_is_not_legacy(wd, tmp_path, monkeypatch):
    (tmp_path / ".9router" / "hotswap").mkdir(parents=True)
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "refused"),
    )
    status = wd.managed_status(home=tmp_path, uid=os.getuid())
    assert status is not None
    assert status["errors"]


def test_cycle_uses_managed_decisions(wd, monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "run_probes", lambda **k: _path_results(wd))
    monkeypatch.setattr(wd, "managed_status", lambda **k: _managed_status())
    monkeypatch.setattr(wd, "http_probe", lambda *a, **k: (True, "http_200"))
    calls = []
    monkeypatch.setattr(
        wd, "apply_kicks", lambda targets, **k: calls.extend(targets) or []
    )
    log = tmp_path / "watchdog.log"
    assert (
        wd.cycle(
            home=tmp_path,
            uid=os.getuid(),
            timeout_s=1,
            fail_threshold=2,
            cooldown_s=180,
            ts_socket="unused",
            ts_bin="unused",
            state=wd.WatchState(),
            dry_run=True,
            log_path=log,
        )
        == 0
    )
    assert calls == []
    assert json.loads(log.read_text())["probes"]["managed_control"]["ok"] is True


def test_failure_streak_is_reset_when_active_slot_changes(wd, monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "run_probes", lambda **k: _path_results(wd, local=False))
    status = _managed_status()
    monkeypatch.setattr(wd, "managed_status", lambda **k: status)
    monkeypatch.setattr(wd, "http_probe", lambda *a, **k: (False, "timeout"))
    calls = []
    monkeypatch.setattr(
        wd, "apply_kicks", lambda targets, **k: calls.extend(targets) or []
    )
    state = wd.WatchState()
    for active in ("b", "a"):
        status["route"] = active
        status["state"]["active"] = active
        status["workers"][active]["mode"] = "active"
        wd.cycle(
            home=tmp_path,
            uid=os.getuid(),
            timeout_s=1,
            fail_threshold=2,
            cooldown_s=180,
            ts_socket="unused",
            ts_bin="unused",
            state=state,
            dry_run=True,
            log_path=tmp_path / "watchdog.log",
        )
        assert state.fail_streaks["active_worker"] == 1
    assert calls == []


def test_managed_watchdog_lock_excludes_controller_mutation(wd, tmp_path):
    directory = tmp_path / ".9router" / "hotswap"
    directory.mkdir(parents=True, mode=0o700)
    lock = directory / "deploy.lock"
    lock.write_text("")
    lock.chmod(0o600)
    with wd.managed_lock(tmp_path, os.getuid()) as held:
        assert held is True
        with lock.open() as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with lock.open() as controller:
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with wd.managed_lock(tmp_path, os.getuid()) as held:
            assert held is False


def test_managed_status_runs_as_requested_home(wd, tmp_path, monkeypatch):
    (tmp_path / ".9router" / "hotswap").mkdir(parents=True)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, json.dumps(_managed_status()), ""
        )

    monkeypatch.setattr(wd.subprocess, "run", runner)
    assert wd.managed_status(home=tmp_path, uid=os.getuid()) == _managed_status()
    assert calls[0][0][-1] == "status"
    assert calls[0][1]["env"]["HOME"] == str(tmp_path)


def test_cycle_unknown_enrollment_is_degraded_not_legacy(wd, monkeypatch, tmp_path):
    (tmp_path / ".9router" / "hotswap").mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(
        wd, "run_probes", lambda **k: _path_results(wd, local=False, hairpin=False)
    )
    monkeypatch.setattr(
        wd,
        "managed_status",
        lambda **k: (_ for _ in ()).throw(AssertionError("lock refused")),
    )
    calls = []
    monkeypatch.setattr(
        wd, "apply_kicks", lambda targets, **k: calls.extend(targets) or []
    )
    state = wd.WatchState()
    for _ in range(2):
        assert (
            wd.cycle(
                home=tmp_path,
                uid=os.getuid(),
                timeout_s=1,
                fail_threshold=2,
                cooldown_s=180,
                ts_socket="unused",
                ts_bin="unused",
                state=state,
                dry_run=True,
                log_path=tmp_path / "watchdog.log",
            )
            == 1
        )
    assert calls == []
