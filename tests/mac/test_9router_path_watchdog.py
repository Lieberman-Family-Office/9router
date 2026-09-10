"""Unit tests for 9router_path_watchdog kick decisions."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "mac" / "9router_path_watchdog.py"
)


def _load():
    # Register before exec so @dataclass can resolve cls.__module__.
    name = "og_9router_path_watchdog"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec and spec.loader
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
