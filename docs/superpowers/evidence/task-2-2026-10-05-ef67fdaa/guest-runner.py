# Archived runner cleanup; original executed bytes are in commit 4d24627f.
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path("/Volumes/devbox/task2-ef67fdaa-20261005-2215")
OUT = ROOT / "evidence"
OUT.mkdir(parents=True, exist_ok=False)
HEAD = "05b606931b824cf77dc3cc1164b75df88590ed24"
BASE = "5fd82262218f5f00b4f987587318908e548ad65a"
results = []
failed = False


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(name, cmd, cwd=None, limit=120, extra=None):
    started = time.time()
    log = OUT / (name + ".log")
    env = os.environ.copy()
    for k in list(env):
        if any(x in k.upper() for x in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")):
            env.pop(k, None)
    env.update(
        {
            "HOME": str(ROOT / "fake-home"),
            "DATA_DIR": str(ROOT / "fake-data"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "CI": "1",
            "NEXT_TELEMETRY_DISABLED": "1",
            "NINEROUTER_TEST_PACKAGES": str(ROOT / "head"),
            "NINEROUTER_SKIP_BACKGROUND_REFRESH": "1",
        }
    )
    if extra:
        env.update(extra)
    timed = False
    with log.open("wb") as f:
        f.write(("COMMAND: " + json.dumps(cmd) + "\nCWD: " + str(cwd) + "\n").encode())
        f.flush()
        try:
            p = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                rc = p.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                timed = True
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
                rc = 124
        except Exception as e:
            f.write((repr(e) + "\n").encode())
            rc = 127
    r = {
        "name": name,
        "command": cmd,
        "cwd": str(cwd),
        "exit": rc,
        "timeout": timed,
        "seconds": round(time.time() - started, 3),
        "log": log.name,
    }
    results.append(r)
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(r), flush=True)
    return rc


try:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Guest is not macOS ARM64")
    (ROOT / "fake-home").mkdir()
    (ROOT / "fake-data").mkdir()
    run(
        "runtime-versions",
        [
            "/bin/sh",
            "-c",
            (
                "uname -a; sw_vers; id; node --version; n"
                "pm --version; python3 --version; caddy v"
                "ersion; command -v sonar; command -v ruf"
                "f; command -v git"
            ),
        ],
        ROOT,
    )
    ids = {
        p.name: digest(p)
        for p in (
            ROOT / "head.tar",
            ROOT / "baseline.tar",
            ROOT / "source.bundle",
            ROOT / "vitest.config.mjs",
            ROOT / "tests-package-lock.json",
        )
    }
    (OUT / "inputs.json").write_text(
        json.dumps(
            {
                "head": HEAD,
                "baseline": BASE,
                "sha256": ids,
                "platform": platform.platform(),
            },
            indent=2,
        )
    )
    expected = json.loads((ROOT / "expected-inputs.json").read_text())
    if (
        expected["head"] != HEAD
        or expected["baseline"] != BASE
        or ids != expected["inputs"]
    ):
        raise RuntimeError("Transferred source hash mismatch")
    for name, commit in [("head", HEAD), ("baseline", BASE)]:
        target = ROOT / name
        target.mkdir()
        with tarfile.open(ROOT / (name + ".tar")) as archive:
            archive.extractall(target, filter="data")
        if run(name + "-git-init", ["git", "init", "-q"], target):
            raise RuntimeError("git init failed")
        if run(
            name + "-git-fetch",
            ["git", "fetch", "--quiet", str(ROOT / "source.bundle"), commit],
            target,
        ):
            raise RuntimeError("bundle fetch failed")
        if run(name + "-revision", ["git", "reset", "--mixed", commit], target):
            raise RuntimeError("revision bind failed")
        run(
            name + "-source-identity",
            [
                "/bin/sh",
                "-c",
                (
                    "git rev-parse HEAD && git diff --exit-code"
                    ' && test -z "$(git status --porcelain)" && shasum -a 256 '
                    "package.json package-lock.json cli/packa"
                    "ge-lock.json"
                ),
            ],
            target,
        )
    head = ROOT / "head"
    base = ROOT / "baseline"
    # Inspection only: do not borrow guest baseline modules or credentials.
    run(
        "existing-test-lock-inventory",
        [
            "/bin/sh",
            "-c",
            (
                "for p in /Volumes/devbox/9router/tests/p"
                "ackage-lock.json /Volumes/devbox/9router"
                "/source/tests/package-lock.json; do if t"
                'est -f "$p"; then printf "%s\\n" "$p"; sh'
                'asum -a 256 "$p"; fi; done'
            ),
        ],
        ROOT,
    )
    run("locked-root-dependencies", ["npm", "ci", "--no-audit", "--no-fund"], head, 300)
    run(
        "locked-baseline-dependencies",
        ["npm", "ci", "--no-audit", "--no-fund"],
        base,
        300,
    )
    lock = json.loads((ROOT / "tests-package-lock.json").read_text())
    test_manifest = json.loads((head / "tests/package.json").read_text())
    if lock["packages"][""]["devDependencies"] != test_manifest["devDependencies"]:
        raise RuntimeError("test lock root mismatch")
    shutil.copyfile(ROOT / "tests-package-lock.json", head / "tests/package-lock.json")
    if run(
        "locked-test-dependencies",
        [
            "npm",
            "ci",
            "--prefix",
            "tests",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ],
        head,
        300,
    ):
        raise RuntimeError("test dependency provision failed")
    shutil.copyfile(ROOT / "vitest.config.mjs", ROOT / "alias.config.mjs")
    cfg = (
        (ROOT / "alias.config.mjs")
        .read_text()
        .replace(
            "path.resolve(import.meta.dirname, '../..')",
            "path.resolve(import.meta.dirname, 'head')",
        )
    )
    (ROOT / "alias.config.mjs").write_text(cfg)
    run(
        "installed-runtime-identities",
        [
            "node",
            "-e",
            (
                "for(const n of ['next','eslint','better-"
                "sqlite3','@babel/parser']){try{console.l"
                "og(n,require(n+'/package.json').version)"
                "}catch(e){console.log(n,'UNAVAILABLE',e."
                "code)}};console.log('native sqlite',requ"
                "ire('node:sqlite').DatabaseSync.name)"
            ),
        ],
        head,
    )
    run("worker", ["node", "tests/mac/9router_worker.check.cjs"], head, 240)
    run(
        "cli-build-artifacts-native",
        ["node", "--test", "tests/unit/cli-build-artifacts.test.js"],
        head,
        120,
    )
    run("hotswap", ["node", "tests/mac/9router_hotswap.check.cjs"], head, 270)
    run(
        "route-counterfactual",
        ["node", "tests/mac/9router_hotswap_counterfactual.check.cjs"],
        head,
        300,
    )
    run(
        "managed-cleanup",
        [
            "node",
            "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON",
            "tests/unit/managed-cleanup.check.mjs",
        ],
        head,
        120,
        {"NODE_ENV": "production"},
    )
    run(
        "worker-templates",
        ["python3", "tests/mac/test_9router_worker_templates.py"],
        head,
        120,
    )
    for name, file in [
        ("managed-dispatch", "managed-dispatch.check.mjs"),
        ("managed-state", "managed-state.check.mjs"),
        ("token-refresh-cross-process", "token-refresh-cross-process.check.mjs"),
        ("managed-cas-process", "managed-cas-process.check.mjs"),
    ]:
        run(name, ["node", "tests/unit/" + file], head, 150)
    modules = [
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
    ]
    run(
        "ten-module-vitest",
        [
            "node",
            "tests/node_modules/vitest/vitest.mjs",
            "run",
            "--config",
            str(ROOT / "alias.config.mjs"),
            *[("tests/unit/" + m + ".test.js") for m in modules],
            "--reporter=dot",
        ],
        head,
        180,
    )
    paths = [
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
        "src/sse/services/tokenRefresh.js",
        "open-sse/utils/streamHandler.js",
    ]
    for i, p in enumerate(paths):
        run("syntax-" + str(i + 1), ["node", "--check", p], head)
    run(
        "python-syntax",
        ["python3", "-m", "py_compile", "tests/mac/test_9router_worker_templates.py"],
        head,
    )
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", BASE, HEAD], cwd=head, text=True
    ).splitlines()
    js = [p for p in changed if p.endswith((".js", ".cjs", ".mjs"))]
    run("eslint-changed", ["node", "node_modules/eslint/bin/eslint.js", *js], head, 150)
    run(
        "format-tool-availability",
        [
            "/bin/sh",
            "-c",
            (
                "if test -f node_modules/prettier/bin/pre"
                "ttier.cjs; then node node_modules/pretti"
                'er/bin/prettier.cjs --check "$@"; else e'
                'cho "BLOCKED: no manifest-locked formatt'
                'er"; exit 3; fi'
            ),
            "format",
            *js,
        ],
        head,
    )
    run(
        "secrets-scan",
        [
            "/bin/sh",
            "-c",
            (
                "if command -v sonar >/dev/null; then son"
                'ar analyze secrets "$@"; else echo "BLOC'
                "KED: Sonar CLI unavailable; no unpinned "
                'installation permitted"; exit 3; fi'
            ),
            "scan",
            *changed,
        ],
        head,
        180,
    )
    run(
        "s3776-self-check",
        [
            "/bin/sh",
            "-c",
            (
                "if test -f .cursor/scripts/sonar_pr_issu"
                "es.py; then python3 .cursor/scripts/sona"
                'r_pr_issues.py --self-check; else echo "'
                "BLOCKED: repository has no S3776 self-ch"
                'eck driver"; exit 3; fi'
            ),
        ],
        head,
    )
    run(
        "build-cli",
        ["node", "cli/scripts/build-cli.js"],
        head,
        420,
        {"NINEROUTER_CLI_APP_DIR": str(ROOT / "build-app")},
    )
    for name, checkout in [("h2c-baseline", base), ("h2c-head", head)]:
        run(name, ["node", "tests/unit/custom-server-h2c.test.cjs"], checkout, 60)
except Exception as e:
    failed = True
    (OUT / "guest-error.txt").write_text(repr(e))
    print("GUEST ERROR", repr(e), flush=True)
finally:
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    (OUT / "log-hashes.json").write_text(
        json.dumps(
            {
                p.name: digest(p)
                for p in OUT.iterdir()
                if p.is_file() and p.name != "log-hashes.json"
            },
            indent=2,
        )
    )
    with tarfile.open(ROOT / "evidence.tar", "w") as archive:
        archive.add(OUT, arcname="evidence")
    print(
        "EVIDENCE",
        str(ROOT / "evidence.tar"),
        digest(ROOT / "evidence.tar"),
        flush=True,
    )
sys.exit(
    2
    if failed or any(r["exit"] != 0 and r["name"] != "h2c-baseline" for r in results)
    else 0
)
