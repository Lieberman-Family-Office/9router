#!/usr/bin/env python3
"""Run changed-file push checks only in Namespace and export exact outcomes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

run = Path(sys.argv[1])
source = run / 'source'
spec = importlib.util.spec_from_file_location('qualifier', source / 'scripts/mac/9router_vm_qualify.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
q.guest_guard()
os.umask(0o077)
binding = q.private_json(run / 'input.json')
q.runtime_preflight(binding, run)
out = run / 'evidence'
out.mkdir(mode=0o700)
checks = []
secret = run / 'scanner-credential'

def check(name, argv, env, subjects, timeout=180):
    result = subprocess.run(argv, cwd=source, env=env, capture_output=True, text=True, timeout=timeout)
    log = result.stdout + result.stderr
    if secret.exists():
        log = log.replace(secret.read_text(), '[REDACTED]')
    (out / (name + '.log')).write_text(log)
    checks.append({'name': name, 'exit_code': result.returncode, 'subjects': subjects, 'command': argv, 'log_sha256': q.sha256(out / (name + '.log'))})
    return result.returncode

try:
    env = q.isolated_environment(run / 'checks-home', source)
    env.pop('NODE_ENV')
    for folder in [source, source / 'tests', source / 'cli']:
        lock = folder / 'package-lock.json'
        if not lock.exists():
            lock.write_bytes((source / q.TEST_LOCK).read_bytes())
        q.guest_command(['npm', 'ci', '--ignore-scripts', '--include=dev', '--include=optional', '--no-audit', '--no-fund'], folder, env, 900)
    venv = run / 'tools'
    q.output(['python3', '-m', 'venv', str(venv)], env=env)
    python = str(venv / 'bin/python')
    q.guest_command([python, '-m', 'pip', 'install', '--disable-pip-version-check', 'ruff==0.15.18'], source, env, 180)
    changed = q.output(['git', 'diff', '--name-only', 'c0ceebc70d892398870f93dba9af522f870ccc08', 'HEAD'], source).splitlines()
    python_files = [name for name in changed if name.endswith('.py')]
    js_files = [name for name in changed if Path(name).suffix in {'.js', '.cjs', '.mjs'}]
    q.require(python_files and js_files and changed, 'Zero changed check population')
    check('ruff-check', [python, '-m', 'ruff', 'check', '--select=E,F,I', *python_files], env, len(python_files))
    check('ruff-format', [python, '-m', 'ruff', 'format', '--check', *python_files], env, len(python_files))
    check('eslint', ['node', 'node_modules/eslint/bin/eslint.js', '--no-warn-ignored', *js_files], env, len(js_files))
    for name in js_files:
        check('syntax-' + hashlib.sha256(name.encode()).hexdigest()[:8], ['node', '--check', name], env, 1)
    # Produce only a guest formatting/import diff for the host authoring surface.
    q.guest_command([python, '-m', 'ruff', 'check', '--select=I', '--fix', *python_files], source, env, 180)
    q.guest_command([python, '-m', 'ruff', 'format', *python_files], source, env, 180)
    patch = subprocess.run(['git', 'diff', '--binary'], cwd=source, capture_output=True, check=True).stdout
    (out / 'format.patch').write_bytes(patch)
    if secret.exists():
        scanner = run / 'sonar'
        url = 'https://binaries.sonarsource.com/Distribution/sonarqube-cli/1.9.0.15656/macos/sonarqube-cli-1.9.0.15656-macos-arm64.bin'
        with urllib.request.urlopen(url, timeout=120) as incoming, scanner.open('xb') as stream:
            __import__('shutil').copyfileobj(incoming, stream)
        q.require(q.sha256(scanner) == '8de8ec62c3614a9abb7053114fda85b9460b6ebdbca7bb612c6e58dc88ab2015', 'Scanner pin mismatch')
        scanner.chmod(0o700)
        env.update(SONARQUBE_CLI_TOKEN=secret.read_text(), SONARQUBE_CLI_ORG='lieberman-family-office')
        check('sonar-secrets', [str(scanner), 'analyze', 'secrets', *changed], env, len(changed), 600)
    else:
        checks.append({'name': 'sonar-secrets', 'exit_code': None, 'subjects': len(changed), 'failure': 'Existing scanner credential missing'})
finally:
    secret.unlink(missing_ok=True)
    q.new_json(out / 'push-checks.json', {'source_commit': binding['source_commit'], 'namespace': binding['namespace'], 'checks': checks, 'success': bool(checks) and all(item.get('exit_code') == 0 and item['subjects'] > 0 for item in checks)})
    q.new_json(out / 'export.json', {'files': {p.name: q.sha256(p) for p in out.iterdir() if p.is_file() and p.stat().st_size > 0}})
