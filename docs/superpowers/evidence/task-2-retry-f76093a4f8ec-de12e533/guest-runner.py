import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

root = pathlib.Path(sys.argv[1])
m = json.loads((root / "input.json").read_text())
out = root / "evidence"
out.mkdir(mode=0o700)
active = None
results = []
aborted = False


def digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1048576), b""):
            h.update(b)
    return h.hexdigest()


def clean(text):
    secret = root / "scanner-credential"
    if secret.exists():
        text = text.replace(secret.read_text(), "[REDACTED]")
    text = re.sub(
        (
            "(?i)(authorization\\s*[:=]\\s*(?:bearer\\s+"
            ")?|(?:access_token|refresh_token|api[_-]"
            "?key|password|client_secret|session_secr"
            'et)\\s*["\\x27]?\\s*[:=]\\s*["\\x27]?)[^\\s,"\\'
            "x27}]+"
        ),
        r"\1[REDACTED]",
        text,
    )
    return re.sub(
        r"\b(?:nsct_|ghp_|github_pat_|sk-)[A-Za-z0-9_.-]+", "[REDACTED]", text
    )


def stop_group(p):
    if p is None:
        return
    if p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        print(
            json.dumps({"phase": "group-signal-denied", "ownedPid": p.pid}), flush=True
        )
        p.terminate()
    time.sleep(1)
    if p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The group already exited; still wait below to reap the owned child.
            pass
        except PermissionError:
            p.kill()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise RuntimeError("owned process did not exit")
    # ponytail: guest may deny group signaling; compute shutdown contains reparented
    # descendants.


def interrupt(sig, frame):
    global aborted
    aborted = True
    raise KeyboardInterrupt("interrupted")


for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
    signal.signal(sig, interrupt)
signal.alarm(7200)


def env_for(name, checkout):
    d = root / "i" / name
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    for n in ("h", "t", "d", "c"):
        (d / n).mkdir(exist_ok=True, mode=0o700)
    # No guest provider secrets or real HOME/database enter subprocess environments.
    return {
        "PATH": str(root / "tools")
        + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(d / "h"),
        "TMPDIR": str(d / "t"),
        "DATA_DIR": str(d / "d"),
        "XDG_CONFIG_HOME": str(d / "h/config"),
        "XDG_CACHE_HOME": str(d / "c"),
        "NINEROUTER_TEST_PACKAGES": str(checkout),
        "CI": "true",
        "NODE_ENV": "production",
        "PYTHONDONTWRITEBYTECODE": "1",
        "npm_config_cache": str(root / "npm-cache"),
        "npm_config_audit": "false",
        "npm_config_fund": "false",
    }


def run(name, argv, cwd, limit, population, check=None):
    global active
    begin = time.monotonic()
    status = None
    timed = False
    print(
        json.dumps({"phase": "check-start", "name": name, "limitSeconds": limit}),
        flush=True,
    )
    raw = root / (name + ".raw")
    try:
        with raw.open("wb") as f:
            environment = env_for(name, cwd)
            if name.startswith("h2c-"):
                environment.update(
                    NINEROUTER_SKIP_BACKGROUND_REFRESH="1",
                    NINEROUTER_SKIP_RESPONSES_WS="1",
                )
            if name == "s3776-self-check":
                environment["PATH"] = (
                    str(root / "lint-venv/bin") + ":" + environment["PATH"]
                )
            if name == "sonar-secrets":
                environment.update(
                    SONARQUBE_CLI_TOKEN=(root / "scanner-credential").read_text(),
                    SONARQUBE_CLI_ORG="lieberman-family-office",
                )
            active = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + limit
            while status is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed = True
                    break
                try:
                    status = active.wait(timeout=min(15, remaining))
                except subprocess.TimeoutExpired:
                    print(
                        json.dumps(
                            {
                                "phase": "check-running",
                                "name": name,
                                "elapsedSeconds": round(time.monotonic() - begin),
                            }
                        ),
                        flush=True,
                    )
    finally:
        cleanup = None
        try:
            stop_group(active)
        except Exception as e:
            cleanup = {"type": type(e).__name__, "reason": clean(str(e))}
        active = None
        text = clean(raw.read_text(errors="replace")) if raw.exists() else "NOT RUN"
        (out / (name + ".log")).write_text(text)
        raw.unlink(missing_ok=True)
        entry = {
            "name": name,
            "argv": argv,
            "revision": m["baseline"]
            if pathlib.Path(cwd).name == "baseline"
            else m["head"],
            "exitCode": status,
            "timeout": timed,
            "cleanupFailure": cleanup,
            "seconds": round(time.monotonic() - begin, 3),
            "population": population,
        }
        results.append(entry)
        (out / "checks.json").write_text(json.dumps(results, indent=2))
    valid = True
    if check:
        try:
            entry.update(check(text))
        except Exception as e:
            valid = False
            entry["validationFailure"] = {
                "type": type(e).__name__,
                "reason": clean(str(e)),
            }
        (out / "checks.json").write_text(json.dumps(results, indent=2))
    print(
        json.dumps(
            {
                "phase": "check-end",
                "name": name,
                "exitCode": status,
                "timeout": timed,
                "validated": valid,
            }
        ),
        flush=True,
    )
    return status == 0 and not timed and valid and cleanup is None


