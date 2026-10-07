"""Qualification checks. Execute only inside an authorized Namespace guest."""

import copy
import importlib.util
import io
import json
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mac/9router_vm_qualify.py"
spec = importlib.util.spec_from_file_location("qualify", SCRIPT)
qualify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualify)


def write_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def fixture(root):
    identity = {
        "protocol": 1,
        "sha256": "a" * 64,
        "runtime_sha256": {key: "b" * 64 for key in qualify.RUNTIME_KEYS},
        "source_commit": "c" * 40,
        "source_patch_sha256": "d" * 64,
        "namespace": {"devbox_id": "fixture-box", "instance_id": "fixture-instance"},
        "proxy_version": "v2.11.4 fixture",
        "manifest_sha256": "e" * 64,
        "persistenceFingerprint": "fixture-persistence",
        "guest_binaries": {
            "node": {
                "path": "/guest/bin/node",
                "sha256": "b" * 64,
                "version": "fixture",
            },
            "caddy": {
                "path": "/guest/bin/caddy",
                "sha256": "b" * 64,
                "version": "v2.11.4 fixture",
            },
        },
        "guest_versions": {
            "macos": "fixture",
            "node": "fixture",
            "python": "fixture",
            "caddy": "v2.11.4 fixture",
            "architecture": "arm64",
            "locked_dependencies": {"package-lock.json": "f" * 64},
        },
        "package_files": {"package.json": "f" * 64},
        "run_id": "fixture-run",
        "execution_id": "fixture-exec",
        "required_checks": sorted(qualify.CHECKS),
    }
    transitions = [
        {
            "from": old,
            "to": new,
            "new_http_completed": 100,
            "old_sse_open_before": 1,
            "old_ws_open_before": 1,
            "old_sse_terminal": 1,
            "old_ws_turns_after": 2,
            "errors": 0,
            "truncations": 0,
            "proxy_pid_before": 17,
            "proxy_pid_after": 17,
        }
        for old, new in (("a", "b"), ("b", "a"))
    ]
    measurements = {
        "continuity": {
            "transitions": transitions,
            "provider": "deterministic-guest-fixture",
        },
        "authentication": {
            "live_streams_completed": 3,
            "concurrent_streams_completed": 4,
            "concurrent_api_polls": 2,
            "concurrent_api_max_latency_s": 0.1,
            "routing_models": 3,
            "unauthorized_refusals": 1,
            "guest_credentials_only": True,
            "errors": 0,
        },
        "recovery": {
            "intentional_crashes": 1,
            "recovered_requests": 1,
            "app_pid_before": 31,
            "app_pid_after": 32,
            "pid_source": "private-worker-status",
        },
    }
    checks = {}
    for name, measurement in measurements.items():
        filename, logname = name + ".json", name + ".log"
        write_json(
            root / filename,
            {
                "identity": identity,
                "exit_code": 0,
                "subjects": 2,
                "measurement": measurement,
            },
        )
        (root / logname).write_text("fixture log; not runtime acceptance evidence\n")
        checks[name] = {
            "exit_code": 0,
            "subjects": 2,
            "evidence_file": filename,
            "evidence_sha256": qualify.sha256(root / filename),
            "log_file": logname,
        }
    write_json(root / "identity.json", identity)
    write_json(root / "checks.json", checks)
    files = {path.name: qualify.sha256(path) for path in root.iterdir()}
    write_json(root / "manifest.json", {"identity": identity, "files": files})
    expected = {
        **copy.deepcopy(identity),
        "evidence_manifest_sha256": qualify.sha256(root / "manifest.json"),
    }
    cleanup = {
        "verified": True,
        "stop_succeeded": True,
        "connections_closed": True,
        "observations": [
            {"at": 1000, "monotonic": 10, "state": "stopped", "instance_id": None},
            {"at": 1060, "monotonic": 70, "state": "stopped", "instance_id": None},
        ],
    }
    return {
        **identity,
        "checks": checks,
        "guest_result": "pass",
        "result": "incomplete",
        "cleanup": cleanup,
    }, expected


def rebind_measurement(root, record, expected, name, mutation):
    path = root / record["checks"][name]["evidence_file"]
    value = json.loads(path.read_text())
    mutation(value["measurement"])
    write_json(path, value)
    record["checks"][name]["evidence_sha256"] = qualify.sha256(path)
    write_json(root / "checks.json", record["checks"])
    manifest = json.loads((root / "manifest.json").read_text())
    for filename in (path.name, "checks.json"):
        manifest["files"][filename] = qualify.sha256(root / filename)
    write_json(root / "manifest.json", manifest)
    expected["evidence_manifest_sha256"] = qualify.sha256(root / "manifest.json")


