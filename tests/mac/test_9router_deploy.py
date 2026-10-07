"""9router_deploy: pointer switch, no-overwrite, auto-rollback, rollback target."""

from __future__ import annotations

import hashlib
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
    monkeypatch.setattr(mod, "HOME", tmp_path)
    monkeypatch.setattr(mod, "RELEASES", tmp_path / "releases")
    monkeypatch.setattr(mod, "LOG", tmp_path / "deploys.log")
    monkeypatch.setattr(mod, "LINK", tmp_path / "node_modules" / "9router")
    monkeypatch.setattr(mod, "QUALIFIED", tmp_path / "qualified")
    monkeypatch.setattr(mod, "in_vm", lambda: False)
    monkeypatch.setattr(mod, "restart", lambda: None)
    monkeypatch.setattr(mod, "snapshot_db", lambda dest: dest.write_text("db"))
    (tmp_path / "node_modules").mkdir()
    return mod


def make_release(dep, version):
    d = dep.release_dir(version)
    d.mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"version": version}))
    return d


def make_tgz(tmp_path, version, dep=None, result="pass"):
    """Build a tarball; with `dep`, also write a VM qualification record for it."""
    tgz = tmp_path / f"9router-{version}.tgz"
    data = json.dumps({"version": version}).encode()
    with tarfile.open(tgz, "w:gz") as tf:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    if dep is not None:
        dep.QUALIFIED.mkdir(exist_ok=True)
        digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
        me = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
        rec = {"result": result, "deploy_sha256": me}
        (dep.QUALIFIED / f"{digest}.json").write_text(json.dumps(rec))
    return tgz


def deploy_args(tgz):
    return type("A", (), {"tgz": str(tgz)})


def fake_npm(dep, monkeypatch):
    def run(argv, check=False):
        make_release(dep, dep.tgz_version(Path(argv[-1])))

    monkeypatch.setattr(dep.subprocess, "run", run)


def test_release_build_metadata_stays_confined(dep):
    assert (
        dep.release_dir("1.2.3+arm64").relative_to(dep.RELEASES).parts[0]
        == "1.2.3+arm64"
    )


def test_status_skips_invalid_release_entries(dep, monkeypatch, capsys):
    release = make_release(dep, "v1")
    dep.LINK.symlink_to(release)
    (dep.RELEASES / "invalid entry").mkdir()
    (dep.RELEASES / "v2").write_text("partial metadata")
    monkeypatch.setattr(dep, "http", lambda *args, **kwargs: io.BytesIO(b"{}"))
    dep.cmd_status(None)
    output = capsys.readouterr().out
    assert "v1" in output
    assert "invalid entry" not in output