def command(argv, cwd=None):
    return subprocess.check_output(
        argv, cwd=cwd, env=env_for("metadata", root), text=True, timeout=30
    ).strip()


def unit_summary(p, minimum, files):
    def validate(text):
        j = json.loads(p.read_text())
        assert len(j["testResults"]) == files and j["numTotalTests"] >= minimum, (
            "zero/incomplete unit population"
        )
        assert (
            j["numPendingTests"] == 0
            and j["numFailedTests"] == 0
            and j["success"] is True
        ), "failed/skipped unit tests"
        assert all(x["status"] == "passed" for x in j["testResults"]), (
            "unrun unit module"
        )
        return {
            "testCount": j["numTotalTests"],
            "fileCount": len(j["testResults"]),
            "reportSha256": digest(p),
        }

    return validate


success = False
try:
    assert command(["uname", "-s"]) == "Darwin" and command(["uname", "-m"]) == "arm64"
    archive = root / "caddy.tar.gz"
    assert run(
        "caddy-download",
        [
            "/usr/bin/curl",
            "--fail",
            "--location",
            "--retry",
            "2",
            "--max-time",
            "120",
            "--output",
            str(archive),
            (
                "https://github.com/caddyserver/caddy/rel"
                "eases/download/v2.11.4/caddy_2.11.4_mac_"
                "arm64.tar.gz"
            ),
        ],
        root,
        150,
        {"unit": "pinned guest runtime downloads", "count": 1},
    )
    assert digest(archive) == m["caddyArchiveSha256"], (
        "official Caddy archive hash mismatch"
    )
    assert run(
        "caddy-extract",
        ["/usr/bin/tar", "-xzf", str(archive), "-C", str(root / "tools"), "caddy"],
        root,
        30,
        {"unit": "verified runtime extractions", "count": 1},
    )
    os.chmod(root / "tools/caddy", 0o700)
    versions = {
        "node": command(["node", "--version"]),
        "npm": command(["npm", "--version"]),
        "caddy": command(["caddy", "version"]).split()[0].lstrip("v"),
        "python": command(["python3", "--version"]).split()[1],
        "macos": command(["sw_vers", "-productVersion"]),
    }
    (out / "runtime.json").write_text(json.dumps({"versions": versions}, indent=2))
    assert all(versions[k] == v for k, v in m["runtime"].items() if v != "observe"), (
        "runtime version mismatch"
    )
    m["measuredRuntime"] = versions
    runtime = {"versions": versions, "binaries": {}}
    for name in ("node", "caddy", "python3", "git"):
        executable = command(["/usr/bin/which", name])
        runtime["binaries"][name] = {
            "path": executable,
            "sha256": digest(pathlib.Path(executable).resolve()),
        }
    (out / "runtime.json").write_text(json.dumps(runtime, indent=2))
    assert digest(root / "source.bundle") == m["bundleSha256"], "bundle hash mismatch"
    assert digest(root / "runner.py") == m["runnerSha256"], "runner hash mismatch"
    for name, rev in [("head", m["head"]), ("baseline", m["baseline"])]:
        dest = root / name
        assert run(
            "clone-" + name,
            ["git", "clone", "--no-checkout", str(root / "source.bundle"), str(dest)],
            root,
            120,
            {"unit": "checkout", "count": 1},
        )
        assert run(
            "checkout-" + name,
            ["git", "checkout", "--detach", rev],
            dest,
            30,
            {"unit": "revision", "count": 1},
        )
        assert command(["git", "rev-parse", "HEAD"], dest) == rev
        command(["git", "bundle", "verify", str(root / "source.bundle")], dest)
        if name == "baseline":
            # Explicit toolchain overlay: baseline source stays pinned; test deps use
            # reviewed HEAD manifests.
            (dest / "tests/package.json").write_bytes(
                (root / "head/tests/package.json").read_bytes()
            )
        (dest / "tests/package-lock.json").write_bytes(
            (root / "tests-package-lock.json").read_bytes()
        )
        assert (
            json.loads((dest / "tests/package.json").read_text())["devDependencies"]
            == json.loads((dest / "tests/package-lock.json").read_text())["packages"][
                ""
            ]["devDependencies"]
        ), "test lock/manifest mismatch"
        for lock in ("package-lock.json", "tests/package-lock.json"):
            expected = m["lockHashes"][name][lock]
            assert digest(dest / lock) == expected, "lockfile identity mismatch"
        # Locked package graph only. No npx download or host dependencies; no install
        # lifecycle scripts.
        for suffix, location in [("app", dest), ("tests", dest / "tests")]:
            assert run(
                "dependencies-" + name + "-" + suffix,
                [
                    "npm",
                    "ci",
                    "--ignore-scripts",
                    "--include=dev",
                    "--include=optional",
                    "--no-audit",
                    "--no-fund",
                ],
                location,
                900,
                {"unit": "lockfile installs", "count": 1},
            )
        assert (dest / "tests/node_modules/vitest/vitest.mjs").is_file(), (
            "locked Vitest missing"
        )
    head = root / "head"
    baseline = root / "baseline"
    for filename, expected in m["runtimeFiles"].items():
        assert digest(head / filename) == expected, "source runtime hash mismatch"
    (out / "dependencies.json").write_text(
        json.dumps(
            {
                "head": command(["npm", "ls", "--all", "--json"], head),
                "tests": command(["npm", "ls", "--all", "--json"], head / "tests"),
                "runtimeFiles": m["runtimeFiles"],
            },
            indent=2,
        )
    )
    for filename, expected in m["patchFiles"].items():
        incoming = root / pathlib.Path(filename).name
        assert digest(incoming) == expected, "enumerated patch hash mismatch"
        target = head / filename
        if target.exists():
            assert digest(target) == expected, "patch would overwrite unreviewed source"
        target.write_bytes(incoming.read_bytes())
    cfg = root / "targeted.config.mjs"
    cfg.write_text(
        (
            'import path from "node:path"; import {cr'
            'eateRequire} from "node:module"; const r'
            "oot="
        )
        + json.dumps(str(head))
        + (
            "; const req=createRequire(path.join(root"
            ',"package.json")); export default {root,'
            'test:{environment:"node",globals:true},r'
            'esolve:{alias:[{find:"open-sse",replacem'
            'ent:path.join(root,"open-sse")},{find:"@'
            '",replacement:path.join(root,"src")},{fi'
            'nd:"vitest",replacement:path.join(root,"'
            'tests/node_modules/vitest/dist/index.js"'
            ')},...["uuid","sql.js","undici","bcryptj'
            's","node-machine-id"].map(name=>({find:n'
            "ame,replacement:req.resolve(name)}))]}};"
        )
    )
    (out / "alias-config.mjs").write_text(cfg.read_text())
    checks = [
        ("worker", ["node", "tests/mac/9router_worker.check.cjs"], 600),
        ("proxy", ["node", "tests/mac/9router_hotswap.check.cjs"], 180),
        (
            "proxy-counterfactual",
            ["node", "tests/mac/9router_hotswap_counterfactual.check.cjs"],
            300,
        ),
        (
            "cleanup",
            [
                "node",
                "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON",
                "tests/unit/managed-cleanup.check.mjs",
            ],
            120,
        ),
        ("templates", ["python3", "tests/mac/test_9router_worker_templates.py"], 60),
        ("dispatch", ["node", "tests/unit/managed-dispatch.check.mjs"], 120),
        ("state", ["node", "tests/unit/managed-state.check.mjs"], 180),
        (
            "refresh-cross-process",
            ["node", "tests/unit/token-refresh-cross-process.check.mjs"],
            180,
        ),
        ("cas-process", ["node", "tests/unit/managed-cas-process.check.mjs"], 180),
    ]
    all_ok = True
    markers = {
        "worker": ["PASS: real pinned workers"],
        "proxy": ["PASS"],
        "proxy-counterfactual": [
            "GREEN: unchanged byte-identical mirror",
            "RED: removing only",
            "PASS: routing counterfactual",
        ],
        "dispatch": ["GREEN: AST/import-aware census"],
    }

    def native_summary(name):
        def validate(text):
            assert text.strip(), "empty native proof log"
            if name != "h2c-head":
                assert not re.search(r"(?i)\b(?:skipped|todo)\b", text), (
                    "skipped native proof"
                )
            for marker in markers.get(name, []):
                assert marker in text, "missing native assertion identity"
            if name == "h2c-head":
                assert (
                    re.search(r"^# tests 5$", text, re.M)
                    and re.search(r"^# pass 5$", text, re.M)
                    and re.search(r"^# fail 0$", text, re.M)
                    and re.search(r"^# cancelled 0$", text, re.M)
                    and re.search(r"^# skipped 0$", text, re.M)
                    and re.search(r"^# todo 0$", text, re.M)
                ), "incomplete h2c population"
            return {
                "logSha256": digest(out / (name + ".log")),
                "requiredMarkers": markers.get(name, []),
            }

        return validate

    for name, argv, limit in checks if m["testScope"] == "task2" else []:
        all_ok = (
            run(
                name,
                argv,
                head,
                limit,
                {"unit": "named native proof commands", "count": 1},
                native_summary(name),
            )
            and all_ok
        )
    batches = [
        (
            "task2-unit",
            66,
            [
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
            ],
        ),
    ]
    for name, minimum, modules in batches if m["testScope"] == "task2" else []:
        report = out / (name + ".json")
        argv = [
            "node",
            "tests/node_modules/vitest/vitest.mjs",
            "run",
            "--config",
            str(cfg),
            *[f"tests/unit/{x}.test.js" for x in modules],
            "--reporter=json",
            "--outputFile=" + str(report),
        ]
        all_ok = (
            run(
                name,
                argv,
                head,
                300,
                {"unit": "selected unit modules", "count": len(modules)},
                unit_summary(report, minimum, len(modules)),
            )
            and all_ok
        )
    syntax = [
        "scripts/mac/9router_worker.cjs",
        "custom-server.js",
        "src/lib/db/managed.cjs",
        "src/lib/db/repos/settingsRepo.js",
        "src/shared/services/initializeApp.js",
        "tests/mac/9router_worker.check.cjs",
        "tests/mac/9router_hotswap_counterfactual.check.cjs",
        "tests/unit/managed-worker.test.js",
        "tests/unit/managed-credentials.test.js",
    ]
    for i, f in enumerate(syntax if m["testScope"] == "task2" else []):
        all_ok = (
            run(
                "syntax-" + str(i),
                ["node", "--check", f],
                head,
                30,
                {"unit": "syntax files", "count": 1},
            )
            and all_ok
        )
    for name, checkout in (
        [("baseline", baseline), ("head", head)]
        if m["testScope"] == "task2"
        else [("head", head)]
    ):
        if name == "baseline":
            observed = run(
                "h2c-baseline",
                [
                    "node",
                    "--test",
                    "--test-reporter=tap",
                    "--test-timeout=10000",
                    "tests/unit/custom-server-h2c.test.cjs",
                ],
                checkout,
                30,
                {"unit": "original baseline h2c failing reproduction", "count": 1},
            )
            assert (
                not observed
                and "test timed out" in (out / "h2c-baseline.log").read_text()
            ), "baseline did not reproduce the known failure"
        else:
            all_ok = (
                run(
                    "h2c-head",
                    [
                        "node",
                        "--test",
                        "--test-reporter=tap",
                        "--test-timeout=10000",
                        "tests/unit/custom-server-h2c.test.cjs",
                    ],
                    checkout,
                    30,
                    {"unit": "h2c framing and upgrade regression cases", "count": 5},
                    native_summary("h2c-head"),
                )
                and all_ok
            )
        if (
            name == "baseline"
        ):  # Preserve existing boundary traces for the failing baseline.
            original = (checkout / "tests/unit/custom-server-h2c.test.cjs").read_text()
            diagnostic = (
                original.replace(
                    "const body = [];",
                    (
                        'console.error("TRACE handler-enter"); re'
                        's.on("finish",()=>console.error("TRACE r'
                        'esponse-finish")); const body = [];'
                    ),
                )
                .replace(
                    "for await (const chunk of req) body.push(chunk);",
                    (
                        "for await (const chunk of req) { console"
                        '.error("TRACE body-chunk",chunk.length);'
                        ' body.push(chunk); } console.error("TRAC'
                        'E body-complete");'
                    ),
                )
                .replace(
                    "const port = server.address().port;",
                    (
                        'console.error("TRACE listening"); const '
                        "emit=server.emit;server.emit=function(ev"
                        'ent,...a){if(event==="upgrade"||event==='
                        '"request")console.error("TRACE server-ev'
                        'ent",event,a[0]?.headers,a[2]?.length);r'
                        "eturn emit.call(this,event,...a);}; serv"
                        'er.on("connection",s=>{console.error("TR'
                        'ACE accepted");s.on("close",()=>console.'
                        'error("TRACE server-socket-close"));}); '
                        "const port = server.address().port;"
                    ),
                )
                .replace(
                    'const body = \'{"model":"test","stream":true}\';',
                    (
                        'console.error("TRACE client-connected");'
                        ' const body = \'{"model":"test","stream":'
                        "true}';"
                    ),
                )
                .replace(
                    "setImmediate(() => socket.write(body));",
                    (
                        'setImmediate(() => { console.error("TRAC'
                        'E client-body-write"); socket.write(body'
                        "); });"
                    ),
                )
                .replace(
                    'socket.on("end", () => resolve',
                    (
                        'socket.on("end", () => {console.error("T'
                        'RACE client-end");}); socket.on("end", ('
                        ") => resolve"
                    ),
                )
                .replace(
                    "await new Promise((resolve) => server.close(resolve));",
                    (
                        'console.error("TRACE server-close-start"'
                        "); await new Promise((resolve) => server"
                        '.close(resolve)); console.error("TRACE s'
                        'erver-close-end");'
                    ),
                )
                .replace(
                    "socket.setTimeout(2_000, () => {",
                    (
                        'socket.on("error",e=>console.error("TRAC'
                        'E client-error",e.code,e.message)); sock'
                        'et.on("data",b=>console.error("TRACE cli'
                        'ent-data",b.toString())); socket.setTime'
                        'out(2_000, () => { console.error("TRACE '
                        'client-timeout");'
                    ),
                )
                .replace(
                    "const [req, socket, head] = eventArgs;",
                    "const [req, socket, head] = eventArgs;",
                )
            )
            p = checkout / "tests/unit/h2c-diagnostic.cjs"
            p.write_text(diagnostic)
            try:
                run(
                    "h2c-diagnostic-" + name,
                    [
                        "node",
                        "--test",
                        "--test-reporter=tap",
                        "--test-timeout=10000",
                        str(p),
                    ],
                    checkout,
                    30,
                    {"unit": "instrumented test mirrors; not acceptance", "count": 1},
                )
            finally:
                p.unlink()
            p.write_text(
                diagnostic.replace(
                    (
                        'setImmediate(() => { console.error("TRAC'
                        'E client-body-write"); socket.write(body'
                        "); });"
                    ),
                    (
                        'console.error("TRACE client-body-write-s'
                        'ync"); socket.write(body);'
                    ),
                )
            )
            try:
                run(
                    "h2c-coalesced-" + name,
                    [
                        "node",
                        "--test",
                        "--test-reporter=tap",
                        "--test-timeout=10000",
                        str(p),
                    ],
                    checkout,
                    30,
                    {
                        "unit": "coalesced-body counterfactual; not acceptance",
                        "count": 1,
                    },
                )
            finally:
                p.unlink()
            assert (
                checkout / "tests/unit/custom-server-h2c.test.cjs"
            ).read_text() == original
    mirror = root / "h2c-counterfactual"
    (mirror / "tests/unit").mkdir(parents=True)
    (mirror / "custom-server.js").write_text(
        command(
            [
                "git",
                "show",
                m["h2cCounterfactualBaseline"] + ":custom-server.js",
            ],
            head,
        )
        + "\n"
    )
    (mirror / "tests/unit/custom-server-h2c.test.cjs").write_bytes(
        (head / "tests/unit/custom-server-h2c.test.cjs").read_bytes()
    )
    red_ok = run(
        "h2c-counterfactual",
        [
            "node",
            "--test",
            "--test-reporter=tap",
            "--test-timeout=10000",
            "tests/unit/custom-server-h2c.test.cjs",
        ],
        mirror,
        30,
        {
            "unit": "new regression suite on pre-fix server mirror; expected failure",
            "count": 5,
        },
    )
    assert (
        not red_ok
        and "h2c fallback response timed out"
        in (out / "h2c-counterfactual.log").read_text()
    ), "regression did not detect pre-fix failure"
    managed_tap = run(
        "h2c-managed",
        [
            "node",
            "--test",
            "--test-reporter=tap",
            "--test-timeout=10000",
            "tests/unit/custom-server-h2c-managed.test.cjs",
        ],
        head,
        30,
        {"unit": "managed h2c HTTP and IPC drain cases", "count": 1},
    )
    managed_log = (out / "h2c-managed.log").read_text()
    all_ok = (
        managed_tap
        and all(
            re.search(r"^# " + k + " " + str(v) + r"$", managed_log, re.M)
            for k, v in [
                ("tests", 1),
                ("pass", 1),
                ("fail", 0),
                ("cancelled", 0),
                ("skipped", 0),
                ("todo", 0),
            ]
        )
        and all_ok
    )
    assert run(
        "lint-venv",
        ["python3", "-m", "venv", str(root / "lint-venv")],
        root,
        60,
        {"unit": "isolated lint environments", "count": 1},
    )
    lint_python = str(root / "lint-venv/bin/python")
    assert run(
        "ruff-install",
        [
            lint_python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "ruff==0.15.18",
        ],
        root,
        180,
        {"unit": "pinned lint tools", "count": 1},
    )
    changed = command(
        ["git", "diff", "--name-only", "--diff-filter=ACM", m["scanBase"], "HEAD"], head
    ).splitlines()
    python_files = [
        p for p in changed if p.endswith(".py") and not p.startswith("docs/")
    ]
    assert python_files, "zero changed Python files"
    all_ok = (
        run(
            "ruff-check",
            [lint_python, "-m", "ruff", "check", "--select=E,F,I", *python_files],
            head,
            60,
            {"unit": "changed Python source files", "count": len(python_files)},
        )
        and all_ok
    )
    all_ok = (
        run(
            "ruff-format",
            [lint_python, "-m", "ruff", "format", "--check", *python_files],
            head,
            60,
            {"unit": "changed Python source files", "count": len(python_files)},
        )
        and all_ok
    )
    checker = root / "sonar_pr_issues.py"
    owner = root / "gh_owner.py"
    assert (
        digest(checker) == m["selfCheckSha256"]
        and digest(owner) == m["ownerHelperSha256"]
    )
    selfcheck_code = (
        "import sys; sys.path.insert(0,"
        + repr(str(root))
        + (
            "); import sonar_pr_issues as s; raise Sy"
            "stemExit(s.self_check(sys.argv[2:],base="
            "sys.argv[1]))"
        )
    )
    all_ok = (
        run(
            "s3776-self-check",
            [lint_python, "-c", selfcheck_code, m["scanBase"], *python_files],
            head,
            60,
            {"unit": "changed Python source files", "count": len(python_files)},
        )
        and all_ok
    )
    js_files = [
        p
        for p in changed
        if p.endswith((".js", ".cjs", ".mjs")) and not p.startswith("docs/")
    ] + [p for p in m["patchFiles"] if p.endswith((".js", ".cjs", ".mjs"))]
    all_ok = (
        run(
            "eslint",
            [
                "node",
                "node_modules/eslint/bin/eslint.js",
                "--no-warn-ignored",
                *js_files,
            ],
            head,
            120,
            {
                "unit": "changed JavaScript source and test files",
                "count": len(js_files),
            },
        )
        and all_ok
    )
    scanner = root / "tools/sonar"
    assert run(
        "sonar-download",
        [
            "/usr/bin/curl",
            "--fail",
            "--location",
            "--max-time",
            "120",
            "--output",
            str(scanner),
            (
                "https://binaries.sonarsource.com/Distrib"
                "ution/sonarqube-cli/1.9.0.15656/macos/so"
                "narqube-cli-1.9.0.15656-macos-arm64.bin"
            ),
        ],
        root,
        150,
        {"unit": "pinned scanner downloads", "count": 1},
    )
    assert (
        digest(scanner)
        == "8de8ec62c3614a9abb7053114fda85b9460b6ebdbca7bb612c6e58dc88ab2015"
    )
    scanner.chmod(0o700)
    all_ok = (
        run(
            "sonar-version",
            [str(scanner), "--version"],
            head,
            60,
            {"unit": "pinned scanner versions", "count": 1},
        )
        and all_ok
    )

    def secrets_summary(text):
        assert text.strip() and not re.search(
            r"(?i)\b(?:skipped|not entitled|unauthorized|forbidden)\b", text
        ), "secrets leg did not run"
        return {
            "logSha256": digest(out / "sonar-secrets.log"),
            "leg": "secrets only; not code analysis",
        }

    all_ok = (
        run(
            "sonar-secrets",
            [str(scanner), "analyze", "secrets", *changed, *m["patchFiles"]],
            head,
            600,
            {
                "unit": "changed branch files and enumerated test patches",
                "count": len(changed) + len(m["patchFiles"]),
            },
            secrets_summary,
        )
        and all_ok
    )
    for filename, expected in m["runtimeFiles"].items():
        assert digest(head / filename) == expected, (
            "source runtime changed during checks"
        )
    assert set(
        command(["git", "diff", "--name-only", "HEAD"], head).splitlines()
    ) <= set(m["patchFiles"]), "tracked guest source exceeds enumerated patch"
    all_ok = (
        run(
            "templates-patched",
            ["python3", "tests/mac/test_9router_worker_templates.py"],
            head,
            60,
            {"unit": "patched template assertion commands", "count": 1},
            native_summary("templates-patched"),
        )
        and all_ok
    )
    success = all_ok