class ReceiptTest(unittest.TestCase):
    def test_exported_bytes_and_complete_populations_are_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record, expected = fixture(root)
            self.assertEqual(qualify.finish(record, root, expected)["result"], "pass")

    def test_format_only_and_tampered_exports_are_refused(self):
        for mutation in (
            lambda root, record, expected: (root / "continuity.log").write_text(
                "tampered"
            ),
            lambda root, record, expected: (root / "identity.json").unlink(),
            lambda root, record, expected: expected.update(
                evidence_manifest_sha256="0" * 64
            ),
            lambda root, record, expected: record["runtime_sha256"].update(
                worker="0" * 64
            ),
            lambda root, record, expected: record["guest_binaries"]["node"].update(
                path="/guest/other/node"
            ),
            lambda root, record, expected: record["guest_binaries"]["caddy"].update(
                sha256="0" * 64
            ),
            lambda root, record, expected: record["guest_versions"].update(
                node="stale-version"
            ),
            lambda root, record, expected: record["namespace"].update(
                instance_id="stale"
            ),
            lambda root, record, expected: record["checks"]["continuity"].update(
                subjects=0
            ),
            lambda root, record, expected: record["checks"].pop("recovery"),
            lambda root, record, expected: record.update(guest_result="unrun"),
            lambda root, record, expected: record.update(failure="TimeoutExpired"),
        ):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                record, expected = fixture(root)
                mutation(root, record, expected)
                with self.assertRaises(ValueError):
                    qualify.finish(record, root, expected)

    def test_hash_bound_but_insufficient_continuity_is_refused(self):
        for field, value in (
            ("new_http_completed", 99),
            ("old_sse_open_before", 0),
            ("old_ws_open_before", 0),
            ("old_sse_terminal", 0),
            ("old_ws_turns_after", 1),
            ("errors", 1),
            ("truncations", 1),
            ("new_http_completed", True),
            ("proxy_pid_after", 18),
        ):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                root = Path(directory)
                record, expected = fixture(root)
                rebind_measurement(
                    root,
                    record,
                    expected,
                    "continuity",
                    lambda m: m["transitions"][1].update({field: value}),
                )
                with self.assertRaises(ValueError):
                    qualify.finish(record, root, expected)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record, expected = fixture(root)
            rebind_measurement(
                root, record, expected, "continuity", lambda m: m.update(transitions=[])
            )
            with self.assertRaises(ValueError):
                qualify.finish(record, root, expected)

    def test_authentication_and_recovery_cannot_fill_healthy_population(self):
        for name, mutation in (
            ("authentication", lambda m: m.update(live_streams_completed=0)),
            ("authentication", lambda m: m.update(concurrent_streams_completed=3)),
            ("authentication", lambda m: m.pop("concurrent_api_polls")),
            ("authentication", lambda m: m.update(concurrent_api_polls=0)),
            ("authentication", lambda m: m.update(concurrent_api_polls=True)),
            (
                "authentication",
                lambda m: m.update(concurrent_api_max_latency_s=qualify.STALL_S + 0.01),
            ),
            (
                "authentication",
                lambda m: m.update(concurrent_api_max_latency_s=float("nan")),
            ),
            (
                "authentication",
                lambda m: m.update(concurrent_api_max_latency_s=float("inf")),
            ),
            ("authentication", lambda m: m.update(concurrent_api_max_latency_s=True)),
            ("authentication", lambda m: m.update(concurrent_api_max_latency_s=-1)),
            ("authentication", lambda m: m.update(guest_credentials_only=False)),
            ("recovery", lambda m: m.update(intentional_crashes=0)),
            ("recovery", lambda m: m.update(pid_source="first-pgrep-match")),
            ("recovery", lambda m: m.update(app_pid_after=31)),
        ):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                record, expected = fixture(root)
                rebind_measurement(root, record, expected, name, mutation)
                with self.assertRaises(ValueError):
                    qualify.finish(record, root, expected)

    def test_cleanup_failure_reactivation_and_short_interval_refuse(self):
        for mutation in (
            lambda c: c.update(stop_succeeded=False),
            lambda c: c.update(connections_closed=False),
            lambda c: c["observations"][1].update(
                state="running", instance_id="restarted"
            ),
            lambda c: c["observations"][1].update(at=1059),
            lambda c: c["observations"][1].update(monotonic=69),
            lambda c: c["observations"][1].update(at=float("nan")),
            lambda c: c["observations"][1].pop("instance_id"),
        ):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                record, expected = fixture(root)
                mutation(record["cleanup"])
                with self.assertRaises(ValueError):
                    qualify.finish(record, root, expected)