def test_stream_terminal_detection(dep):
    assert dep.is_terminal(b"data: [DONE]\n")
    assert dep.is_terminal(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n')
    assert not dep.is_terminal(
        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n'
    )
    assert not dep.is_terminal(b"\n")
    assert not dep.is_terminal(b"data: {not json\n")


@pytest.mark.parametrize(
    "frame",
    [
        b"event: error\n",
        b'data: {"error":{"message":"failed"}}\n',
        b'data: {"type":"error","error":null}\n',
        b"event: response.failed\n",
        b'data: {"type":"response.incomplete"}\n',
    ],
)
def test_stream_once_rejects_error_before_done_without_retry(dep, monkeypatch, frame):
    calls = []

    def http(*args, **kwargs):
        calls.append((args, kwargs))
        return io.BytesIO(frame + b"\ndata: [DONE]\n\n")

    monkeypatch.setattr(dep, "http", http)
    assert dep.stream_probe(5) == "stream: upstream error event"
    assert len(calls) == 1


def test_stream_once_accepts_successful_sse(dep, monkeypatch):
    monkeypatch.setattr(
        dep,
        "http",
        lambda *args, **kwargs: io.BytesIO(
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        ),
    )
    assert dep.stream_once(5) == (None, False)


def scripted_probe(dep, monkeypatch, outcomes):
    calls = iter(outcomes)
    monkeypatch.setattr(dep, "stream_once", lambda timeout: next(calls))
    monkeypatch.setattr(dep.time, "sleep", lambda s: None)
    return calls


def test_probe_retries_transient_then_passes(dep, monkeypatch):
    left = scripted_probe(dep, monkeypatch, [("stream: 503", True), (None, False)])
    assert dep.stream_probe() is None
    assert next(left, "drained") == "drained"


def test_probe_wedged_release_still_fails(dep, monkeypatch):
    timeout = ("stream: not terminated within 90s", True)
    left = scripted_probe(dep, monkeypatch, [timeout] * dep.PROBE_ATTEMPTS)
    assert dep.stream_probe() == f"{timeout[0]} (attempt 3/3)"
    assert next(left, "drained") == "drained"


def test_probe_non_transient_fails_without_retry(dep, monkeypatch):
    left = scripted_probe(dep, monkeypatch, [("stream: 401", False), (None, False)])
    assert dep.stream_probe() == "stream: 401"
    assert next(left) == (None, False)  # second attempt never consumed


@pytest.mark.parametrize(
    "exc, transient",
    [
        (TimeoutError("timed out"), True),  # the 2026-09-28 .8 rollback
        ("http:503", True),
        ("http:429", True),
        ("http:401", False),
        (ConnectionRefusedError(), False),
    ],
)
def test_stream_once_classifies_failures(dep, monkeypatch, exc, transient):
    if isinstance(exc, str):
        code = int(exc.split(":")[1])
        exc = dep.urllib.error.HTTPError("u", code, "m", None, None)

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(dep, "http", boom)
    reason, got = dep.stream_once(1)
    assert reason.startswith("stream:") and got is transient


def test_adopt_moves_real_dir_behind_symlink(dep):
    dep.LINK.mkdir()
    (dep.LINK / "package.json").write_text(json.dumps({"version": "v7"}))
    assert dep.cmd_adopt(None) == 0
    assert dep.LINK.is_symlink() and dep.live() == dep.release_dir("v7").resolve()


def test_deploy_ok_switches_pointer(dep, tmp_path, monkeypatch):
    dep.LINK.symlink_to(make_release(dep, "v7"))
    fake_npm(dep, monkeypatch)
    monkeypatch.setattr(dep, "verify", lambda v, **k: None)
    rc = dep.cmd_deploy(deploy_args(make_tgz(tmp_path, "v8", dep)))
    assert rc == 0
    assert dep.live() == dep.release_dir("v8").resolve()
    assert (dep.RELEASES / "v8" / "pre-deploy-data.sqlite").exists()


def test_deploy_refuses_existing_version(dep, tmp_path):
    dep.LINK.symlink_to(make_release(dep, "v7"))
    rc = dep.cmd_deploy(deploy_args(make_tgz(tmp_path, "v7", dep)))
    assert rc == 1
    assert dep.live() == dep.release_dir("v7").resolve()


@pytest.mark.parametrize("record", [None, "fail", "other-bytes", "other-deploy"])
def test_deploy_refuses_without_vm_pass(dep, tmp_path, monkeypatch, record):
    dep.LINK.symlink_to(make_release(dep, "v7"))
    fake_npm(dep, monkeypatch)
    monkeypatch.setattr(dep, "verify", lambda v, **k: None)
    if record == "fail":
        tgz = make_tgz(tmp_path, "v8", dep, result="fail")
    elif record == "other-bytes":
        tgz = make_tgz(tmp_path, "v8", dep)  # qualify these bytes...
        tgz.write_bytes(tgz.read_bytes() + b"\0")  # ...then change them
    elif record == "other-deploy":
        tgz = make_tgz(tmp_path, "v8", dep)
        rec = next(dep.QUALIFIED.glob("*.json"))
        rec.write_text(json.dumps({"result": "pass", "deploy_sha256": "0" * 64}))
    else:
        tgz = make_tgz(tmp_path, "v8")
    assert dep.cmd_deploy(deploy_args(tgz)) == 1
    assert dep.live() == dep.release_dir("v7").resolve()
    assert not (dep.RELEASES / "v8").exists()


def test_deploy_inside_vm_needs_no_record(dep, tmp_path, monkeypatch):
    dep.LINK.symlink_to(make_release(dep, "v7"))
    fake_npm(dep, monkeypatch)
    monkeypatch.setattr(dep, "verify", lambda v, **k: None)
    monkeypatch.setattr(dep, "in_vm", lambda: True)
    assert dep.cmd_deploy(deploy_args(make_tgz(tmp_path, "v8"))) == 0


def test_failed_verify_rolls_back(dep, tmp_path, monkeypatch):
    dep.LINK.symlink_to(make_release(dep, "v7"))
    fake_npm(dep, monkeypatch)
    monkeypatch.setattr(
        dep, "verify", lambda v, **k: None if v == "v7" else "stream: hung"
    )
    rc = dep.cmd_deploy(deploy_args(make_tgz(tmp_path, "v8", dep)))
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


@pytest.mark.parametrize("operation", ["deploy", "rollback"])
def test_enrolled_dispatch_never_enters_legacy_mutation(
    dep, tmp_path, monkeypatch, operation
):
    calls = []
    fake = type(
        "Controller",
        (),
        {
            "deploy_tarball": staticmethod(
                lambda tgz: calls.append(("deploy", tgz)) or 17
            ),
            "rollback_release": staticmethod(
                lambda version: calls.append(("rollback", version)) or 19
            ),
        },
    )
    (dep.HOME / "hotswap").mkdir()
    (dep.HOME / "hotswap/state.json").write_text("{corrupted-but-enrolled")
    monkeypatch.setattr(dep, "load_hotswap", lambda: fake)

    def prohibited(*args, **kwargs):
        pytest.fail("Enrolled dispatch entered legacy pointer/restart/install path")

    for name in ("restart", "switch", "snapshot_db", "unqualified"):
        monkeypatch.setattr(dep, name, prohibited)
    monkeypatch.setattr(dep.subprocess, "run", prohibited)
    if operation == "deploy":
        tgz = tmp_path / "candidate.tgz"
        assert dep.cmd_deploy(deploy_args(tgz)) == 17
        assert calls == [("deploy", tgz.resolve())]
    else:
        assert dep.cmd_rollback(type("A", (), {"version": "v1"})) == 19
        assert calls == [("rollback", "v1")]


@pytest.mark.parametrize(
    "version", ["../escape", "/absolute", "v2/nested", ".", "..", ""]
)
def test_release_path_rejects_invalid_version_before_join(dep, version):
    with pytest.raises(ValueError):
        dep.release_dir(version)


def test_partial_enrollment_never_falls_back_to_legacy_deploy(dep, monkeypatch):
    (dep.HOME / "hotswap").mkdir()
    calls = []
    fake = type(
        "Controller",
        (),
        {
            "deploy_tarball": staticmethod(lambda tgz: calls.append(tgz) or 1),
        },
    )
    monkeypatch.setattr(dep, "load_hotswap", lambda: fake)

    def prohibited(*args, **kwargs):
        pytest.fail("Partial enrollment entered legacy deployment")

    monkeypatch.setattr(dep, "live", prohibited)
    assert dep.cmd_deploy(deploy_args(dep.HOME / "candidate.tgz")) == 1
    assert calls == [(dep.HOME / "candidate.tgz").resolve()]


def test_http_explicit_base_does_not_change_global(dep, monkeypatch):
    seen = []
    original_base = dep.BASE
    monkeypatch.setattr(
        dep.urllib.request, "urlopen", lambda req, timeout: seen.append(req.full_url)
    )
    dep.http("/api/version", base="http://127.0.0.1:21130")
    assert seen == ["http://127.0.0.1:21130/api/version"]
    assert dep.BASE == original_base


@pytest.mark.parametrize(
    "path, body, authenticated",
    [
        ("/api/version", None, False),
        ("/v1/models", None, True),
        ("/v1/chat/completions", {"stream": True}, True),
    ],
)
def test_http_authenticates_models_without_changing_get_method(
    dep, monkeypatch, path, body, authenticated
):
    seen = []
    monkeypatch.setattr(dep, "api_key", lambda: "fixture-client-key")
    monkeypatch.setattr(
        dep.urllib.request, "urlopen", lambda req, timeout: seen.append(req)
    )
    dep.http(path, body, base="http://127.0.0.1:20128")
    assert seen[0].get_header("Authorization") == (
        "Bearer fixture-client-key" if authenticated else None
    )
    assert seen[0].get_method() == ("GET" if body is None else "POST")


def test_verify_reports_missing_models_probe_key(dep, monkeypatch):
    def urlopen(req, timeout):
        assert req.full_url.endswith("/api/version")
        return io.BytesIO(b'{"currentVersion":"v2"}')

    def missing_key():
        raise RuntimeError("no active API key for the stream probe")

    monkeypatch.setattr(dep.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(dep, "api_key", missing_key)
    assert dep.verify("v2") == "models: no active API key for the stream probe"


def test_stream_probe_propagates_private_base_without_mutating_global(dep, monkeypatch):
    seen = []
    original_base = dep.BASE

    def stream_once(timeout, *, base=dep.BASE):
        seen.append(base)
        return None, False

    monkeypatch.setattr(dep, "stream_once", stream_once)
    assert dep.stream_probe(base="http://127.0.0.1:21130") is None
    assert seen == ["http://127.0.0.1:21130"] and dep.BASE == original_base


def test_verify_propagates_private_base_to_all_probes(dep, monkeypatch):
    seen = []

    def http(path, body=None, timeout=10, *, base=dep.BASE):
        seen.append((path, base))
        value = {"currentVersion": "v2"} if path == "/api/version" else {"data": [{}]}
        return io.BytesIO(json.dumps(value).encode())

    def stream_probe(timeout=90, *, base=dep.BASE):
        seen.append(("stream", base))
        return None

    monkeypatch.setattr(dep, "http", http)
    monkeypatch.setattr(dep, "stream_probe", stream_probe)
    assert dep.verify("v2", base="http://127.0.0.1:21130") is None
    assert seen == [
        ("/api/version", "http://127.0.0.1:21130"),
        ("/v1/models", "http://127.0.0.1:21130"),
        ("stream", "http://127.0.0.1:21130"),
    ]


def test_stream_once_uses_explicit_private_base(dep, monkeypatch):
    seen = []

    def http(path, body=None, timeout=10, *, base=dep.BASE):
        seen.append(base)
        return io.BytesIO(b"data: [DONE]\n")

    monkeypatch.setattr(dep, "http", http)
    assert dep.stream_once(1, base="http://127.0.0.1:21130") == (None, False)
    assert seen == ["http://127.0.0.1:21130"]
