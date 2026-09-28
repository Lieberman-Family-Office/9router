"""9router_deploy: pointer switch, no-overwrite, auto-rollback, rollback target."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mac" / "9router_deploy.py"


@pytest.fixture
def dep(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("og_9router_deploy", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "RELEASES", tmp_path / "releases")
    monkeypatch.setattr(mod, "LOG", tmp_path / "deploys.log")
    monkeypatch.setattr(mod, "LINK", tmp_path / "node_modules" / "9router")
    monkeypatch.setattr(mod, "restart", lambda: None)
    monkeypatch.setattr(mod, "snapshot_db", lambda dest: dest.write_text("db"))
    (tmp_path / "node_modules").mkdir()
    return mod


def make_release(dep, version):
    d = dep.release_dir(version)
    d.mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"version": version}))
    return d


def make_tgz(tmp_path, version):
    tgz = tmp_path / f"9router-{version}.tgz"
    data = json.dumps({"version": version}).encode()
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return tgz


def fake_npm(dep, monkeypatch):
    def run(argv, check=False):
        make_release(dep, dep.tgz_version(Path(argv[-1])))

    monkeypatch.setattr(dep.subprocess, "run", run)


def test_stream_terminal_detection(dep):
    assert dep.is_terminal(b"data: [DONE]\n")
    assert dep.is_terminal(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n')
    assert not dep.is_terminal(
        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n'
    )
    assert not dep.is_terminal(b"\n")
    assert not dep.is_terminal(b"data: {not json\n")


def test_adopt_moves_real_dir_behind_symlink(dep):
    dep.LINK.mkdir()
    (dep.LINK / "package.json").write_text(json.dumps({"version": "v7"}))
    assert dep.cmd_adopt(None) == 0
    assert dep.LINK.is_symlink() and dep.live() == dep.release_dir("v7").resolve()


def test_deploy_ok_switches_pointer(dep, tmp_path, monkeypatch):
    dep.LINK.symlink_to(make_release(dep, "v7"))
    fake_npm(dep, monkeypatch)
    monkeypatch.setattr(dep, "verify", lambda v, **k: None)
    rc = dep.cmd_deploy(type("A", (), {"tgz": str(make_tgz(tmp_path, "v8"))}))
    assert rc == 0
    assert dep.live() == dep.release_dir("v8").resolve()
    assert (dep.RELEASES / "v8" / "pre-deploy-data.sqlite").exists()


def test_deploy_refuses_existing_version(dep, tmp_path):
    dep.LINK.symlink_to(make_release(dep, "v7"))
    rc = dep.cmd_deploy(type("A", (), {"tgz": str(make_tgz(tmp_path, "v7"))}))
    assert rc == 1
    assert dep.live() == dep.release_dir("v7").resolve()


def test_failed_verify_rolls_back(dep, tmp_path, monkeypatch):
    dep.LINK.symlink_to(make_release(dep, "v7"))
    fake_npm(dep, monkeypatch)
    monkeypatch.setattr(
        dep, "verify", lambda v, **k: None if v == "v7" else "stream: hung"
    )
    rc = dep.cmd_deploy(type("A", (), {"tgz": str(make_tgz(tmp_path, "v8"))}))
    assert rc == 2
    assert dep.live() == dep.release_dir("v7").resolve()
    entries = [json.loads(x) for x in dep.LOG.read_text().splitlines()]
    assert [e["action"] for e in entries] == ["deploy", "rollback"]
    assert entries[1]["why"] == "stream: hung"


def test_rollback_targets_what_live_version_replaced(dep, monkeypatch):
    for v in ("v7", "v8", "v9"):
        make_release(dep, v)
    dep.LINK.symlink_to(dep.release_dir("v9"))
    dep.LOG.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"action": "deploy", "version": "v8", "frm": "v7", "result": "ok"},
                {"action": "deploy", "version": "v9", "frm": "v8", "result": "ok"},
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(dep, "verify", lambda v, **k: None)
    assert dep.cmd_rollback(type("A", (), {"version": None})) == 0
    assert dep.live() == dep.release_dir("v8").resolve()