class LifecycleTest(unittest.TestCase):
    def fixture_metadata(self, events, cleanup_failure):
        observations = [0]

        def metadata(devbox_id):
            events.append("metadata")
            observations[0] += 1
            if cleanup_failure == "unreadable" and observations[0] == 2:
                raise OSError("fixture metadata failure")
            state = (
                "running"
                if cleanup_failure == "reactivation" and observations[0] == 3
                else "stopped"
            )
            return {
                "id": devbox_id,
                "name": "fixture-box",
                "state": state,
                "instance_id": "restarted" if state == "running" else None,
            }

        return metadata

    def fixture_work(self, events, phase):
        def work():
            for name in ("provisioning", "setup", "testing", "export"):
                events.append(name)
                if phase == name:
                    raise RuntimeError("fixture failure")
            if phase == "timeout":
                raise TimeoutError("fixture timeout")
            if phase == "interruption":
                raise KeyboardInterrupt
            return {"result": "pass", "guest_result": "pass"}

        return work

    def exercise(self, phase=None, cleanup_failure=None):
        events, ticks = [], [100.0]
        metadata = self.fixture_metadata(events, cleanup_failure)
        work = self.fixture_work(events, phase)

        def close():
            events.append("close")
            if cleanup_failure == "close":
                raise OSError("fixture close failure")

        def stop(name):
            events.append("stop:" + name)
            if cleanup_failure == "stop":
                raise OSError("fixture stop failure")

        def wait(seconds):
            events.append("wait")
            ticks[0] += seconds

        result = qualify.qualification_lifecycle(
            "fixture-box",
            "fixture-id",
            work,
            metadata,
            close,
            stop=stop,
            wait=wait,
            clock=lambda: ticks[0],
            now=lambda: ticks[0] + 1000,
        )
        return result, events

    def test_every_work_failure_still_closes_stops_and_observes_twice(self):
        for phase in (
            "provisioning",
            "setup",
            "testing",
            "export",
            "timeout",
            "interruption",
        ):
            with self.subTest(phase=phase):
                result, events = self.exercise(phase)
                self.assertEqual(result["result"], "fail")
                self.assertTrue(result["cleanup"]["verified"])
                self.assertEqual(events.count("metadata"), 3)
                self.assertLess(events.index("close"), events.index("stop:fixture-box"))
                self.assertIn("wait", events)

    def test_cleanup_failure_cannot_pass_even_with_guest_success(self):
        for failure in ("stop", "close", "unreadable", "reactivation"):
            with self.subTest(failure=failure):
                result, events = self.exercise(cleanup_failure=failure)
                self.assertEqual(result["guest_result"], "pass")
                self.assertEqual(result["result"], "fail")
                self.assertFalse(result["cleanup"]["verified"])
                self.assertEqual(events.count("metadata"), 3)
                self.assertIn("stop:fixture-box", events)
                self.assertIn("wait", events)

    def test_successful_callback_is_not_a_qualification_receipt(self):
        result, _ = self.exercise()
        self.assertEqual(result["result"], "incomplete")
        self.assertTrue(result["cleanup"]["verified"])

    def test_foreign_or_running_devbox_is_not_claimed_or_stopped(self):
        for before in (
            {
                "id": "foreign",
                "name": "fixture-box",
                "state": "stopped",
                "instance_id": None,
            },
            {
                "id": "fixture-id",
                "name": "fixture-box",
                "state": "running",
                "instance_id": "live",
            },
        ):
            calls = []
            result = qualify.qualification_lifecycle(
                "fixture-box",
                "fixture-id",
                lambda: calls.append("work"),
                lambda _: before,
                lambda: calls.append("close"),
                stop=lambda _: calls.append("stop"),
            )
            self.assertEqual(result["result"], "fail")
            self.assertEqual(calls, [])


