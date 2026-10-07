// Run the same route tests on a temporary mirror, then restore only baseline source in that mirror.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { stripVTControlCharacters } from 'node:util';
const root = path.resolve(import.meta.dirname, '../..');
const installed = process.env.NINEROUTER_TEST_PACKAGES;
assert.ok(installed, 'NINEROUTER_TEST_PACKAGES must name installed dependencies');
const require = createRequire(path.join(installed, 'package.json'));
const baseline = '245de4a9df6e62a3134d67c30c017a2ef6b43376';
assert.equal(spawnSync('git', ['cat-file', '-e', `${baseline}^{commit}`], { cwd: root }).status, 0,
  `Counterfactual baseline ${baseline} is unavailable; fetch PR 52 history before running this proof`);
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), '9r-review-mirror-'));
try {
  for (const directory of ['src', 'open-sse', 'tests/unit']) fs.cpSync(path.join(root, directory), path.join(temporary, directory), { recursive: true });
  fs.copyFileSync(path.join(root, 'package.json'), path.join(temporary, 'package.json'));
  const aliases = [
    { find: 'open-sse', replacement: path.join(temporary, 'open-sse') },
    { find: '@', replacement: path.join(temporary, 'src') },
    ...['uuid', 'sql.js', 'undici'].map(name => ({ find: name, replacement: require.resolve(name) })),
    { find: 'vitest', replacement: path.join(installed, 'tests/node_modules/vitest/dist/index.js') },
  ];
  const config = path.join(temporary, 'vitest.config.mjs');
  fs.writeFileSync(config, `export default ${JSON.stringify({ root: temporary, test: { environment: 'node' }, resolve: { alias: aliases } })};`);
  const run = () => spawnSync(process.execPath, [path.join(installed, 'tests/node_modules/vitest/vitest.mjs'), 'run',
    '--config', config, 'tests/unit/managed-review-batch.test.js', '--reporter=verbose', '--silent'],
  { cwd: temporary, encoding: 'utf8', timeout: 30000 });
  const green = run();
  assert.equal(green.status, 0, `unchanged mirror must pass: ${green.stdout}\n${green.stderr}`);
  const files = ['open-sse/utils/proxyFetch.js', 'open-sse/services/tokenRefresh/dedup.js', 'src/lib/db/managed.cjs',
    'src/lib/db/repos/connectionsRepo.js', 'src/app/api/usage/[connectionId]/route.js', 'src/app/api/translator/send/route.js',
    'src/app/api/providers/[id]/route.js', 'open-sse/executors/github.js', 'src/sse/services/tokenRefresh.js',
    'open-sse/services/oauthCredentialManager.js'];
  for (const file of files) fs.writeFileSync(path.join(temporary, file), execFileSync('git', ['show', `${baseline}:${file}`], { cwd: root }));
  const red = run();
  assert.equal(red.status, 1, 'baseline mirror must fail assertions, not launch or time out');
  const output = stripVTControlCharacters(red.stdout + red.stderr);
  for (const subject of ['example.invalid', 'cloudcode-pa.googleapis.com', 'repeatable Copilot', 'usage real GET',
    'translator actual POST', 'provider actual PUT', 'reauth fences', 'durable completion']) {
    assert.ok(output.includes(`FAIL  tests/unit/managed-review-batch.test.js > ${subject}`) ||
      output.split('\n').some(line => line.includes('FAIL  tests/unit/managed-review-batch.test.js >') && line.includes(subject)),
    `baseline must fail the specific assertion: ${subject}`);
  }
  assert.ok(output.split('\n').some(line => line.includes('✓') && line.includes('uncertain and pending Copilot exchanges are never reclaimed')),
    `pending/uncertain safety must remain green in the baseline: ${output}`);
  console.log('GREEN: unchanged mirror passes; RED: baseline mirror fails all eight named review mechanisms while pending/uncertain safety passes');
} finally { fs.rmSync(temporary, { recursive: true, force: true }); }
