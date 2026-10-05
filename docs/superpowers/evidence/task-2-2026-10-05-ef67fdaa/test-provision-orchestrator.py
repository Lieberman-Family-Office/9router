import subprocess,json,hashlib
from pathlib import Path
src=Path('/Users/jasonlieberman/dev/9router/tests/package-lock.json');out=Path('/Users/jasonlieberman/dev/worktrees/9router-hot-swap-plan-ef67fdaa/docs/superpowers/evidence/task-2-2026-10-05-ef67fdaa')
manifest=json.loads(Path('/Users/jasonlieberman/dev/worktrees/9router-hot-swap-plan-ef67fdaa/tests/package.json').read_text());lock=json.loads(src.read_text())
if lock['packages']['']['devDependencies']!=manifest['devDependencies']:raise RuntimeError('test lock root mismatch')
(out/'test-lock-input.json').write_text(json.dumps({'source':str(src),'sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'provenance':'Existing ignored local lock, read-only inspection; extra hashed dependency input, not source commit','vitestVersion':lock['packages']['node_modules/vitest']['version'],'manifestDependencies':manifest['devDependencies']},indent=2))
with (out/'vitest-provision.log').open('wb') as f:
    cmd=['devbox','upload','9router-qualify-recovery',str(src),'/Volumes/devbox/task2-ef67fdaa-20261005-2215/head/tests/package-lock.json'];f.write((json.dumps(cmd)+'\n').encode());f.flush();r=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=120)
    if r.returncode:raise RuntimeError('test lock upload failed')
    cmd=['devbox','exec','9router-qualify-recovery','--','/bin/sh','-c','cd /Volumes/devbox/task2-ef67fdaa-20261005-2215/head && HOME=/Volumes/devbox/task2-ef67fdaa-20261005-2215/fake-home PATH=/opt/homebrew/bin:/usr/bin:/bin /opt/homebrew/bin/npm ci --prefix tests --no-audit --no-fund && shasum -a 256 tests/package-lock.json'];f.write((json.dumps(cmd)+'\n').encode());f.flush();r=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=240);f.write(('EXIT: '+str(r.returncode)+'\n').encode())
