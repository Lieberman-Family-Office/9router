// Run: NINEROUTER_TEST_PACKAGES=<installed dependency root> node tests/unit/pr52-archive-bindings.check.mjs
// Parse archived host code without executing its local or remote lifecycle actions.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const installed=process.env.NINEROUTER_TEST_PACKAGES;
assert.ok(installed,'Installed parser dependency root required');
const packageFile=path.resolve(installed,'package.json');
assert.ok(fs.existsSync(packageFile),`Installed dependency manifest missing: ${packageFile}`);
const require=createRequire(packageFile);
const {parse}=require('@babel/parser');
const traverse=require('@babel/traverse').default;
const root=fileURLToPath(new URL('../../',import.meta.url));
const ledger=JSON.parse(fs.readFileSync(path.join(root,'pr52-quality-outcomes.json'),'utf8'));
const round=ledger.reviewRoundFour;
assert.ok(round.findingHead&&round.verifiedFixCommit&&round.findingHead!==round.verifiedFixCommit,'Finding and fix provenance must be distinct');
const gitFile=(commit,name)=>execFileSync('git',['show',`${commit}:${name}`],{cwd:root,encoding:'utf8'});
const greenName='docs/superpowers/evidence/task-4-green-959c577a870a-8a365ef0/orchestrator.mjs';
const redName='docs/superpowers/evidence/task-4-red-959c577a870a-652526fc/orchestrator.mjs';
const beforeGreen=gitFile(round.findingHead,greenName),fixedGreen=gitFile(round.verifiedFixCommit,greenName);
assert.ok(beforeGreen.includes('nonce: randomUUID()'),'Finding head must reproduce the reported nonce fault');
assert.ok(!fixedGreen.includes('nonce: randomUUID()')&&fixedGreen.includes("nonce: (await import('node:crypto')).randomUUID()"),'Verified fix commit must close the nonce fault');
const beforeRed=gitFile(round.findingHead,redName),fixedRed=gitFile(round.verifiedFixCommit,redName);
assert.ok(beforeRed.indexOf("staging=fs.mkdtempSync")<beforeRed.indexOf("throw new Error('Historical transcript"),'Finding head has the prefix staging effect');
assert.ok(fixedRed.indexOf("staging=fs.mkdtempSync")>fixedRed.indexOf("throw new Error('Historical transcript"),'Fix commit moves staging after refusal');
const base=new URL('../../docs/superpowers/evidence/',import.meta.url);
const green=fs.readFileSync(new URL('task-4-green-959c577a870a-8a365ef0/orchestrator.mjs',base),'utf8');
const red=fs.readFileSync(new URL('task-4-red-959c577a870a-652526fc/orchestrator.mjs',base),'utf8');
function unbound(text){
 const failures=[];
 traverse(parse(text,{sourceType:'module'}),{ReferencedIdentifier(p){
  if(['randomUUID','os','staging','remote'].includes(p.node.name)&&!p.scope.hasBinding(p.node.name))failures.push(p.node.name);
 }});
 return failures;
}
assert.deepEqual(unbound(green),[],'GREEN archived host identifiers must resolve');
assert.deepEqual(unbound(red),[],'RED archived host identifiers must resolve');
const nonce="nonce: (await import('node:crypto')).randomUUID()";
assert.equal(green.split(nonce).length,2,'Nonce dynamic import subject must be unique');
assert.ok(unbound(green.replace(nonce,'nonce: randomUUID()')).includes('randomUUID'),'Counterfactual missed the reported nonce failure');
function directPrefixMkdtempCalls(text){
 const ast=parse(text,{sourceType:'module'});
 const refusal=ast.program.body.findIndex(node=>node.type==='ThrowStatement');
 assert.ok(refusal>=0,'Historical refusal missing');
 let directCalls=0;
 for(const node of ast.program.body.slice(0,refusal))traverse(node,{noScope:true,CallExpression(p){if(p.node.callee.type==='MemberExpression'&&!p.node.callee.computed&&p.node.callee.property.name==='mkdtempSync')directCalls++;}});
 return directCalls;
}
// ponytail: this assertion covers direct, non-computed .mkdtempSync calls only; use import/dataflow resolution before claiming alias/computed coverage.
assert.equal(directPrefixMkdtempCalls(red),0,'Disabled prefix has no direct non-computed .mkdtempSync call');
assert.equal(directPrefixMkdtempCalls('fs.mkdtempSync("fixture"); throw new Error("refuse");'),1);
assert.equal(directPrefixMkdtempCalls('fs["mkdtempSync"]("fixture"); throw new Error("refuse");'),0,'Computed calls are outside the declared syntax scope');
assert.equal(directPrefixMkdtempCalls('const temp=fs.mkdtempSync; temp("fixture"); throw new Error("refuse");'),0,'Aliases are outside the declared syntax scope');
assert.equal(directPrefixMkdtempCalls('throw new Error("refuse"); fs.mkdtempSync("fixture");'),0);
console.log('PASS: two archived host symbol scopes; nonce counterfactual refused; zero direct non-computed .mkdtempSync calls in disabled prefix (aliases/computed calls not measured)');