except (KeyboardInterrupt, SystemExit) as e:
    (out / "failure.json").write_text(
        json.dumps({"type": type(e).__name__, "reason": clean(str(e))})
    )
    raise
except Exception as e:
    (out / "failure.json").write_text(
        json.dumps({"type": type(e).__name__, "reason": clean(str(e))})
    )
finally:
    signal.alarm(0)
    (root / "scanner-credential").unlink(missing_ok=True)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
        signal.signal(sig, signal.SIG_IGN)
    try:
        stop_group(active)
    except Exception as e:
        success = False
        (out / "cleanup-failure.json").write_text(
            json.dumps({"type": type(e).__name__, "reason": clean(str(e))})
        )
    for p in out.iterdir():
        if p.is_file():
            p.write_text(clean(p.read_text(errors="replace")))
    for filename, expected in m["patchFiles"].items():
        if (
            "head" not in globals()
            or not (head / filename).is_file()
            or digest(head / filename) != expected
        ):
            success = False
    identity = {
        "head": m["head"],
        "patchFiles": m["patchFiles"],
        "baseline": m["baseline"],
        "counterfactualBaseline": m["counterfactualBaseline"],
        "bundleSha256": m["bundleSha256"],
        "runnerSha256": m["runnerSha256"],
        "lockHashes": m["lockHashes"],
        "authorization": m["authorization"],
        "devboxId": m["devboxId"],
        "instanceId": m["instanceId"],
        "success": success,
        "interrupted": aborted,
        "scope": (
            "Task 2 deterministic source checks and r"
            "outing prerequisites only; NOT packaging"
            "/launchd/production qualification"
        ),
        "unrun": [
            "Sonar code analysis",
            "build/package",
            "native Caddy-to-real-worker",
            "launchd",
            "Task 4/5 lifecycle fault-injection proofs",
        ],
    }
    (out / "identity.json").write_text(json.dumps(identity, indent=2))
    identity["testScope"] = m["testScope"]
    (out / "identity.json").write_text(json.dumps(identity, indent=2))
    # Only these regular evidence files leave the guest. No databases, HOME,
    # credentials, or dependency trees.
    hashes = {p.name: digest(p) for p in out.iterdir() if p.is_file()}
    pending = out / "manifest.pending"
    pending.write_text(
        json.dumps(
            {"files": hashes, "head": m["head"], "instanceId": m["instanceId"]},
            indent=2,
        )
    )
    os.replace(pending, out / "manifest.json")
print(
    json.dumps(
        {
            "phase": "evidence-complete",
            "success": success,
            "manifestSha256": digest(out / "manifest.json"),
        }
    ),
    flush=True,
)
sys.exit(0 if success else 2)
