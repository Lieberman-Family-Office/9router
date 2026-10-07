// Regenerable orchestration only. All executable validation occurs inside Namespace.
import assert from 'node:assert/strict';
import {createHash, randomUUID} from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {setTimeout as delay} from 'node:timers/promises';
import {createDevboxClient} from '@namespacelabs/sdk';
const tree=process.argv[2];
assert(tree && path.isAbsolute(tree), 'Supply the owned absolute worktree');
const head='959c577a870a6a598afe05745a8b649f6624f368', id='2tc0b2eg4mveo', name='9router-qualify-recovery';
const tests=['tests/mac/test_9router_hotswap.py','tests/mac/test_9router_deploy.py'];
const cli=path.join(os.homedir(),'.local/bin/devbox');
const sha=b=>createHash('sha256').update(b).digest('hex');
const git=(...args)=>{const p=spawnSync('git',['-C',tree,...args],{env:{...process.env,GIT_OPTIONAL_LOCKS:'0'},timeout:120000,maxBuffer:64*1024*1024}); assert(!p.error&&p.status===0,`Git read/transfer failed: ${args[0]}`);return p.stdout;};
const runId=head.slice(0,12)+'-'+randomUUID().slice(0,8);
const evidence=path.join(tree,'docs/superpowers/evidence','task-4-red-'+runId);
const relative=path.relative(tree,evidence), staging=fs.mkdtempSync('/tmp/9router-task4-red-'), remote='/Volumes/devbox/r'+runId.slice(-8);
const claimPath='/tmp/namespace-owner-2tc0b2eg4mveo.lock';
assert.equal(git('rev-parse','HEAD').toString().trim(),head);
const indexPath=git('rev-parse','--path-format=absolute','--git-path','index').toString().trim();
const priorStatus=git('status','--porcelain=v1','-z','--untracked-files=all');
const priorPaths=new Set([...git('ls-files','-z').toString().split('\0'),...priorStatus.toString().split('\0').filter(Boolean).map(x=>x.slice(3))].filter(Boolean));
const priorHashes=Object.fromEntries([...priorPaths].map(p=>[p,sha(fs.readFileSync(path.join(tree,p)))]));
const indexHash=sha(fs.readFileSync(indexPath));
const overlay=Object.fromEntries(tests.map(p=>[p,sha(fs.readFileSync(path.join(tree,p)))]));
const immutable=()=>{assert.equal(git('rev-parse','HEAD').toString().trim(),head);assert.equal(sha(fs.readFileSync(indexPath)),indexHash,'Index bytes changed');for(const [p,h] of Object.entries(priorHashes))assert.equal(sha(fs.readFileSync(path.join(tree,p))),h,'Existing file changed: '+p);assert(git('status','--porcelain=v1','-z','--untracked-files=all','--','.',':(exclude)'+relative).equals(priorStatus),'Existing index/worktree status changed');};
const transcript=process.argv[3];
assert(transcript && path.isAbsolute(transcript), 'Supply the authorization source');
throw new Error('Historical transcript is evidence, not current execution authorization. Prepare a newly bound runner.');
const transcriptLines=fs.readFileSync(transcript,'utf8').split('\n');
const authorization=[{line:3353,quote:'Yes—authorize Namespace startup and testing, with verified shutdown afterward (Recommended)'},{line:3541,quote:'APPROVED'},{line:3688,quote:'continue to next task on Implementation plan'}].map(x=>{const turn=JSON.parse(transcriptLines[x.line-1]);assert.equal(turn.role,'user');assert(turn.message.content.some(c=>c.text?.includes(x.quote)));return {...x,role:turn.role,turnSha256:sha(Buffer.from(transcriptLines[x.line-1]))};});
const plugin=String.raw`import json, os
from pathlib import Path
records=[]
collected=[]
collection=[]
def pytest_collection_modifyitems(items):
    collected.extend(item.nodeid for item in items)
def pytest_collectreport(report):
    if report.failed: collection.append({'nodeid':report.nodeid,'message':str(report.longrepr)})
def pytest_runtest_logreport(report):
    records.append({'nodeid':report.nodeid,'phase':report.when,'outcome':report.outcome,'message':str(report.longrepr) if report.failed else None})
def pytest_sessionfinish(session, exitstatus):
    Path(os.environ['CHECK_REPORT']).write_text(json.dumps({'exitCode':int(exitstatus),'collected':collected,'collectionFailures':collection,'reports':records},indent=2))
`;
const guest=String.raw`import hashlib, importlib.util, json, os, pathlib, platform, signal, subprocess, sys, time
root=pathlib.Path(sys.argv[1]); m=json.loads((root/'input.json').read_text()); out=root/'evidence'; out.mkdir(mode=0o700)
checks=[]; active=None; complete=False

def digest(p): return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def save(p,v): p.write_text(json.dumps(v,indent=2))
def environment(label):
    d=root/'isolated'/label
    for n in ('home','data','tmp','cache'): (d/n).mkdir(parents=True,exist_ok=True,mode=0o700)
    return {'PATH':str(root/'tools')+':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin','HOME':str(d/'home'),'DATA_DIR':str(d/'data'),'TMPDIR':str(d/'tmp')+'/','XDG_CACHE_HOME':str(d/'cache'),'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','CI':'true'}
def stop_owned():
    global active
    if active is not None and active.poll() is None:
        try: os.killpg(active.pid,signal.SIGTERM)
        except PermissionError: active.terminate()
        try: active.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: os.killpg(active.pid,signal.SIGKILL)
            except PermissionError: active.kill()
            active.wait(timeout=5)
    active=None
def run(label,args,cwd,timeout=120,extra=None):
    global active
    start=time.time(); env=environment(label); env.update(extra or {})
    log=out/(label+'.log'); rc=None; timed=False
    try:
        with log.open('wb') as f:
            active=subprocess.Popen(args,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
            try: rc=active.wait(timeout=timeout)
            except subprocess.TimeoutExpired: timed=True
    finally:
        stop_owned()
        entry={'name':label,'argv':args,'exitCode':rc,'timeout':timed,'startedAt':start,'finishedAt':time.time(),'logSha256':digest(log)}
        checks.append(entry);save(out/'checks.json',checks)
    print(json.dumps(entry),flush=True)
    return rc
try:
    assert platform.system()=='Darwin' and platform.machine()=='arm64'
    assert digest(root/'source.tar')==m['sourceArchiveSha256']
    assert digest(root/'runner.py')==m['guestRunnerSha256']
    assert digest(root/'checkpoint_plugin.py')==m['pluginSha256']
    checkout=root/'source';checkout.mkdir(mode=0o700);(root/'tools').mkdir(mode=0o700)
    assert run('extract',['/usr/bin/tar','-xf',str(root/'source.tar'),'-C',str(checkout)],root)==0
    for p,h in m['overlay'].items():
        incoming=root/pathlib.Path(p).name
        assert digest(incoming)==h
        target=checkout/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(incoming.read_bytes());assert digest(target)==h
    assert not (checkout/'scripts/mac/9router_hotswap.py').exists(), 'Controller exists: checkpoint is not the reviewed RED state'
    candidates=[]
    for p in [sys.executable, '/opt/homebrew/bin/python3','/usr/local/bin/python3','/usr/bin/python3']:
        if p not in candidates and pathlib.Path(p).exists(): candidates.append(p)
    selected=None
    for i,p in enumerate(candidates):
        if run('pytest-probe-'+str(i),[p,'-c','import pytest; print(pytest.__version__)'],root)==0: selected=p;break
    installed=False
    if selected is None:
        assert run('venv',[sys.executable,'-m','venv',str(root/'venv')],root)==0
        selected=str(root/'venv/bin/python3')
        assert run('pytest-install',[selected,'-m','pip','install','--disable-pip-version-check','pytest==8.4.2'],root,300)==0
        installed=True
    runtime={'devboxId':m['devboxId'],'instanceId':m['instanceId'],'os':platform.platform(),'machine':platform.machine(),'bootstrapPython':sys.version,'pythonExecutable':selected,'pythonSha256':digest(pathlib.Path(selected).resolve()),'installedPytest':installed}
    assert run('runtime',[selected,'-c','import sys,pytest,importlib.metadata,json;print(json.dumps({"python":sys.version,"pytest":pytest.__version__,"packages":{d.metadata["Name"]:d.version for d in importlib.metadata.distributions()}},indent=2))'],root)==0
    runtime['versionLogSha256']=digest(out/'runtime.log');save(out/'runtime.json',runtime)
    for p in m['overlay']:
        label=pathlib.Path(p).stem;report=out/(label+'.json')
        rc=run(label,[selected,'-m','pytest',p,'-q'],checkout,180,{'PYTHONPATH':str(root),'PYTEST_PLUGINS':'checkpoint_plugin','CHECK_REPORT':str(report)})
        assert report.exists(), 'pytest did not produce report'
        parsed=json.loads(report.read_text());assert parsed['exitCode']==rc
        assert parsed['collected'] and not parsed['collectionFailures'], 'Collection failed or zero tests'
        assert rc==1, 'Expected actual pytest RED (exit 1), not infrastructure or unexpected GREEN'
        assert not any('ModuleNotFoundError' in (r['message'] or '') for r in parsed['reports']), 'Dependency/import failure'
        failed=[r for r in parsed['reports'] if r['outcome']=='failed']
        passed=[r['nodeid'] for r in parsed['reports'] if r['phase']=='call' and r['outcome']=='passed']
        skipped=[r['nodeid'] for r in parsed['reports'] if r['outcome']=='skipped']
        checks[-1].update(collected=len(parsed['collected']),passed=len(passed),failed=sum(r['phase']=='call' for r in failed),errors=sum(r['phase']!='call' for r in failed),skipped=len(skipped),failedIdentities=[r['nodeid'] for r in failed],reportSha256=digest(report))
        if label=='test_9router_hotswap':
            assert failed and all(r['phase']=='setup' and 'Task 4 controller is missing' in r['message'] for r in failed), 'Hotswap RED is not uniformly the missing controller assertion'
            checks[-1]['cause']='Task 4 controller is missing; fixture assertion before import'
        else:
            checks[-1]['cause']='Task 4 deploy contracts; inspect individually bound report and log'
        save(out/'checks.json',checks)
    for p,h in m['overlay'].items(): assert digest(checkout/p)==h, 'Test overlay mutated'
    complete=True
except BaseException as e:
    save(out/'failure.json',{'type':type(e).__name__,'message':str(e)})
finally:
    stop_owned()
    save(out/'identity.json',{'head':m['head'],'sourceArchiveSha256':m['sourceArchiveSha256'],'overlay':m['overlay'],'guestRunnerSha256':m['guestRunnerSha256'],'pluginSha256':m['pluginSha256'],'devboxId':m['devboxId'],'instanceId':m['instanceId'],'completedRedRuns':complete,'scope':'Task 4 RED only; no controller implementation, lint, scans, or production qualification'})
    files={p.name:digest(p) for p in out.iterdir() if p.is_file()}
    save(out/'manifest.pending',{'head':m['head'],'instanceId':m['instanceId'],'files':files})
    os.replace(out/'manifest.pending',out/'manifest.json')
print(json.dumps({'completedRedRuns':complete}),flush=True)
sys.exit(0 if complete else 2)
`;
fs.mkdirSync(evidence,{recursive:true,mode:0o700});
const record={schema:1,runId,head,devboxId:id,devboxName:name,guestPath:remote,authorization,overlay,indexSha256:indexHash,preservedFiles:priorHashes,lifecycle:[],result:'incomplete',guestRunnerSha256:sha(Buffer.from(guest)),pluginSha256:sha(Buffer.from(plugin)),orchestratorSha256:sha(fs.readFileSync(new URL(import.meta.url))),scope:'Only Task 4 RED; no host validation',docs:'https://namespace.so/docs/reference/devbox-cli/stop; SDK metadata Fetch verified from installed 1.4.0 resources.js; documentation did not establish nonactivation'};
const save=()=>fs.writeFileSync(path.join(evidence,'host.json'),JSON.stringify(record,null,2),{mode:0o600});
const error=e=>({name:e?.name||'Error',message:String(e?.message||e).replace(/(?:nsct_|ghp_|github_pat_)[A-Za-z0-9_.-]+/g,'[REDACTED]')});
const observe=async phase=>{const b=await metadata.devboxes.get(id,{timeoutMs:20000});assert.equal(b.id,id);assert.equal(b.name,name);assert.equal(b.info.shape?.os,'macos');assert.equal(b.info.shape?.architecture,'arm64');const s={phase,id:b.id,name:b.name,state:b.info.state,instanceId:b.info.instanceId||null,shape:b.info.shape,at:new Date().toISOString()};record.lifecycle.push(s);save();return s;};
const cliRun=(label,args,timeout=120000)=>{const p=spawnSync(cli,args,{timeout,encoding:'utf8',maxBuffer:16*1024*1024});fs.writeFileSync(path.join(evidence,label+'.cli.log'),(p.stdout||'')+'\n'+(p.stderr||''),{mode:0o600});record.cli??=[];record.cli.push({label,args,exitCode:p.status,at:new Date().toISOString(),error:p.error?error(p.error):null});save();assert(!p.error&&p.status===0,'CLI failed: '+label);return p.stdout;};
let metadata,claim,owns=false,complete=false,interrupted=false;
const onSignal=()=>{interrupted=true;};
for(const s of ['SIGINT','SIGTERM','SIGHUP'])process.on(s,onSignal);
save();fs.writeFileSync(path.join(evidence,'guest-runner.py'),guest);fs.writeFileSync(path.join(evidence,'checkpoint_plugin.py'),plugin);fs.writeFileSync(path.join(evidence,'orchestrator.mjs'),fs.readFileSync(new URL(import.meta.url)));
try{
    immutable();git('archive','--format=tar','--output='+path.join(staging,'source.tar'),head);record.sourceArchiveSha256=sha(fs.readFileSync(path.join(staging,'source.tar')));save();
    metadata=createDevboxClient({connectionTimeoutMs:20000});
    try{claim=fs.openSync(claimPath,'wx',0o600);}catch(e){
        if(e.code!=='EEXIST')throw e;
        const bytes=fs.readFileSync(claimPath);let prior;try{prior=JSON.parse(bytes);}catch{throw new Error('Existing claim unreadable: ownership unknown');}
        record.existingClaim=prior;save();assert(Number.isSafeInteger(prior.pid)&&prior.pid>1,'Existing owner PID uncertain');
        let dead=false;try{process.kill(prior.pid,0);}catch(x){if(x.code==='ESRCH')dead=true;else throw new Error('Existing owner state uncertain');}assert(dead,'Existing owner is live');
        for(const phase of ['dead-claim-immediate','dead-claim-after-60-seconds']){if(phase.endsWith('60-seconds'))await delay(61000);const s=await observe(phase);assert(s.state==='stopped'&&!s.instanceId,'Dead claim compute not stopped');}
        assert(fs.readFileSync(claimPath).equals(bytes),'Existing claim changed');fs.writeFileSync(path.join(evidence,'recovered-claim.json'),JSON.stringify({previous:prior,bytesSha256:sha(bytes),observations:record.lifecycle},null,2));fs.unlinkSync(claimPath);claim=fs.openSync(claimPath,'wx',0o600);
    }
    fs.writeFileSync(claim,JSON.stringify({pid:process.pid,runId,head,evidence}));
    const before=await observe('before');assert(before.state==='stopped'&&!before.instanceId,'Refuse preexisting active compute');
    assert(!interrupted,'Interrupted before activation');owns=true;
    cliRun('activate',['exec',name,'--','/usr/bin/uname','-m'],180000);
    const activated=await observe('activated');assert(activated.state==='running'&&activated.instanceId,'Activation identity unavailable');record.instanceId=activated.instanceId;save();
    for(const p of tests){const bytes=fs.readFileSync(path.join(tree,p));assert.equal(sha(bytes),overlay[p]);fs.writeFileSync(path.join(staging,path.basename(p)),bytes);fs.writeFileSync(path.join(evidence,path.basename(p)+'.input'),bytes);}
    fs.writeFileSync(path.join(staging,'runner.py'),guest);fs.writeFileSync(path.join(staging,'checkpoint_plugin.py'),plugin);fs.writeFileSync(path.join(staging,'input.json'),JSON.stringify(record,null,2));
    fs.writeFileSync(path.join(evidence,'input.json'),fs.readFileSync(path.join(staging,'input.json')));
    cliRun('mkdir',['exec',name,'--','/bin/mkdir','-m','700',remote]);
    for(const file of ['source.tar','runner.py','checkpoint_plugin.py','input.json',...tests.map(p=>path.basename(p))])cliRun('upload-'+file,['upload',name,path.join(staging,file),remote+'/'+file]);
    immutable();assert(!interrupted,'Interrupted before guest start');
    const started=cliRun('guest-start',['exec','--detach',name,'--','/opt/homebrew/bin/python3',remote+'/runner.py',remote],90000);
    record.executionId=started.match(/\bexec_[a-z0-9]+\b/)?.[0];assert(record.executionId,'Missing execution ID');save();
    cliRun('guest-execution',['logs',name,record.executionId],600000);
    const current=await observe('before-export');assert(current.instanceId===record.instanceId&&current.state==='running','Runtime changed');
    cliRun('download-manifest',['download',name,remote+'/evidence/manifest.json',path.join(evidence,'manifest.json')],30000);
    const manifest=JSON.parse(fs.readFileSync(path.join(evidence,'manifest.json')));assert.equal(manifest.head,head);assert.equal(manifest.instanceId,record.instanceId);assert(Object.keys(manifest.files).length>0);
    for(const [file,h] of Object.entries(manifest.files)){assert(/^[A-Za-z0-9._-]+$/.test(file)&&/^[a-f0-9]{64}$/.test(h));assert(!fs.existsSync(path.join(evidence,file)),'Evidence filename collision');cliRun('download-'+file,['download',name,remote+'/evidence/'+file,path.join(evidence,file)],30000);assert.equal(sha(fs.readFileSync(path.join(evidence,file))),h);}
    const identity=JSON.parse(fs.readFileSync(path.join(evidence,'identity.json')));assert.equal(identity.guestRunnerSha256,record.guestRunnerSha256);assert.deepEqual(identity.overlay,overlay);assert.equal(identity.sourceArchiveSha256,record.sourceArchiveSha256);assert.equal(identity.instanceId,record.instanceId);complete=identity.completedRedRuns===true;immutable();record.preservedInputAndIndex=true;
}catch(e){record.failure=error(e);save();}
finally{
    // CLI child connections exited before stop. This SDK client performed Fetch only, never connection()/exec().
    if(owns){
        try{cliRun('stop',['stop',name,'--force'],120000);}catch(e){complete=false;record.stopFailure=error(e);save();}
        for(const phase of ['immediate','after-60-seconds']){
            if(phase==='after-60-seconds')await delay(61000);
            try{const s=await observe(phase);assert(s.state==='stopped'&&!s.instanceId,'Shutdown verification failed');}catch(e){complete=false;record[phase+'Failure']=error(e);save();}
        }
    }
    try{metadata?.close();}catch(e){complete=false;record.metadataCloseFailure=error(e);}
    try{immutable();record.preservedInputAndIndex=true;}catch(e){complete=false;record.preservationFailure=error(e);}
    record.interrupted=interrupted;record.result=complete&&!interrupted&&!record.stopFailure?'red-checkpoint-complete':'blocked-or-incomplete';save();
    if(claim!==undefined){fs.closeSync(claim);const observations=record.lifecycle.filter(x=>['immediate','after-60-seconds'].includes(x.phase));if(!owns||(observations.length===2&&observations.every(x=>x.state==='stopped'&&!x.instanceId)&&!record.stopFailure&&!record.immediateFailure&&!record['after-60-secondsFailure'])){const c=JSON.parse(fs.readFileSync(claimPath));assert.equal(c.runId,runId);fs.unlinkSync(claimPath);record.claimReleased=true;save();}}
    const hashes=Object.fromEntries(fs.readdirSync(evidence).filter(f=>fs.statSync(path.join(evidence,f)).isFile()).map(f=>[f,sha(fs.readFileSync(path.join(evidence,f)))]));fs.writeFileSync(path.join(evidence,'host-manifest.json'),JSON.stringify({files:hashes,at:new Date().toISOString()},null,2));
    for(const s of ['SIGINT','SIGTERM','SIGHUP'])process.removeListener(s,onSignal);
    console.log(JSON.stringify({evidence,result:record.result,overlay,lifecycle:record.lifecycle,failure:record.failure}));process.exitCode=record.result==='red-checkpoint-complete'?0:2;
}
