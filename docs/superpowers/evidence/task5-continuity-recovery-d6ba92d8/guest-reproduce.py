#!/usr/bin/env python3
"""Run the current packaged fixture inside the approved Namespace guest only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

run = Path(sys.argv[1])
source = run / 'source'
spec = importlib.util.spec_from_file_location('qualifier', source / 'scripts/mac/9router_vm_qualify.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
q.guest_guard()
os.umask(0o077)
binding = q.private_json(run / 'input.json')
evidence = run / 'evidence'
evidence.mkdir(mode=0o700)
results = []
record = {'scope': 'focused current-source packaged continuity reproduction, not qualification', 'source_commit': binding['source_commit'], 'namespace': binding['namespace'], 'checks': results, 'success': False}

def check(name, argv, cwd, env, timeout, expected=0):
    try:
        log = q.guest_command(argv, cwd, env, timeout, expected)
        results.append({'name': name, 'exit_code': expected, 'command': argv, 'where': str(cwd)})
        (evidence / (name + '.log')).write_text(log)
        return log
    except BaseException as error:
        (evidence / (name + '.log')).write_text(str(error) + '\n')
        results.append({'name': name, 'exit_code': None, 'command': argv, 'failure': type(error).__name__, 'where': str(cwd)})
        raise

try:
    q.require(q.output(['git', 'rev-parse', 'HEAD'], source) == binding['source_commit'], 'source revision mismatch')
    for name, digest in binding['source_files'].items():
        q.require(q.sha256(source / name) == digest, 'source file mismatch: ' + name)
    q.require(q.sha256(source / 'qualification.patch') == binding['source_patch_sha256'], 'patch mismatch')
    q.runtime_preflight(binding, run)
    env = q.isolated_environment(run / 'build-home', source)
    env.pop('NODE_ENV')
    lock = source / 'tests/package-lock.json'
    if not lock.exists():
        data = (source / q.TEST_LOCK).read_bytes()
        lock.write_bytes(data)
    for folder, name in [(source, 'root'), (source / 'tests', 'tests'), (source / 'cli', 'cli')]:
        manifest = json.loads((folder / 'package.json').read_text())
        data = json.loads((folder / 'package-lock.json').read_text())
        for key in ['dependencies', 'devDependencies', 'optionalDependencies']:
            q.require(manifest.get(key, {}) == data['packages'][''].get(key, {}), 'locked dependency mismatch')
        check('dependencies-' + name, ['npm', 'ci', '--ignore-scripts', '--include=dev', '--include=optional', '--no-audit', '--no-fund'], folder, env, 1200)
    record['versions'] = {'node': q.output(['node', '--version']), 'npm': q.output(['npm', '--version']), 'python': q.output(['python3', '--version']), 'caddy': q.output(['caddy', 'version']), 'macos': q.output(['sw_vers', '-productVersion']), 'architecture': q.output(['uname', '-m'])}
    record['locked_dependencies'] = {str(p.relative_to(source)): q.sha256(p) for p in [source / 'package-lock.json', source / 'tests/package-lock.json', source / 'cli/package-lock.json']}
    check('build', ['node', 'cli/scripts/build-cli.js'], source, env, 1800)
    pack = check('pack', ['npm', 'pack', '--ignore-scripts', '--pack-destination', str(run), '--json'], source / 'cli', env, 300)
    packed = json.loads(pack)
    q.require(len(packed) == 1, 'package population mismatch')
    tarball = run / packed[0]['filename']
    tarball.chmod(0o600)
    binding['tgz'] = str(tarball)
    binding['sha256'] = q.package_preflight(tarball)
    record.update(tarball=tarball.name, sha256=binding['sha256'], runtime_sha256=binding['runtime_sha256'])
    q.new_json(evidence / 'binding.json', binding)
    scope_path, scope, manifest = q.prepare_scope(tarball, binding, install=False)
    package = scope['packages'][binding['sha256']]
    record['manifest_sha256'] = package['manifest_sha256']
    record['persistenceFingerprint'] = manifest['persistenceFingerprint']
    fixture_env = q.isolated_environment(Path(scope['home']), source)
    process = subprocess.Popen(['node', str(source / 'tests/mac/9router_packaged_hotswap.check.mjs'), str(source), str(tarball), binding['sha256'], str(evidence / 'packaged-result.json'), scope['home'], str(scope_path)], cwd=source, env=fixture_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        log, _ = process.communicate(timeout=1800)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 15)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, 9)
                process.wait(timeout=10)
    (evidence / 'fixture.log').write_text(log)
    results.append({'name': 'packaged-fixture', 'exit_code': process.returncode, 'where': str(source)})
    fixture = q.private_json(evidence / 'packaged-result.json')
    record['success'] = process.returncode == 0 and fixture.get('success') is True
    record['fixture_failures'] = fixture.get('failures')
    record['firstSessionFault'] = fixture.get('firstSessionFault')
    record['transitions'] = fixture.get('transitions')
except BaseException as error:
    record['failure'] = {'type': type(error).__name__, 'diagnostic': str(error)}
finally:
    q.new_json(evidence / 'guest-reproduction.json', record)
    q.new_json(evidence / 'export.json', {'files': {p.name: q.sha256(p) for p in evidence.iterdir() if p.is_file()}, 'tarball': record.get('tarball'), 'sha256': record.get('sha256')})
