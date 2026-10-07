// Regenerable green-only Task 4 host runner. Preparation is not execution authorization.
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { createDevboxClient } from '@namespacelabs/sdk';

const [requestedTree, requestedPhase = 'green'] = process.argv.slice(2);
assert(requestedTree && requestedPhase === 'green' && process.argv.length <= 4, 'Supply worktree and optional green only');
const tree = fs.realpathSync(requestedTree);
const expectedHead = '959c577a870a6a598afe05745a8b649f6624f368';
const authorization = { quote: 'Yes—run Task 4 tests and scans in Namespace; verify shutdown twice (Recommended)', turn: 'operator chat_selection at 2026-10-06 00:46 EDT; continued by finish task 4 /goal at 2026-10-06 02:16 EDT' };
throw new Error('Archived runner is evidence only. A new run requires current operator authorization and a newly bound runner.');
const sourcePaths = ['scripts/mac/9router_hotswap.py', 'scripts/mac/9router_deploy.py', 'tests/mac/test_9router_hotswap.py', 'tests/mac/test_9router_deploy.py'];
const requiredChecks = ['venv', 'tools', 'tool-versions', 'dependencies-app', 'dependencies-tests', 'controller-tests', 'ruff-check', 'ruff-format', ...sourcePaths.map((_, i) => 'syntax-' + i), 's3776', 'caddy-download', 'caddy-extract', 'worker', 'proxy', 'counterfactual', 'scanner-download', 'scanner-version', 'secrets'];
const scope = 'Task 4 green source checks only; no package/launchd/production acceptance; Sonar secrets is not code analysis';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const git = (...args) => {
  const p = spawnSync('git', ['-C', tree, ...args], { encoding: 'utf8', timeout: 120000, maxBuffer: 8388608, env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' } });
  assert(!p.error && p.status === 0, 'Git read/staging failed: ' + args[0]);
  return args.includes('-z') ? p.stdout : p.stdout.trim();
};
assert.equal(git('rev-parse', 'HEAD'), expectedHead, 'Reviewed HEAD moved; prepare a new binding');
const regular = filename => { assert(fs.lstatSync(filename).isFile(), 'Regular file required: ' + filename); return fs.readFileSync(filename); };
const sourceBytes = Object.fromEntries(sourcePaths.map(p => [p, regular(path.join(tree, p))]));
const sourceHashes = Object.fromEntries(sourcePaths.map(p => [p, sha(sourceBytes[p])]));
const trackedPaths = git('ls-files', '-z').split('\0').filter(Boolean);
const trackedHashes = Object.fromEntries(trackedPaths.map(p => [p, sha(fs.readFileSync(path.join(tree, p)))]));
const sourceBinding = () => {
  const binding = { head: git('rev-parse', 'HEAD'), sourceHashes: Object.fromEntries(sourcePaths.map(p => [p, sha(regular(path.join(tree, p)))])) };
  assert.equal(binding.head, expectedHead, 'Host HEAD changed');
  assert.deepEqual(binding.sourceHashes, sourceHashes, 'Host Task 4 source changed');
  for (const [p, h] of Object.entries(trackedHashes)) assert.equal(sha(fs.readFileSync(path.join(tree, p))), h, 'Host tracked source changed: ' + p);
  return binding;
};
const task2Evidence = path.join(tree, 'docs/superpowers/evidence/task-2-retry-f76093a4f8ec-de12e533');
const testLockBytes = regular(path.join(task2Evidence, 'tests-package-lock.json'));
const testLockHash = '8fdd1fae2e024e1b4c25cd3649dcc20ec802c031d67b8ff02587273a1d6c3fff';
assert.equal(sha(testLockBytes), testLockHash, 'Proven supplementary test lock changed');
const committed = p => {
  const result = spawnSync('git', ['-C', tree, 'show', expectedHead + ':' + p], { timeout: 30000, maxBuffer: 16777216 });
  assert(!result.error && result.status === 0, 'Missing committed dependency manifest: ' + p);
  assert.equal(sha(regular(path.join(tree, p))), sha(result.stdout), 'Dependency input differs from reviewed HEAD: ' + p);
  return result.stdout;
};
const dependencyHashes = Object.fromEntries(['package.json', 'package-lock.json', 'tests/package.json'].map(p => [p, sha(committed(p))]));
const selfCheckBytes = regular(path.join(os.homedir(), 'dev/og-workflow/.cursor/scripts/sonar_pr_issues.py'));
const ownerHelperBytes = regular(path.join(os.homedir(), 'dev/og-workflow/.cursor/scripts/gh_owner.py'));
const scannerToken = process.env.SONAR_TOKEN;
assert(scannerToken, 'Scanner credential unavailable');
const cli = path.join(os.homedir(), '.local/bin/devbox');
assert(fs.existsSync(cli), 'Installed devbox CLI missing');
const stage = fs.mkdtempSync(path.join(os.tmpdir(), '9r-task4-green-'));
const archive = path.join(stage, 'source.tar');
const bundle = path.join(stage, 'source.bundle');
git('archive', '--format=tar', '--output', archive, expectedHead);
git('bundle', 'create', bundle, 'HEAD');
const runId = 'green-' + expectedHead.slice(0, 12) + '-' + randomUUID().slice(0, 8);
const remote = '/Volumes/devbox/t4' + runId.slice(-8);
const evidence = path.join(tree, 'docs/superpowers/evidence/task-4-' + runId);
assert(!fs.existsSync(evidence), 'Evidence output already exists');
fs.mkdirSync(evidence, { recursive: true, mode: 0o700 });
const record = { schema: 2, runId, head: expectedHead, phase: 'green', authorization, sourceHashes, dependencyHashes,
  testLockSha256: testLockHash, selfCheckSha256: sha(selfCheckBytes), ownerHelperSha256: sha(ownerHelperBytes),
  archiveSha256: sha(regular(archive)), bundleSha256: sha(regular(bundle)), orchestratorSha256: sha(regular(new URL(import.meta.url))),
  sourceBefore: sourceBinding(), requiredChecks, devboxId: '2tc0b2eg4mveo', devboxName: '9router-qualify-recovery', lifecycle: [], scope, result: 'incomplete' };
const clean = e => String(e?.message || e).replaceAll(scannerToken, '[REDACTED]').replace(/(?:nsct_|ghp_|github_pat_|sk-)[A-Za-z0-9_.-]+/g, '[REDACTED]');
const save = () => {
  try { fs.writeFileSync(path.join(evidence, 'host.json'), JSON.stringify(record, null, 2), { mode: 0o600 }); return true; }
  catch (e) { record.hostWriteFailure = clean(e); return false; }
};
const snapshot = b => ({ at: new Date().toISOString(), monotonicMs: performance.now(), id: b.id, name: b.name, state: b.info.state, instanceId: b.info.instanceId || null });
const matches = b => { assert.equal(b.id, record.devboxId); assert.equal(b.name, record.devboxName); assert.equal(b.info.shape.os, 'macos'); assert.equal(b.info.shape.architecture, 'arm64'); };
const cliEnv = { ...process.env }; delete cliEnv.SONAR_TOKEN;
const cliRun = (args, timeout = 120000) => {
  const p = spawnSync(cli, args, { encoding: 'utf8', timeout, maxBuffer: 2097152, env: cliEnv });
  assert(!p.error && p.status === 0, 'Devbox ' + args[0] + ' failed: ' + clean(p.error || p.stderr));
  return p.stdout + '\n' + p.stderr;
};
const archivedGuest = String.raw`
import hashlib,json,os,pathlib,re,signal,subprocess,sys,time,xml.etree.ElementTree as ET
root=pathlib.Path(sys.argv[1]);m=json.loads((root/'input.json').read_text());out=root/'evidence';out.mkdir(mode=0o700)
head=root/'head';results=[];active=None;success=False;aborted=False;before=None;after=None;tracked={};credential_clean=False
secret=(root/'scanner-credential').read_text()
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def clean(s):
    s=re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|(?:access_token|refresh_token|api[_-]?key|password|client_secret|session_secret)\s*["\x27]?\s*[:=]\s*["\x27]?)[^\s,"\x27}]+',r'\1[REDACTED]',s.replace(secret,'[REDACTED]'))
    return re.sub(r'(?:nsct_|ghp_|github_pat_|sk-)[A-Za-z0-9_.-]+','[REDACTED]',s)
def environment(name):
    d=root/'isolated'/name
    for p in [d/'home',d/'tmp',d/'data',d/'cache']:p.mkdir(parents=True,exist_ok=True,mode=0o700)
    return {'PATH':str(root/'tools')+':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin','CADDY_BIN':str(root/'tools/caddy'),'HOME':str(d/'home'),'TMPDIR':str(d/'tmp'),'DATA_DIR':str(d/'data'),'XDG_CONFIG_HOME':str(d/'home/config'),'XDG_CACHE_HOME':str(d/'cache'),'CI':'true','NODE_ENV':'production','PYTHONDONTWRITEBYTECODE':'1','NINEROUTER_TEST_PACKAGES':str(head),'npm_config_cache':str(root/'npm-cache'),'npm_config_audit':'false','npm_config_fund':'false'}
def command(args,cwd=root):return subprocess.check_output(args,cwd=cwd,env=environment('metadata'),text=True,timeout=30).strip()
def binding():
    b={'head':command(['git','rev-parse','HEAD'],head),'sourceHashes':{p:digest(head/p) for p in m['sourceHashes']}}
    assert b['head']==m['head'] and b['sourceHashes']==m['sourceHashes'],'Guest HEAD or Task 4 source changed'
    for p,h in tracked.items():assert digest(head/p)==h,'Guest tracked source changed: '+p
    for p,h in m['dependencyHashes'].items():assert digest(head/p)==h,'Guest app/test manifest changed'
    assert digest(head/'tests/package-lock.json')==m['testLockSha256'],'Guest supplementary lock changed'
    return b
def stop_group():
    global active
    if active is None:return
    # Signal the owned group even if its leader exited; descendants can outlive it.
    for sig in (signal.SIGTERM,signal.SIGKILL):
        try:os.killpg(active.pid,sig)
        except ProcessLookupError:break
        except PermissionError:
            if active.poll() is None:active.send_signal(sig)
            raise RuntimeError('owned guest process group cleanup denied')
        if sig==signal.SIGTERM:time.sleep(1)
    active.wait(timeout=5);active=None
def interrupt(sig,frame):
    global aborted
    aborted=True;raise KeyboardInterrupt('guest interrupted or deadline exceeded')
for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP,signal.SIGALRM):signal.signal(sig,interrupt)
signal.alarm(3600)
def run(name,args,cwd,limit,population,validate=None):
    global active
    if before is not None:binding()
    raw=root/(name+'.raw');timed=False;code=None;cleanup=None;valid=False;details={};started=time.monotonic()
    print(json.dumps({'phase':'check-start','name':name}),flush=True)
    try:
        env=environment(name)
        if name=='secrets':env.update(SONARQUBE_CLI_TOKEN=secret,SONARQUBE_CLI_ORG='lieberman-family-office')
        if name=='s3776':env['PATH']=str(root/'venv/bin')+':'+env['PATH']
        with raw.open('wb') as f:
            active=subprocess.Popen(args,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
            try:code=active.wait(timeout=limit)
            except subprocess.TimeoutExpired:timed=True
    finally:
        try:stop_group()
        except BaseException as e:cleanup=clean(str(e))
        text=clean(raw.read_text(errors='replace')) if raw.exists() else 'NOT RUN'
        (out/(name+'.log')).write_text(text);raw.unlink(missing_ok=True)
        try:
            assert code==0 and not timed and cleanup is None,'failed/unrun command'
            if validate:details=validate(text) or {}
            if before is not None:binding()
            valid=True
        except BaseException as e:details={'validationFailure':clean(str(e))}
        entry={'name':name,'args':args,'exitCode':code,'timeout':timed,'cleanupFailure':cleanup,'validated':valid,'population':population,'seconds':time.monotonic()-started,**details}
        results.append(entry);(out/'checks.json').write_text(json.dumps(results,indent=2))
        print(json.dumps({'phase':'check-end','name':name,'exitCode':code,'validated':valid}),flush=True)
    return valid
def unit_summary(text):
    suites=list(ET.parse(out/'pytest.xml').getroot().iter('testsuite'))
    counts={k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped']}
    (out/'population.json').write_text(json.dumps(counts))
    assert counts['tests']>0 and all(counts[k]==0 for k in ['failures','errors','skipped']),'empty/failed/skipped unit population'
    cases=list(ET.parse(out/'pytest.xml').getroot().iter('testcase'))
    assert all(any(x.attrib.get('classname','').endswith(n) for x in cases) for n in ['test_9router_hotswap','test_9router_deploy']),'unrun unit module'
    return {'testCount':counts['tests'],'moduleCount':2}
def native_summary(name):
    def validate(text):
        assert not re.search(r'(?i)\b(?:skipped|todo)\b',text),'skipped native assertions'
        markers={'worker':['PASS: real pinned workers'],'proxy':['PASS'],'counterfactual':['GREEN: unchanged byte-identical mirror','RED: removing only','PASS: routing counterfactual']}
        assert all(marker in text for marker in markers[name]),'missing native proof markers'
        return {'requiredMarkers':markers[name]}
    return validate
def secrets_summary(text):
    assert not re.search(r'(?i)\b(?:skipped|not entitled|unauthorized|forbidden)\b',text),'secrets leg did not run'
    assert 'No secrets found' in text,'secrets scanner did not report a clean result'
    return {'leg':'secrets only; not code analysis'}
try:
    assert command(['uname','-s'])=='Darwin' and command(['uname','-m'])=='arm64'
    versions={k:command(v) for k,v in {'node':['node','--version'],'npm':['npm','--version'],'python':['python3','--version'],'macos':['sw_vers','-productVersion']}.items()}
    (out/'runtime.json').write_text(json.dumps(versions,indent=2))
    assert versions['node']=='v26.10.0' and versions['npm']=='11.19.1','runtime identity mismatch'
    for p,key in [('source.tar','archiveSha256'),('source.bundle','bundleSha256'),('runner.py','runnerSha256'),('sonar_pr_issues.py','selfCheckSha256'),('gh_owner.py','ownerHelperSha256'),('tests-package-lock.json','testLockSha256')]:assert digest(root/p)==m[key],'Transferred artifact hash mismatch: '+p
    head.mkdir(mode=0o700)
    subprocess.run(['/usr/bin/tar','-xf',str(root/'source.tar'),'-C',str(head)],env=environment('extract'),check=True,timeout=60)
    subprocess.run(['git','clone','--no-checkout',str(root/'source.bundle'),str(root/'git-source')],env=environment('clone'),check=True,timeout=120,stdout=subprocess.DEVNULL)
    subprocess.run(['git','-C',str(root/'git-source'),'checkout','--detach',m['head']],env=environment('checkout'),check=True,timeout=60,stdout=subprocess.DEVNULL)
    (head/'.git').write_text('gitdir: '+str(root/'git-source/.git')+'\n')
    for p,h in m['sourceHashes'].items():
        incoming=root/pathlib.Path(p).name;assert digest(incoming)==h
        (head/p).write_bytes(incoming.read_bytes())
    (head/'tests/package-lock.json').write_bytes((root/'tests-package-lock.json').read_bytes())
    for directory in [head,head/'tests']:
        pkg=json.loads((directory/'package.json').read_text());lock=json.loads((directory/'package-lock.json').read_text())
        assert lock['lockfileVersion']>=2
        for key in ['dependencies','devDependencies','optionalDependencies']:assert pkg.get(key,{})==lock['packages'][''].get(key,{}),'Lock/manifest mismatch: '+key
    tracked={p:digest(head/p) for p in command(['git','ls-files','-z'],head).split('\0') if p}
    before=binding()
    assert run('venv',['python3','-m','venv',str(root/'venv')],root,60,{'unit':'guest environments','count':1})
    python=str(root/'venv/bin/python')
    assert run('tools',[python,'-m','pip','install','--disable-pip-version-check','pytest==8.4.2','ruff==0.15.18'],root,240,{'unit':'proven RED tools','count':2})
    version_code='import importlib.metadata as m; assert m.version("pytest")=="8.4.2"; assert m.version("ruff")=="0.15.18"; print("pytest==8.4.2 ruff==0.15.18")'
    assert run('tool-versions',[python,'-c',version_code],root,30,{'unit':'pinned tools','count':2})
    for name,cwd in [('app',head),('tests',head/'tests')]:assert run('dependencies-'+name,['npm','ci','--ignore-scripts','--include=dev','--include=optional','--no-audit','--no-fund'],cwd,900,{'unit':'hash-bound locked guest installs','count':1})
    (out/'dependencies.json').write_text(json.dumps({'dependencyHashes':m['dependencyHashes'],'testLockSha256':m['testLockSha256'],'install':'guest-only npm ci --ignore-scripts; no host dependencies'},indent=2))
    all_ok=run('controller-tests',[python,'-m','pytest','tests/mac/test_9router_hotswap.py','tests/mac/test_9router_deploy.py','-vv','--maxfail=1','-o','faulthandler_timeout=60','--junitxml='+str(out/'pytest.xml')],head,600,{'unit':'Task 4 and legacy deploy unit modules','count':2},unit_summary)
    for name,args in [('ruff-check',[python,'-m','ruff','check','--select=E,F,I',*m['sourceHashes']]),('ruff-format',[python,'-m','ruff','format','--check',*m['sourceHashes']])]:
        passed=run(name,args,head,60,{'unit':'exact Task 4 Python files','count':4});all_ok=passed and all_ok
        if name=='ruff-format' and not passed:
            for p in m['sourceHashes']:
                formatted=subprocess.run([python,'-m','ruff','format','--stdin-filename',p],input=(head/p).read_bytes(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment('format-preview'),check=True,timeout=30)
                (out/('formatted-'+pathlib.Path(p).name)).write_bytes(formatted.stdout)
    for i,p in enumerate(m['sourceHashes']):all_ok=run('syntax-'+str(i),[python,'-m','py_compile',p],head,30,{'unit':'Task 4 Python files','count':1}) and all_ok
    # Explicit population remains visible when the helper reports advisory C901 debt.
    code='import sys;sys.path.insert(0,'+repr(str(root))+');import sonar_pr_issues as s; p=sys.argv[2:]; assert len(p)==4; print("task4-self-check-population: 4 file(s)",flush=True); raise SystemExit(s.self_check(p,base=sys.argv[1]))'
    def selfcheck_summary(text):
        assert re.search(r'^task4-self-check-population: 4 file\(s\)$',text,re.M),'zero/unreadable self-check population'
        assert 'self-check' in text and not re.search(r'(?i)could not run|0 file\(s\)|could not parse',text),'self-check did not run'
        return {'fileCount':4,'leg':'S3776 self-check heuristic; not Sonar code analysis'}
    all_ok=run('s3776',[python,'-c',code,m['head'],*m['sourceHashes']],head,60,{'unit':'exact Task 4 Python files','count':4},selfcheck_summary) and all_ok
    caddy=root/'tools/caddy';tar=root/'caddy.tgz'
    assert run('caddy-download',['curl','--fail','--location','--max-time','120','--output',str(tar),'https://github.com/caddyserver/caddy/releases/download/v2.11.4/caddy_2.11.4_mac_arm64.tar.gz'],root,150,{'unit':'pinned guest runtime downloads','count':1})
    assert digest(tar)=='9efb0af2d6cf09cfb5053c0e51721b9b3d4956d346234f39368d943d25a3c9a7'
    assert run('caddy-extract',['tar','-xzf',str(tar),'-C',str(root/'tools'),'caddy'],root,30,{'unit':'hash-verified runtime extractions','count':1})
    caddy.chmod(0o700);versions['caddy']=command([str(caddy),'version']);assert versions['caddy'].split()[0]=='v2.11.4'
    (out/'runtime.json').write_text(json.dumps(versions,indent=2))
    for name,file in [('worker','9router_worker.check.cjs'),('proxy','9router_hotswap.check.cjs'),('counterfactual','9router_hotswap_counterfactual.check.cjs')]:all_ok=run(name,['node','tests/mac/'+file],head,600,{'unit':'native assertion commands','count':1},native_summary(name)) and all_ok
    scanner=root/'tools/sonar'
    assert run('scanner-download',['curl','--fail','--location','--max-time','120','--output',str(scanner),'https://binaries.sonarsource.com/Distribution/sonarqube-cli/1.9.0.15656/macos/sonarqube-cli-1.9.0.15656-macos-arm64.bin'],root,150,{'unit':'pinned guest scanners','count':1})
    assert digest(scanner)=='8de8ec62c3614a9abb7053114fda85b9460b6ebdbca7bb612c6e58dc88ab2015';scanner.chmod(0o700)
    all_ok=run('scanner-version',[str(scanner),'--version'],head,60,{'unit':'pinned scanners','count':1}) and all_ok
    all_ok=run('secrets',[str(scanner),'analyze','secrets',*m['sourceHashes']],head,600,{'unit':'exact Task 4 source files','count':4},secrets_summary) and all_ok
    names=[x['name'] for x in results]
    assert sorted(names)==sorted(m['requiredChecks']) and all(x['validated'] for x in results),'failed/unrun required checks'
    success=all_ok
except BaseException as e:
    success=False;(out/'failure.json').write_text(json.dumps({'type':type(e).__name__,'reason':clean(str(e))}))
finally:
    signal.alarm(0)
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP,signal.SIGALRM):signal.signal(sig,signal.SIG_IGN)
    try:stop_group()
    except BaseException as e:success=False;(out/'cleanup-failure.json').write_text(json.dumps({'reason':clean(str(e))}))
    try:(root/'scanner-credential').unlink(missing_ok=True);credential_clean=not (root/'scanner-credential').exists()
    except BaseException as e:success=False;(out/'credential-failure.json').write_text(json.dumps({'reason':clean(str(e))}))
    try:after=binding();assert before==after
    except BaseException as e:success=False;(out/'immutability-failure.json').write_text(json.dumps({'reason':clean(str(e))}))
    success=success and credential_clean and not aborted
    (out/'source-binding.json').write_text(json.dumps({'before':before,'after':after},indent=2))
    (out/'checks.json').write_text(json.dumps(results,indent=2))
    identity={**m,'success':success,'interrupted':aborted,'credentialCleanup':credential_clean,'sourceBefore':before,'sourceAfter':after,'scope':m['scope']}
    (out/'identity.json').write_text(json.dumps(identity,indent=2))
    for p in out.iterdir():
        if p.is_file():p.write_text(clean(p.read_text(errors='replace')))
    files={p.name:digest(p) for p in out.iterdir() if p.is_file()}
    pending=out/'manifest.pending';pending.write_text(json.dumps({'files':files,'head':m['head'],'instanceId':m['instanceId'],'runId':m['runId']}));pending.replace(out/'manifest.json')
print(json.dumps({'phase':'evidence-complete','success':success}),flush=True)
sys.exit(0 if success else 2)
`;
const guest = fs.readFileSync(new URL('./guest-runner.py', import.meta.url), 'utf8');
record.runnerSha256 = sha(Buffer.from(guest));
fs.writeFileSync(path.join(evidence, 'guest-runner.py'), guest, { mode: 0o600 });
fs.writeFileSync(path.join(evidence, 'orchestrator.mjs'), regular(new URL(import.meta.url)), { mode: 0o600 });
save();
let metadata, claim, claimStat, owns = false, complete = false, interrupted = false, guestExported = false;
const claimPath = path.join(os.tmpdir(), 'namespace-owner-2tc0b2eg4mveo.lock');
const claimBytes = Buffer.from(JSON.stringify({ pid: process.pid, runId, head: expectedHead, nonce: randomUUID() }));
const claimOwned = () => {
  if (claim === undefined || !claimStat) return false;
  try { const s = fs.lstatSync(claimPath); const fd = fs.fstatSync(claim); return s.isFile() && s.dev === claimStat.dev && s.ino === claimStat.ino && fd.dev === s.dev && fd.ino === s.ino && fs.readFileSync(claimPath).equals(claimBytes); }
  catch { return false; }
};
const requireClaim = () => assert(claimOwned(), 'Exclusive PID claim no longer belongs to this runner');
const onSignal = () => { interrupted = true; };
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(sig, onSignal);
try {
  claim = fs.openSync(claimPath, 'wx', 0o600);claimStat = fs.fstatSync(claim);fs.writeFileSync(claim, claimBytes);
  requireClaim();sourceBinding();assert(!interrupted, 'Interrupted before activation');
  // Metadata-only SDK Fetch: no connection/session/display calls in shutdown observation.
  metadata = createDevboxClient({ connectionTimeoutMs: 15000 });
  let box = await metadata.devboxes.get(record.devboxId, { timeoutMs: 15000 });matches(box);
  record.lifecycle.push({ phase: 'before', ...snapshot(box) });save();
  assert.equal(box.info.state, 'stopped');assert(!box.info.instanceId);
  requireClaim();assert(!interrupted, 'Interrupted before activation');
  owns = true; // Includes an uncertain-start failure: cleanup must still stop owned compute.
  cliRun(['exec', record.devboxName, '--', '/usr/bin/uname', '-m'], 180000);
  box = await metadata.devboxes.get(record.devboxId, { timeoutMs: 15000 });matches(box);assert.equal(box.info.state, 'running');assert(box.info.instanceId);
  record.instanceId = box.info.instanceId;record.lifecycle.push({ phase: 'activated', ...snapshot(box) });save();
  // Remove only the credential stranded by this conversation's interrupted attempt.
  cliRun(['exec', record.devboxName, '--', '/opt/homebrew/bin/python3', '-c', 'import pathlib; p=pathlib.Path("/Volumes/devbox/t4d889f255/scanner-credential"); p.unlink(missing_ok=True); assert not p.exists()']);
  record.priorCredentialCleanup = true;
  cliRun(['exec', record.devboxName, '--', '/bin/mkdir', '-m', '700', remote]);cliRun(['exec', record.devboxName, '--', '/bin/mkdir', '-m', '700', remote + '/tools']);
  const inputs = [[archive, 'source.tar'], [bundle, 'source.bundle']];
  const stageBytes = (name, bytes) => { const p = path.join(stage, name);fs.writeFileSync(p, bytes, { mode: 0o600 });inputs.push([p, name]); };
  stageBytes('input.json', JSON.stringify(record));stageBytes('runner.py', guest);stageBytes('tests-package-lock.json', testLockBytes);
  stageBytes('sonar_pr_issues.py', selfCheckBytes);stageBytes('gh_owner.py', ownerHelperBytes);stageBytes('scanner-credential', scannerToken);
  for (const p of sourcePaths) stageBytes(path.basename(p), sourceBytes[p]);
  for (const [local, target] of inputs) { requireClaim();assert(!interrupted, 'Interrupted during transfer');cliRun(['upload', record.devboxName, local, remote + '/' + target]); }
  sourceBinding();requireClaim();assert(!interrupted, 'Interrupted before guest start');
  const output = cliRun(['exec', '--detach', record.devboxName, '--', '/opt/homebrew/bin/python3', remote + '/runner.py', remote], 90000);
  record.executionId = output.match(/\bexec_[a-z0-9]+\b/)?.[0];assert(record.executionId, 'Missing detached execution identity');save();
  const deadline = performance.now() + 3630000;
  while (performance.now() < deadline && !interrupted) {
    requireClaim();sourceBinding();box = await metadata.devboxes.get(record.devboxId, { timeoutMs: 15000 });matches(box);assert.equal(box.info.state, 'running');assert.equal(box.info.instanceId, record.instanceId);
    const p = spawnSync(cli, ['exec', record.devboxName, '--', '/bin/test', '-s', remote + '/evidence/manifest.json'], { encoding: 'utf8', timeout: 30000, env: cliEnv });
    assert(!p.error && (p.status === 0 || p.status === 1), 'Manifest observation failed');
    if (p.status === 0) { complete = true;break; }
    await delay(15000);
  }
  assert(complete, 'Guest evidence missing, interrupted, or deadline exceeded');
} catch (e) { complete = false;record.failure = clean(e);save(); }
finally {
  try { fs.rmSync(path.join(stage, 'scanner-credential'), { force: true });record.hostCredentialCleanup = true; }
  catch (e) { complete = false;record.hostCredentialCleanupFailure = clean(e); }
  if (owns) {
    try {
      requireClaim();const box = await metadata.devboxes.get(record.devboxId, { timeoutMs: 15000 });matches(box);assert.equal(box.info.state, 'running');assert.equal(box.info.instanceId, record.instanceId);
      const remoteManifestHash = cliRun(['exec', record.devboxName, '--', '/usr/bin/shasum', '-a', '256', remote + '/evidence/manifest.json'], 30000).trim().split(/\s+/)[0];
      assert(/^[a-f0-9]{64}$/.test(remoteManifestHash));
      const manifestPath = path.join(evidence, 'manifest.json');cliRun(['download', record.devboxName, remote + '/evidence/manifest.json', manifestPath], 30000);
      assert.equal(sha(regular(manifestPath)), remoteManifestHash);record.manifestSha256 = remoteManifestHash;
      const manifest = JSON.parse(regular(manifestPath));assert.equal(manifest.head, expectedHead);assert.equal(manifest.instanceId, record.instanceId);assert.equal(manifest.runId, runId);
      const mandatory = ['identity.json', 'checks.json', 'source-binding.json', 'population.json', 'pytest.xml', 'runtime.json', 'dependencies.json', ...requiredChecks.map(p => p + '.log')];
      for (const [p, h] of Object.entries(manifest.files)) {
        assert(/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(p) && /^[a-f0-9]{64}$/.test(h) && !['host.json', 'orchestrator.mjs', 'guest-runner.py', 'manifest.json'].includes(p), 'Unsafe evidence export');
        const dest = path.join(evidence, p);cliRun(['download', record.devboxName, remote + '/evidence/' + p, dest], 30000);assert.equal(sha(regular(dest)), h);fs.chmodSync(dest, 0o600);
      }
      record.guestDiagnosticsExported = true;
      assert(mandatory.every(p => Object.hasOwn(manifest.files, p)), 'Incomplete guest evidence manifest');
      const identity = JSON.parse(regular(path.join(evidence, 'identity.json')));
      for (const key of ['runId', 'head', 'phase', 'archiveSha256', 'bundleSha256', 'runnerSha256', 'selfCheckSha256', 'ownerHelperSha256', 'testLockSha256', 'devboxId', 'instanceId', 'scope']) assert.equal(identity[key], record[key], 'Guest identity mismatch: ' + key);
      for (const key of ['sourceHashes', 'dependencyHashes', 'authorization', 'requiredChecks']) assert.deepEqual(identity[key], record[key]);
      assert.equal(identity.success, true);assert.equal(identity.interrupted, false);assert.equal(identity.credentialCleanup, true);
      assert.deepEqual(identity.sourceBefore, { head: expectedHead, sourceHashes });assert.deepEqual(identity.sourceAfter, identity.sourceBefore);
      assert.deepEqual(JSON.parse(regular(path.join(evidence, 'source-binding.json'))), { before: identity.sourceBefore, after: identity.sourceAfter });
      const checks = JSON.parse(regular(path.join(evidence, 'checks.json')));
      assert.deepEqual(checks.map(p => p.name).sort(), [...requiredChecks].sort(), 'Failed/unrun required checks');
      assert(checks.every(p => p.exitCode === 0 && p.timeout === false && p.cleanupFailure === null && p.validated === true && Number.isInteger(p.population?.count) && p.population.count > 0));
      const population = JSON.parse(regular(path.join(evidence, 'population.json')));assert(population.tests > 0 && ['failures', 'errors', 'skipped'].every(k => population[k] === 0));
      assert.equal(checks.find(p => p.name === 'controller-tests').moduleCount, 2);assert.equal(checks.find(p => p.name === 's3776').fileCount, 4);
      assert.equal(checks.find(p => p.name === 'secrets').leg, 'secrets only; not code analysis');
      sourceBinding();guestExported = true;record.guestSuccess = true;
    } catch (e) { complete = false;record.exportFailure = clean(e); }
    try {
      requireClaim();const b = await metadata.devboxes.get(record.devboxId, { timeoutMs: 15000 });matches(b);
      assert.equal(b.info.state, 'running');assert(!record.instanceId || b.info.instanceId === record.instanceId, 'Different instance; do not connect for cleanup');
      cliRun(['exec', record.devboxName, '--', '/opt/homebrew/bin/python3', '-c', 'import pathlib;p=pathlib.Path(' + JSON.stringify(remote + '/scanner-credential') + ');p.unlink(missing_ok=True);assert not p.exists()'], 30000);
      record.guestCredentialCleanup = true;
    } catch (e) { complete = false;record.credentialCleanupFailure = clean(e); }
    // Cleanup failures must not skip stop. A replaced/foreign claim is never used as authority.
    try { requireClaim();cliRun(['stop', record.devboxName, '--force'], 120000);record.stopSucceeded = true; }
    catch (e) { complete = false;record.stopFailure = clean(e); }
  }
  if (claimOwned() && metadata) {
    for (const phase of ['immediate', 'after-60-seconds']) {
      if (phase === 'after-60-seconds') await delay(60000);
      try {
        requireClaim();const b = await metadata.devboxes.get(record.devboxId, { timeoutMs: 15000 });matches(b);
        const observation = { phase, ...snapshot(b) };record.lifecycle.push(observation);
        assert.equal(b.info.state, 'stopped');assert(!b.info.instanceId);
        if (phase === 'after-60-seconds') { const first = record.lifecycle.find(x => x.phase === 'immediate');assert(first && observation.monotonicMs - first.monotonicMs >= 60000, 'Delayed observation was too early'); }
      } catch (e) {
        complete = false;record[phase + 'Failure'] = clean(e);
        if (owns && claimOwned()) { try { cliRun(['stop', record.devboxName, '--force'], 120000); } catch (retry) { record[phase + 'RestopFailure'] = clean(retry); } }
      }
      save();
    }
  }
  try { record.sourceAfter = sourceBinding();assert.deepEqual(record.sourceAfter, record.sourceBefore); }
  catch (e) { complete = false;record.sourceBindingFailure = clean(e); }
  try { metadata?.close(); } catch (e) { complete = false;record.closeFailure = clean(e); }
  const observations = record.lifecycle.filter(x => ['immediate', 'after-60-seconds'].includes(x.phase));
  const shutdownSafe = observations.length === 2 && observations.every(x => x.state === 'stopped' && !x.instanceId) && observations[1].monotonicMs - observations[0].monotonicMs >= 60000 && !record.immediateFailure && !record['after-60-secondsFailure'] && (!owns || (record.stopSucceeded && !record.stopFailure));
  record.interrupted = interrupted;record.shutdownVerified = shutdownSafe;
  record.result = complete && guestExported && shutdownSafe && record.hostCredentialCleanup && record.guestCredentialCleanup && !interrupted && !record.failure && !record.hostWriteFailure && !record.sourceBindingFailure && !record.closeFailure && claimOwned() ? 'source-checks-pass' : 'incomplete-or-failed';
  if (!save()) record.result = 'incomplete-or-failed';
  if (claim !== undefined) {
    // Never unlink on !owns alone: uncertain metadata/cleanup retains this PID claim.
    // ponytail: cooperative exclusive PID lock; hostile path replacement needs an external supervisor, not another local authorization layer.
    try { if (shutdownSafe && claimOwned() && !record.hostWriteFailure && !record.closeFailure && record.hostCredentialCleanup && (!owns || record.guestCredentialCleanup)) { fs.unlinkSync(claimPath);record.claimReleased = true; } else record.claimRetained = true; }
    catch (e) { record.claimRetained = true;record.claimReleaseFailure = clean(e);record.result = 'incomplete-or-failed'; }
    try { fs.closeSync(claim); } catch (e) { record.claimCloseFailure = clean(e);record.result = 'incomplete-or-failed'; }
  }
  if (!save()) record.result = 'incomplete-or-failed';
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.removeListener(sig, onSignal);
  console.log(JSON.stringify({ evidence, result: record.result, scope }));process.exitCode = record.result === 'source-checks-pass' ? 0 : 2;
}
// ponytail: SIGKILL/power loss bypass finally; retained PID claim requires externally supervised stop and two metadata observations.
