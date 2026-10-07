// Run: NINEROUTER_TEST_PACKAGES=<installed dependency root> node tests/unit/pr52-archive-bindings.check.mjs
// Parse archived host code without executing its local or remote lifecycle actions.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const installed=process.env.NINEROUTER_TEST_PACKAGES;
assert.ok(installed,'Installed parser dependency root required');
const require=createRequire(path.join(installed,'package.json'));
const {parse}=require('@babel/parser');
const traverse=require('@babel/traverse').default;
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
const ast=parse(red,{sourceType:'module'});
const refusal=ast.program.body.findIndex(node=>node.type==='ThrowStatement');
assert.ok(refusal>=0,'Historical refusal missing');
let prefixEffects=0;
for(const node of ast.program.body.slice(0,refusal))traverse(node,{noScope:true,CallExpression(p){if(p.node.callee.type==='MemberExpression'&&p.node.callee.property.name==='mkdtempSync')prefixEffects++;}});
assert.equal(prefixEffects,0,'Disabled archived host must not create a staging directory');
console.log('PASS: two archived host symbol scopes; unbound nonce counterfactual refused; disabled prefix has no staging creation');