class RuntimeBindingTest(unittest.TestCase):
    def runtime_fixture(self, root):
        source = root / "source"
        for name, relative in qualify.RUNTIME_SOURCES.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture source: " + name)
        tools = root / "tools"
        tools.mkdir()
        binaries = {}
        for name, version in (("node", "v26.10.0"), ("caddy", "v2.11.4 fixture")):
            path = tools / name
            path.write_text("guest native bytes: " + name)
            path.chmod(0o700)
            binaries[name] = {
                "path": str(path),
                "sha256": qualify.sha256(path),
                "version": version,
            }
        binding = {"source_runtime_sha256": qualify.source_runtime_hashes(source)}
        qualify.bind_guest_runtime(binding, copy.deepcopy(binaries))
        return source, binding, binaries

    def test_preflight_accepts_guest_binaries_and_pins_fixture_controller_path(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(qualify.os.environ, {"PATH": "/usr/bin:/bin"}),
        ):
            root = Path(directory).resolve()
            source, binding, binaries = self.runtime_fixture(root)
            versions = {item["path"]: item["version"] for item in binaries.values()}
            controller = qualify.controller_module()
            with (
                patch.object(qualify, "ROOT", source),
                patch.object(qualify, "controller_module", return_value=controller),
                patch.object(
                    controller,
                    "__file__",
                    str(source / qualify.RUNTIME_SOURCES["controller"]),
                ),
                patch.object(controller, "HERE", source / "scripts/mac"),
                patch.object(
                    qualify, "output", side_effect=lambda argv: versions[argv[0]]
                ),
            ):
                qualify.runtime_preflight(binding, root)
                self.assertEqual(controller.runtime_hashes(), binding["runtime_sha256"])
                env = qualify.isolated_environment(root / "home", source)
                for name, item in binaries.items():
                    selected = qualify.shutil.which(name, path=env["PATH"])
                    self.assertEqual(str(Path(selected).resolve()), item["path"])
                    self.assertEqual(binding["runtime_sha256"][name], item["sha256"])
                self.assertEqual(qualify.guest_binary_bindings(), binaries)
                self.assertEqual(
                    binding["guest_binaries"]["node"]["version"], "v26.10.0"
                )
                self.assertEqual(binding["proxy_version"], binaries["caddy"]["version"])

    def test_guest_dispatch_refuses_static_and_native_mismatch_before_dependencies(
        self,
    ):
        for mutation, reason in (
            (
                lambda source, binding: (
                    source / qualify.RUNTIME_SOURCES["worker"]
                ).write_text("changed"),
                "guest static runtime differs",
            ),
            (
                lambda source, binding: binding["guest_binaries"]["node"].update(
                    sha256="0" * 64
                ),
                "guest binaries differ",
            ),
            (
                lambda source, binding: binding["guest_binaries"]["node"].update(
                    version="v26.6.0"
                ),
                "guest binaries differ",
            ),
            (
                lambda source, binding: binding["runtime_sha256"].update(
                    caddy="0" * 64
                ),
                "qualification runtime differs",
            ),
            (
                lambda source, binding: binding["guest_binaries"]["node"].update(
                    path="relative/node"
                ),
                "guest binary identity incomplete",
            ),
        ):
            with (
                self.subTest(reason=reason),
                TemporaryDirectory() as directory,
                patch.dict(qualify.os.environ, {"PATH": "/usr/bin:/bin"}),
            ):
                root = Path(directory).resolve()
                source, binding, binaries = self.runtime_fixture(root)
                binding.update(
                    protocol=1,
                    sha256="a" * 64,
                    source_commit="c" * 40,
                    source_patch_sha256="d" * 64,
                    run_id="fixture-run",
                    namespace={
                        "devbox_id": "fixture-box",
                        "instance_id": "fixture-instance",
                    },
                )
                run_root = root / "qualification/fixture-run"
                run_root.mkdir(parents=True)
                mutation(source, binding)
                write_json(run_root / "input.json", binding)
                write_json(
                    run_root / "execution.json",
                    {"run_id": binding["run_id"], "execution_id": "exec_fixture"},
                )
                versions = {item["path"]: item["version"] for item in binaries.values()}
                with (
                    patch.object(qualify, "ROOT", source),
                    patch.object(qualify, "GUEST_ROOT", root / "qualification"),
                    patch.object(qualify, "guest_guard"),
                    patch.object(qualify, "source_binding"),
                    patch.object(qualify, "controller_module") as controller,
                    patch.object(
                        qualify, "output", side_effect=lambda argv: versions[argv[0]]
                    ),
                    patch.object(qualify, "provision_dependencies") as dependencies,
                    patch.object(qualify, "prerequisite_checks") as tests,
                ):
                    controller.return_value.runtime_hashes.return_value = {
                        **binding["source_runtime_sha256"],
                        **{name: item["sha256"] for name, item in binaries.items()},
                    }
                    self.assertEqual(
                        qualify.main(
                            [
                                "guest",
                                "candidate.tgz",
                                "--input",
                                str(run_root / "input.json"),
                            ]
                        ),
                        1,
                    )
                    dependencies.assert_not_called()
                    tests.assert_not_called()
                self.assertIn(
                    reason,
                    qualify.private_json(run_root / "evidence/failure.json")["blocker"],
                )

    def test_host_stage_binds_static_source_without_host_native_observations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, _, _ = self.runtime_fixture(root)
            stage = root / "stage"
            stage.mkdir()
            tarball = root / "candidate.tgz"
            tarball.write_bytes(b"fixture package")
            commit = "c" * 40
            revisions = {
                "5fd82262218f5f00b4f987587318908e548ad65a",
                "e08fa1a43d89caeefa880a612db445c68d578d01",
            }

            def git_output(argv, *args, **kwargs):
                self.assertEqual(
                    argv[0], "git", "host must not observe native binary versions"
                )
                if argv[1:] == ["rev-parse", "HEAD"]:
                    return commit
                if argv[1:] == ["ls-files", "-z"]:
                    return "\x00".join(qualify.RUNTIME_SOURCES.values())
                if argv[1] == "ls-files":
                    return ""
                if (
                    argv[1] == "rev-parse"
                    and argv[2].removesuffix("^{commit}") in revisions
                ):
                    return argv[2].removesuffix("^{commit}")
                if argv[1] == "for-each-ref":
                    return "refs/heads/fixture"
                if argv[1:3] == ["bundle", "create"]:
                    Path(argv[3]).write_bytes(b"fixture bundle")
                    return ""
                raise AssertionError("unexpected host command: " + repr(argv))

            with (
                patch.object(qualify, "ROOT", source),
                patch.object(qualify, "output", side_effect=git_output),
                patch.object(qualify.subprocess, "run") as command,
                patch.object(
                    qualify,
                    "controller_module",
                    side_effect=AssertionError("host native lookup"),
                ),
                patch.object(qualify, "package_preflight", return_value="a" * 64),
            ):
                command.return_value.stdout = b"fixture qualification patch"
                staged = qualify.host_stage(
                    tarball, unittest.mock.Mock(source_file=[]), stage
                )
            self.assertEqual(
                staged["source_runtime_sha256"], qualify.source_runtime_hashes(source)
            )
            self.assertNotIn("runtime_sha256", staged)
            self.assertNotIn("proxy_version", staged)
            self.assertNotIn("guest_binaries", staged)
            self.assertEqual(
                staged["source_patch_sha256"],
                qualify.sha256(stage / "qualification.patch"),
            )

    def test_guest_runtime_reuses_existing_pinned_caddy_without_download(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, _, binaries = self.runtime_fixture(root)
            versions = {item["path"]: item["version"] for item in binaries.values()}
            with (
                patch.dict(qualify.os.environ, {"PATH": str(root / "tools")}),
                patch.object(qualify, "GUEST_ROOT", root),
                patch.object(qualify, "guest_guard"),
                patch.object(qualify.urllib.request, "urlopen") as download,
                patch.object(
                    qualify, "output", side_effect=lambda argv: versions[argv[0]]
                ),
                patch.object(qualify.sys, "stdout", new=io.StringIO()) as stdout,
            ):
                self.assertEqual(
                    qualify.main(
                        [
                            "runtime",
                            "--tools",
                            str(root / "tools" / qualify.CADDY_ARCHIVE_SHA256),
                        ]
                    ),
                    0,
                )
                download.assert_not_called()
                self.assertEqual(json.loads(stdout.getvalue()), binaries)
                versions[binaries["caddy"]["path"]] = "v2.11.3 fixture"
                self.assertEqual(
                    qualify.main(
                        [
                            "runtime",
                            "--tools",
                            str(root / "tools" / qualify.CADDY_ARCHIVE_SHA256),
                        ]
                    ),
                    1,
                )
                download.assert_not_called()

    def test_guest_caddy_provisioning_accepts_only_pinned_archive(self):
        data = b"fixture Caddy native bytes"
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            member = tarfile.TarInfo("caddy")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        for valid in (False, True):
            with self.subTest(valid=valid), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                qualification = root / "qualification"
                qualification.mkdir()
                node = root / "node"
                node.write_text("fixture Node native bytes")
                node.chmod(0o700)
                archive_path = root / "archive.tgz"
                archive_path.write_bytes(archive_bytes.getvalue())
                pin = qualify.sha256(archive_path) if valid else "0" * 64
                tools = qualification / "tools" / pin
                versions = {
                    str(node): "v26.10.0",
                    str(tools / "caddy"): "v2.11.4 fixture",
                }
                with (
                    patch.dict(qualify.os.environ, {"PATH": str(root)}),
                    patch.object(qualify, "GUEST_ROOT", qualification),
                    patch.object(qualify, "guest_guard"),
                    patch.object(qualify, "CADDY_ARCHIVE_SHA256", pin),
                    patch.object(
                        qualify.urllib.request,
                        "urlopen",
                        return_value=io.BytesIO(archive_bytes.getvalue()),
                    ) as download,
                    patch.object(
                        qualify, "output", side_effect=lambda argv: versions[argv[0]]
                    ),
                    patch.object(qualify.sys, "stdout", new=io.StringIO()) as stdout,
                ):
                    result = qualify.main(["runtime", "--tools", str(tools)])
                    self.assertEqual(result, 0 if valid else 1)
                    download.assert_called_once_with(qualify.CADDY_URL, timeout=120)
                    if valid:
                        self.assertEqual((tools / "caddy").read_bytes(), data)
                        observed = json.loads(stdout.getvalue())
                        self.assertEqual(
                            observed["caddy"]["path"], str(tools / "caddy")
                        )
                        self.assertEqual(
                            observed["caddy"]["sha256"], qualify.sha256(tools / "caddy")
                        )
                    else:
                        self.assertFalse((tools / "caddy").exists())


class BootstrapTest(unittest.TestCase):
    def test_prerequisite_checks_keep_socket_temporary_paths_short(self):
        with TemporaryDirectory() as directory:
            run = Path(directory) / ("qualification-" + "a" * 32)
            run.mkdir()
            observed = []

            def native(_binding, env, *_args):
                observed.append(env["TMPDIR"])
                raise RuntimeError("fixture native boundary")

            with patch.object(
                qualify, "prerequisite_native_checks", side_effect=native
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture native boundary"):
                    qualify.prerequisite_checks({}, run, run, {}, {}, "python")
            self.assertEqual(observed, ["/tmp"])

    def test_host_run_creates_persistent_parent_before_new_run_directory(self):
        import argparse
        import subprocess

        with TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "node_modules/@namespacelabs/sdk/package.json"
            sdk.parent.mkdir(parents=True)
            sdk.write_text("{}")
            package = root / "candidate.tgz"
            package.write_bytes(b"fixture")
            guest_root = root / "guest/qualification"
            calls = []
            args = argparse.Namespace(
                sdk_root=str(root),
                tgz=str(package),
                devbox_id="fixture-box",
                devbox_name="fixture-name",
                evidence=str(root / "evidence"),
                authorization_quote="fixture authorization",
                authorization_turn="fixture turn",
            )

            def output(argv, *_args, **_kwargs):
                if "exec" not in argv:
                    raise AssertionError("unexpected setup command")
                command = argv[argv.index("--") + 1 :]
                calls.append(command)
                if command[0] == "/bin/mkdir":
                    subprocess.run(command, check=True, capture_output=True)
                return "arm64"

            def complete(_args, _binding, work, *_rest):
                # Stop at the runtime upload, after the real directory dispatch.
                with self.assertRaisesRegex(AssertionError, "unexpected setup"):
                    work()
                return 0

            with (
                patch.object(qualify, "validate_host_qualification"),
                patch.object(qualify, "package_preflight"),
                patch.object(qualify, "host_stage", return_value={}),
                patch.object(qualify, "GUEST_ROOT", guest_root),
                patch.object(qualify.tempfile, "gettempdir", return_value=directory),
                patch.object(qualify, "output", side_effect=output),
                patch.object(
                    qualify,
                    "host_metadata",
                    return_value={
                        "id": "fixture-box",
                        "name": "fixture-name",
                        "state": "running",
                        "instance_id": "fixture-instance",
                    },
                ),
                patch.object(
                    qualify, "complete_host_qualification", side_effect=complete
                ),
            ):
                self.assertEqual(qualify.cmd_run(args), 0)
            self.assertEqual(
                calls[1], ["/bin/mkdir", "-p", "-m", "700", str(guest_root)]
            )
            self.assertEqual(calls[2][:3], ["/bin/mkdir", "-m", "700"])
            self.assertEqual(Path(calls[2][3]).parent, guest_root)
            self.assertEqual(guest_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(Path(calls[2][3]).stat().st_mode & 0o777, 0o700)

    def test_guest_dispatch_prepares_paired_fixture_and_single_live_baseline(self):
        import hashlib
        import uuid

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tarball = root / "candidate.tgz"
            manifest = {"protocol": 1, "persistenceFingerprint": "fixture-persistence"}
            contents = {
                "package.json": json.dumps({"version": "v1"}).encode(),
                "app/hotswap-manifest.json": json.dumps(manifest).encode(),
            }
            with tarfile.open(tarball, "w:gz") as archive:
                for name, data in contents.items():
                    member = tarfile.TarInfo("package/" + name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            tarball.chmod(0o600)
            runtime = {key: "b" * 64 for key in qualify.RUNTIME_KEYS}
            binding = {
                "protocol": 1,
                "sha256": qualify.sha256(tarball),
                "run_id": "fixture-" + uuid.uuid4().hex,
                "runtime_sha256": runtime,
                "source_commit": "c" * 40,
                "source_patch_sha256": "d" * 64,
                "namespace": {
                    "devbox_id": "fixture-box",
                    "instance_id": "fixture-instance",
                },
                "proxy_version": "v2.11.4 fixture",
                "guest_binaries": {
                    name: {
                        "path": "/guest/bin/" + name,
                        "sha256": runtime[name],
                        "version": "fixture-runtime",
                    }
                    for name in ("node", "caddy")
                },
            }
            run_root = root / "qualification" / binding["run_id"]
            run_root.mkdir(parents=True, mode=0o700)
            input_path = run_root / "input.json"
            write_json(input_path, binding)
            write_json(
                run_root / "execution.json",
                {"run_id": binding["run_id"], "execution_id": "exec_fixture"},
            )
            prepared = []
            original_prepare = qualify.prepare_scope
            module = qualify.controller_module()
            module.runtime_hashes = lambda: runtime

            def prepare(*args, **kwargs):
                result = original_prepare(*args, **kwargs)
                prepared.append(result)
                return result

            def prerequisites(*args):
                raise ValueError("fixture stops before executable checks")

            with (
                patch.object(qualify, "GUEST_ROOT", root / "qualification"),
                patch.object(qualify, "guest_guard"),
                patch.object(qualify, "source_binding"),
                patch.object(qualify, "runtime_preflight"),
                patch.object(
                    qualify,
                    "guest_binary_bindings",
                    return_value=binding["guest_binaries"],
                ),
                patch.object(qualify, "controller_module", return_value=module),
                patch.object(qualify, "prepare_scope", side_effect=prepare),
                patch.object(
                    qualify,
                    "provision_dependencies",
                    return_value=(
                        {"package-lock.json": "e" * 64},
                        "fixture-python",
                        "fixture dependency log",
                    ),
                ),
                patch.object(qualify, "output", return_value="fixture-runtime"),
                patch.object(qualify, "prerequisite_checks", side_effect=prerequisites),
            ):
                self.assertEqual(
                    qualify.main(["guest", str(tarball), "--input", str(input_path)]), 1
                )
                self.assertEqual(len(prepared), 1)
                scope_path, scope, _ = prepared[0]
                package = scope["packages"][binding["sha256"]]
                releases = Path(scope["home"]) / ".9router/releases"
                self.assertEqual(
                    package["installations"],
                    {
                        "a": str(releases / "v1/a/lib/node_modules/9router"),
                        "b": str(releases / "v1/b/lib/node_modules/9router"),
                    },
                )
                self.assertNotIn("release", package)
                self.assertFalse(releases.exists())
                self.assertEqual(
                    package["package_files"],
                    {
                        name: hashlib.sha256(data).hexdigest()
                        for name, data in contents.items()
                    },
                )
                self.assertEqual(
                    package["manifest_sha256"],
                    hashlib.sha256(contents["app/hotswap-manifest.json"]).hexdigest(),
                )
                self.assertEqual(
                    package["persistenceFingerprint"],
                    manifest["persistenceFingerprint"],
                )
                self.assertEqual(
                    qualify.sha256(Path(package["tarball"])), binding["sha256"]
                )
                self.assertEqual(qualify.private_json(scope_path), scope)
                self.assertNotIn("result", scope)

                # Preserve the persistent live baseline's
                # single-installation scope shape.
                baseline_path, baseline_scope, _ = original_prepare(
                    tarball, binding, run_id="baseline-" + binding["run_id"]
                )
                baseline_package = baseline_scope["packages"][binding["sha256"]]
                baseline_release = (
                    Path(baseline_scope["home"])
                    / ".9router/releases/v1/lib/node_modules/9router"
                )
                self.assertEqual(baseline_package["release"], str(baseline_release))
                self.assertNotIn("installations", baseline_package)
                self.assertEqual(
                    module.installed_package_hashes(baseline_release),
                    package["package_files"],
                )
                prepared_again, repeated_scope, _ = original_prepare(
                    tarball, binding, run_id=baseline_scope["run_id"]
                )
                self.assertEqual(prepared_again, baseline_path)
                self.assertEqual(repeated_scope, baseline_scope)
                (baseline_release / "package.json").write_bytes(
                    b"changed existing release"
                )
                with self.assertRaisesRegex(ValueError, "never overwrite"):
                    original_prepare(tarball, binding, run_id=baseline_scope["run_id"])
                self.assertEqual(
                    (baseline_release / "package.json").read_bytes(),
                    b"changed existing release",
                )

    def test_host_run_requires_current_acknowledgement_before_any_activation(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(qualify, "in_vm", return_value=False),
            patch.object(
                qualify.subprocess, "run", side_effect=AssertionError("no subprocess")
            ),
        ):
            self.assertEqual(
                qualify.main(
                    ["run", "candidate.tgz", "--evidence", directory + "/new"]
                ),
                1,
            )

    def test_guest_and_baseline_refuse_host_execution(self):
        with (
            patch.object(qualify, "in_vm", return_value=False),
            patch.object(
                qualify.subprocess, "run", side_effect=AssertionError("no subprocess")
            ),
        ):
            for command in ("guest", "baseline"):
                self.assertEqual(
                    qualify.main([command, "candidate.tgz", "--input", "input.json"]), 1
                )
            self.assertEqual(qualify.main(["runtime", "--tools", "/guest/tools"]), 1)

    def test_models_and_metacharacters_remain_separate_argv_values(self):
        model = 'cx/model; $(touch /tmp/not-a-command) "quoted"'
        with patch.object(qualify, "cmd_guest", return_value=0) as callback:
            self.assertEqual(
                qualify.main(
                    [
                        "guest",
                        "/guest/a package.tgz",
                        "--input",
                        "/guest/input.json",
                        "--model",
                        model,
                        "--model",
                        "cc/another",
                    ]
                ),
                0,
            )
        self.assertEqual(callback.call_args.args[0].models, [model, "cc/another"])
        self.assertEqual(
            qualify.controller_argv(
                Path("/guest/a scope.json"),
                "deploy-installed",
                "/guest/a release",
                "--digest",
                "a" * 64,
            )[-4:],
            ["deploy-installed", "/guest/a release", "--digest", "a" * 64],
        )

    def test_record_check_keeps_failed_populations_out_of_receipt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record, _ = fixture(root)
            identity = {key: record[key] for key in qualify.BINDINGS}
            with self.assertRaises(ValueError):
                qualify.record_check(root, identity, {}, "continuity", 0, {}, "log")
            self.assertFalse((root / "result.json").exists())

    def test_supplementary_unit_population_rejects_missing_modules_and_skips(self):
        with TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            value = {
                "success": True,
                "numTotalTests": 1,
                "numPendingTests": 0,
                "numFailedTests": 0,
                "numTodoTests": 0,
                "testResults": [
                    {
                        "name": "/guest/tests/unit/example.test.js",
                        "status": "passed",
                        "assertionResults": [{"status": "passed"}],
                    }
                ],
            }
            write_json(report, value)
            self.assertEqual(qualify.unit_population(report, ("example",), 1), 1)
            for mutate in (
                lambda v: v.update(numPendingTests=1),
                lambda v: v.update(testResults=[]),
                lambda v: v["testResults"][0]["assertionResults"][0].update(
                    status="pending"
                ),
            ):
                changed = copy.deepcopy(value)
                mutate(changed)
                write_json(report, changed)
                with self.assertRaises(ValueError):
                    qualify.unit_population(report, ("example",), 1)

    def test_isolated_environment_does_not_forward_credentials_or_module_injection(
        self,
    ):
        with (
            TemporaryDirectory() as directory,
            patch.dict(
                qualify.os.environ,
                {
                    "NINEROUTER_PROBE_KEY": "guest-fixture-not-live",
                    "JWT_SECRET": "not-forwarded",
                    "NODE_OPTIONS": "--require /outside.js",
                    "NODE_PATH": "/outside",
                    "HOME": "/outside",
                },
            ),
        ):
            env = qualify.isolated_environment(
                Path(directory) / "home", Path(directory) / "source"
            )
        self.assertFalse(
            {"NINEROUTER_PROBE_KEY", "JWT_SECRET", "NODE_OPTIONS", "NODE_PATH"}
            & env.keys()
        )
        self.assertNotEqual(env["HOME"], "/outside")

    def test_guest_child_failure_keeps_only_redacted_output(self):
        child = unittest.mock.Mock()
        child.pid = 12345
        child.returncode = 1
        child.communicate.return_value = (
            "authorization: sk-fixture-secret\nfailed assertion\n",
            None,
        )
        child.poll.return_value = 1
        with (
            patch.object(qualify.subprocess, "Popen", return_value=child),
            patch.object(qualify.os, "killpg", side_effect=ProcessLookupError),
            self.assertRaises(qualify.GuestCheckFailure) as failure,
        ):
            qualify.guest_command(["node", "fixture.mjs"], Path("/guest"), {}, 30)
        self.assertIn("failed assertion", str(failure.exception))
        self.assertNotIn("sk-fixture-secret", str(failure.exception))

    def test_archive_links_and_legacy_manifest_are_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name, protocol, link in (("legacy", 0, False), ("linked", 1, True)):
                tarball = root / (name + ".tgz")
                with tarfile.open(tarball, "w:gz") as archive:
                    data = json.dumps({"protocol": protocol}).encode()
                    member = tarfile.TarInfo("package/app/hotswap-manifest.json")
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                    if link:
                        member = tarfile.TarInfo("package/escape")
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/outside"
                        archive.addfile(member)
                with self.assertRaises(ValueError):
                    qualify.package_preflight(tarball)


class RuntimeProvisioningTest(unittest.TestCase):
    def exercise(self, root, archive, *, corrupt=False, interrupted=False):
        import hashlib

        pin = hashlib.sha256(archive).hexdigest()
        tools = root / "tools" / pin
        with (
            patch.object(qualify, "GUEST_ROOT", root),
            patch.object(qualify, "CADDY_ARCHIVE_SHA256", pin),
            patch.object(qualify, "guest_guard"),
            patch.object(qualify.shutil, "which", return_value=None),
            patch.object(
                qualify.urllib.request,
                "urlopen",
                side_effect=lambda *args, **kwargs: io.BytesIO(archive),
            ) as download,
            patch.object(
                qualify,
                "guest_binary_bindings",
                side_effect=lambda: {
                    "caddy": {
                        "path": str(tools / "caddy"),
                        "sha256": qualify.sha256(tools / "caddy"),
                    }
                },
            ),
            patch.dict(qualify.os.environ, {"PATH": "/fixture/bin"}),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            if interrupted:
                with patch.object(
                    qualify.os,
                    "replace",
                    side_effect=OSError("fixture publish failure"),
                ):
                    with self.assertRaises(OSError):
                        qualify.cmd_runtime(unittest.mock.Mock(tools=str(tools)))
                self.assertFalse((tools / "caddy.tgz").exists())
            self.assertEqual(
                qualify.cmd_runtime(unittest.mock.Mock(tools=str(tools))), 0
            )
            original = (tools / "caddy").stat()
            if corrupt:
                (tools / "caddy").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "binary differs"):
                    qualify.cmd_runtime(unittest.mock.Mock(tools=str(tools)))
                self.assertEqual((tools / "caddy").read_bytes(), b"changed")
            else:
                self.assertEqual(
                    qualify.cmd_runtime(unittest.mock.Mock(tools=str(tools))), 0
                )
                self.assertEqual(
                    [json.loads(line) for line in output.getvalue().splitlines()][0],
                    [json.loads(line) for line in output.getvalue().splitlines()][1],
                )
                self.assertEqual((tools / "caddy").stat().st_ino, original.st_ino)
                self.assertEqual(
                    (tools / "caddy").stat().st_mtime_ns, original.st_mtime_ns
                )
            self.assertEqual(download.call_count, 2 if interrupted else 1)

    def test_repeated_runs_reuse_the_same_hash_bound_binary_and_path(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as package:
            member = tarfile.TarInfo("caddy")
            member.size = len(b"fixture executable")
            package.addfile(member, io.BytesIO(b"fixture executable"))
        for corrupt, interrupted in ((False, False), (True, False), (False, True)):
            with (
                self.subTest(corrupt=corrupt, interrupted=interrupted),
                TemporaryDirectory() as directory,
            ):
                self.exercise(
                    Path(directory).resolve(),
                    archive.getvalue(),
                    corrupt=corrupt,
                    interrupted=interrupted,
                )

    def test_run_specific_tool_paths_refuse_before_download(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(qualify, "GUEST_ROOT", Path(directory)),
            patch.object(qualify, "guest_guard"),
            patch.object(
                qualify.urllib.request,
                "urlopen",
                side_effect=AssertionError("must not download"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "persistent"):
                qualify.cmd_runtime(unittest.mock.Mock(tools=directory + "/run/tools"))


class ConcurrentApiTest(unittest.TestCase):
    def exercise(self, *, failure=None, latency=0.1, running=True):
        futures = [unittest.mock.Mock() for _ in range(4)]
        for future in futures:
            future.done.return_value = False
            future.running.return_value = running
            future.result.return_value = 1
        pool = unittest.mock.Mock()
        pool.submit.side_effect = futures
        executor = unittest.mock.MagicMock()
        executor.__enter__.return_value = pool
        observations = []

        def poll(url, timeout):
            self.assertTrue(any(future.running() for future in futures) or not running)
            self.assertEqual(url, "http://fixture/api/version")
            self.assertEqual(timeout, qualify.STALL_S)
            observations.append(url)
            if failure == "timeout":
                raise TimeoutError("fixture API stalled")
            response = io.BytesIO(
                json.dumps(
                    {"currentVersion": "wrong" if failure == "version" else "v1"}
                ).encode()
            )
            response.status = 200
            for future in futures:
                future.done.return_value = True
            return response

        with (
            patch.object(
                qualify.concurrent.futures, "ThreadPoolExecutor", return_value=executor
            ),
            patch.object(qualify.urllib.request, "urlopen", side_effect=poll),
            patch.object(qualify.time, "monotonic", side_effect=[10, 10 + latency]),
            patch.object(qualify.time, "sleep"),
        ):
            if failure or latency > qualify.STALL_S or not running:
                with self.assertRaises((ValueError, TimeoutError)):
                    qualify.check_concurrent(
                        lambda model: 1, ["cx/fixture"], "http://fixture", "v1"
                    )
                return
            measured = qualify.check_concurrent(
                lambda model: 1, ["cx/fixture"], "http://fixture", "v1"
            )
        self.assertEqual(pool.submit.call_count, 4)
        self.assertEqual(measured["concurrent_streams_completed"], 4)
        self.assertEqual(measured["concurrent_api_polls"], 1)
        self.assertAlmostEqual(measured["concurrent_api_max_latency_s"], latency)
        self.assertEqual(len(observations), 1)

    def test_api_is_polled_during_streams_and_latency_is_measured(self):
        self.exercise()

    def test_stalled_wrong_version_and_zero_population_api_refuse(self):
        self.exercise(failure="timeout")
        self.exercise(failure="version")
        self.exercise(latency=qualify.STALL_S + 0.1)
        self.exercise(running=False)


class NativeReviewIdentityTest(unittest.TestCase):
    def test_existing_review_counterfactual_terminal_is_accepted_but_wrong_terminal_refuses(self):
        entry = next(item for item in qualify.NATIVE if item[0] == "review-counterfactual")
        terminal = "GREEN: unchanged mirror passes; RED: baseline mirror fails all eight named review mechanisms while pending/uncertain safety passes"
        for emitted, accepted in ((terminal, True), ("PASS: unrelated check", False)):
            with (
                self.subTest(accepted=accepted),
                patch.object(qualify, "NATIVE", (entry,)),
                patch.object(qualify, "source_binding"),
                patch.object(qualify, "guest_command", return_value=emitted),
                patch.object(qualify, "record_check") as record,
            ):
                if accepted:
                    qualify.prerequisite_native_checks({}, {}, Path("guest-evidence"), {}, {})
                    self.assertEqual(record.call_count, 1)
                else:
                    with self.assertRaisesRegex(ValueError, "identity absent"):
                        qualify.prerequisite_native_checks({}, {}, Path("guest-evidence"), {}, {})
                    record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
