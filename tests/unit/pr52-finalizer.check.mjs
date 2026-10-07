// Run: node tests/unit/pr52-finalizer.check.mjs. Evaluates only the cleanup block with isolated stubs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../../docs/superpowers/evidence/task-2-retry-f76093a4f8ec-de12e533/orchestrator.mjs', import.meta.url), 'utf8');
const start = source.indexOf('    // Never act by name on an unverified replacement instance.');
const end = source.indexOf('  try { client?.close(); metadata?.close(); }', start);
assert.ok(start >= 0 && end > start, 'Finalizer subject missing');
const block = source.slice(start, end);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('metadata', 'record', 'matches', 'safeError', 'save', 'cliRun', 'spawnSync', 'client', 'delay', 'args', 'remote', 'cli',
  `let complete=true; const assert=()=>{}; assert.equal=(a,b)=>{if(a!==b)throw new Error('assertion mismatch')}; const snapshot=box=>({state:box.info.state,instanceId:box.info.instanceId}); { ${block} return {complete,record};`);
for (const mode of ['retry-success', 'unavailable', 'replacement']) {
  let lookups=0, effects=0;
  const record={instanceId:'owned',lifecycle:[]};
  const metadata={devboxes:{get:async()=>{
    lookups++;
    if (mode==='unavailable' || (mode==='retry-success' && lookups===1)) throw new Error('isolated transport');
    return {id:'box',info:{instanceId: mode==='replacement' ? 'other' : lookups<=2 ? 'owned' : null,state:lookups<=2?'running':'stopped'}};
  }},close(){}};
  const result=await run(metadata,record,box=>assert.equal(box.id,'box'),error=>({message:error.message}),()=>{},()=>{effects++;},()=>{effects++;return {status:0};},{close(){}},async()=>{}, {'devbox-id':'box','devbox-name':'fixture'},'/isolated','fixture-cli');
  if(mode==='retry-success') {assert.ok(effects>=2);assert.equal(result.complete,true);}
  else {assert.equal(effects,0);assert.equal(result.complete,false);assert.ok(result.record.cleanupOwnershipFailure);}
}
console.log('PASS: bounded cleanup metadata retry recovers transport failure; unavailable/replacement ownership has zero guest effects and preserves failure receipt');
