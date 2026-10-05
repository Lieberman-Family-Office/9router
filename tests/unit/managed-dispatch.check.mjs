// Import-aware AST census. Uses the already-installed parser; never downloads packages.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const root = path.resolve(import.meta.dirname, '../..');
const installed = process.env.NINEROUTER_TEST_PACKAGES;
assert.ok(installed, 'NINEROUTER_TEST_PACKAGES must name installed test dependencies');
const { parse } = createRequire(path.join(installed, 'package.json'))('@babel/parser');
const modules = new Map();
const scopes = ['open-sse', 'src/sse', 'src/app/api'];
function walk(node, visit) {
  if (!node || typeof node !== 'object') return;
  if (node.type) visit(node);
  for (const [key, value] of Object.entries(node)) {
    if (['loc', 'start', 'end', 'extra'].includes(key)) continue;
    if (Array.isArray(value)) for (const item of value) walk(item, visit);
    else if (value && typeof value === 'object') walk(value, visit);
  }
}
function resolve(source, file) {
  if (source.startsWith('.')) return path.resolve(path.dirname(file), source);
  if (source.startsWith('open-sse/')) return path.join(root, source);
  if (source.startsWith('@/')) return path.join(root, 'src', source.slice(2));
  return null;
}
function load(file, text = fs.readFileSync(file, 'utf8')) {
  const ast = parse(text, { sourceType: 'unambiguous', plugins: ['jsx'] });
  const mod = { file, ast, imports: new Map(), functions: new Map(), exports: new Map() };
  walk(ast, node => {
    if (node.type === 'ImportDeclaration') for (const spec of node.specifiers) {
      mod.imports.set(spec.local.name, { file: resolve(node.source.value, file), name: spec.imported?.name || 'default' });
    }
    if (node.type === 'ExportNamedDeclaration' && node.source) for (const spec of node.specifiers) {
      mod.exports.set(spec.exported.name, { file: resolve(node.source.value, file), name: spec.local.name });
    }
    if (node.type === 'FunctionDeclaration') mod.functions.set(node.id.name, node);
    if (node.type === 'VariableDeclarator' && ['ArrowFunctionExpression', 'FunctionExpression'].includes(node.init?.type)) mod.functions.set(node.id.name, node.init);
    if (node.type === 'ClassMethod') mod.functions.set(node.key.name, node);
  });
  return mod;
}
function census(directory) {
  for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, item.name);
    if (item.isDirectory()) census(file);
    else if (item.isFile() && /\.[cm]?js$/.test(file)) modules.set(file, load(file));
  }
}
for (const scope of scopes) census(path.join(root, scope));
function binding(mod, name, seen = new Set()) {
  const key = `${mod.file}:${name}`;
  if (seen.has(key)) return null;
  seen.add(key);
  if (mod.functions.has(name)) return { mod, name, node: mod.functions.get(name) };
  const imported = mod.imports.get(name) || mod.exports.get(name);
  if (!imported?.file) return null;
  const next = modules.get(imported.file) || modules.get(imported.file + '.js') || modules.get(path.join(imported.file, 'index.js'));
  return next ? binding(next, imported.name, seen) : null;
}
// Recognize the actual managed environment comparison, not comments or a substring.
function managedCondition(node) {
  if (!['BinaryExpression'].includes(node?.type) || !['===', '!=='].includes(node.operator)) return null;
  const member = node.left;
  if (member?.type !== 'MemberExpression' || member.property?.name !== 'NINEROUTER_MANAGED_WORKER' ||
      member.object?.property?.name !== 'env' || member.object?.object?.name !== 'process' || node.right?.value !== '1') return null;
  return node.operator === '===';
}
let checkedCalls = 0;
let coordinatedIssuers = 0;
function verify(mod, node, coordinated = false, trail = new Set()) {
  if (!node || typeof node !== 'object') return false;
  if (node.type === 'IfStatement') {
    const condition = managedCondition(node.test);
    if (condition !== null) return verify(mod, condition ? node.consequent : node.alternate, coordinated, trail);
  }
  if (node.type === 'ConditionalExpression') {
    const condition = managedCondition(node.test);
    if (condition !== null) return verify(mod, condition ? node.consequent : node.alternate, coordinated, trail);
  }
  if (node.type === 'BlockStatement') {
    for (const statement of node.body) if (verify(mod, statement, coordinated, trail)) return true;
    return false;
  }
  if (node.type === 'CallExpression') {
    const name = node.callee.type === 'Identifier' ? node.callee.name
      : node.callee.object?.type === 'ThisExpression' ? node.callee.property.name : null;
    const target = name && binding(mod, name);
    const dedup = target?.name === 'dedupRefresh' && target.mod.file === path.join(root, 'open-sse/services/tokenRefresh/dedup.js');
    if (dedup) {
      assert.ok(['ArrowFunctionExpression', 'FunctionExpression'].includes(node.arguments[2]?.type), 'Dedup must wrap the issuer callback');
      verify(mod, node.arguments[2].body, true, trail);
      coordinatedIssuers++;
      return false;
    }
    // All provider network issuance must be dominated by the imported dedup callback.
    if (mod.file.endsWith('/tokenRefresh/providers.js') && ['fetch', 'proxyAwareFetch'].includes(name)) {
      assert.ok(coordinated, `Uncoordinated provider issuance: ${path.relative(root, mod.file)}:${node.loc.start.line}`);
    }
    if (target && target.name !== 'dedupRefresh') {
      const key = `${target.mod.file}:${target.name}:${coordinated}`;
      if (!trail.has(key)) {
        const next = new Set(trail).add(key);
        verify(target.mod, target.node.body, coordinated, next);
      }
    }
    // A raw legacy issuance anywhere on a reachable managed refresh path is forbidden.
    if (!coordinated && ['fetch', 'proxyAwareFetch'].includes(name)) {
      // Service-account JWT minting does not rotate a shared refresh token.
      let serviceAccountGrant = false;
      walk(node, child => { if (child.type === 'StringLiteral' && child.value === 'urn:ietf:params:oauth:grant-type:jwt-bearer') serviceAccountGrant = true; });
      if (serviceAccountGrant) return false;
      assert.fail(`Managed dispatch reaches raw refresh grant: ${path.relative(root, mod.file)}:${node.loc.start.line}`);
    }
  }
  for (const [key, value] of Object.entries(node)) {
    if (['loc', 'start', 'end', 'extra'].includes(key)) continue;
    if (Array.isArray(value)) for (const item of value) verify(mod, item, coordinated, trail);
    else if (value && typeof value === 'object') verify(mod, value, coordinated, trail);
  }
  return node.type === 'ReturnStatement' || node.type === 'ThrowStatement';
}
for (const mod of modules.values()) {
  if (mod.file.endsWith('/tokenRefresh/dedup.js')) continue;
  // Every imported refresh dispatch resolves its real symbol, including aliases and re-exports.
  for (const [local, imported] of mod.imports) {
    if (!/^_?refresh(?:Access|Provider|TokenBy|Codex|Google|GitHub|Copilot|Claude|Kiro|Iflow|Xai|Kimi|Cline|Codebuddy|Trae)/.test(imported.name)) continue;
    const target = binding(mod, local);
    assert.ok(target, `Unresolved refresh import: ${path.relative(root, mod.file)}:${local}`);
    walk(mod.ast, node => {
      if (node.type === 'CallExpression' && node.callee.type === 'Identifier' && node.callee.name === local) {
        checkedCalls++;
        verify(target.mod, target.node.body);
      }
    });
  }
  // Legacy raw helpers are reachable only through these guarded executor/test dispatches.
  for (const name of ['refreshCredentials', 'refreshOAuthToken', 'refreshGitHubToken', 'refreshCopilotToken']) {
    if (mod.file.includes('/executors/') || mod.file.endsWith('/test/testUtils.js')) {
      const target = mod.functions.get(name);
      if (target) { checkedCalls++; verify(mod, target.body); }
    }
  }
}
assert.ok(checkedCalls > 30 && coordinatedIssuers > 10, 'Dispatch census did not evaluate enough subjects');
// Parser counterfactuals prove that alias spelling and managed guard removal cannot hide issuance.
const fixture = load(path.join(root, 'open-sse/services/tokenRefresh/providers.js'),
  'export async function refreshBad(){ return fetch("fake", {body: {grant_type: "refresh_token"}}); }');
assert.throws(() => verify(fixture, fixture.functions.get('refreshBad').body), /Uncoordinated/);
const alias = load(path.join(root, 'open-sse/fixture.js'),
  'import {refreshAccessToken as renamed} from "./services/tokenRefresh/providers.js"; export function dispatch(){return renamed();}');
assert.equal(binding(alias, 'renamed').name, 'refreshAccessToken');
// Mutate an AST mirror, never checkout source: removing the real managed executor branch must go RED.
const defaultFile = path.join(root, 'open-sse/executors/default.js');
const mirror = load(defaultFile);
const dispatch = mirror.functions.get('refreshCredentials');
assert.equal(managedCondition(dispatch.body.body[0].test), true);
verify(mirror, dispatch.body);
dispatch.body.body.shift();
assert.throws(() => verify(mirror, dispatch.body), /raw refresh grant/);
console.log(`GREEN: AST/import-aware census read ${modules.size} files, checked ${checkedCalls} dispatches and ${coordinatedIssuers} coordinated issuer paths; raw-issuance counterfactual refused`);
