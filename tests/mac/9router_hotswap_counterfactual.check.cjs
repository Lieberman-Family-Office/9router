// Run: node tests/mac/9router_hotswap_counterfactual.check.cjs (installed Caddy; no app dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const files = ['tests/mac/9router_hotswap.check.cjs', 'scripts/mac/templates/9router.Caddyfile',
  'tests/mac/9router_worker.check.cjs', 'scripts/mac/9router_worker.cjs', 'custom-server.js', 'src/lib/db/managed.cjs'];
const checkout = path.resolve(__dirname, '../..');
const originals = files.map((file) => fs.readFileSync(path.join(checkout, file)));
const mirror = fs.mkdtempSync(path.join(os.tmpdir(), '9r-counterfactual-'));
fs.chmodSync(mirror, 0o700);
try {
  for (const [index, file] of files.entries()) {
    const target = path.join(mirror, file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, originals[index]);
    assert.deepEqual(fs.readFileSync(target), originals[index], `mirror differs: ${file}`);
  }
  const run = () => {
    const result = spawnSync(process.execPath, [files[0]], {
      cwd: mirror, encoding: 'utf8', timeout: 45000, maxBuffer: 1024 * 1024,
    });
    assert.equal(result.error, undefined, 'mirror check did not complete');
    assert.equal(result.signal, null, 'mirror check was terminated');
    return result;
  };
  const green = run();
  assert.equal(green.status, 0, green.stdout + green.stderr);
  console.log('GREEN: unchanged byte-identical mirror passes the original fake-app check');
  process.stdout.write(green.stdout);

  const replacement = 'fs.renameSync(temporary, active);';
  const source = originals[0].toString('utf8');
  assert.equal(source.split(replacement).length, 2, 'atomic replacement must occur exactly once');
  fs.writeFileSync(path.join(mirror, files[0]), source.replace(replacement, 'void 0;'));
  const red = run();
  assert.equal(red.status, 1, red.stdout + red.stderr);
  assert.match(red.stderr, /AssertionError \[ERR_ASSERTION\]: new HTTP requests must route to B after switching/);
  assert.match(red.stderr, /code: 'ERR_ASSERTION'/);
  assert.match(red.stderr, /actual: 'a'/);
  assert.match(red.stderr, /expected: 'b'/);
  assert.match(red.stderr, /operator: 'strictEqual'/);
  console.log('RED: removing only the mirror atomic replacement fails the B-routing assertion');
  process.stdout.write(red.stderr);
  for (const [index, file] of files.entries()) {
    assert.deepEqual(fs.readFileSync(path.join(checkout, file)), originals[index], `checkout changed: ${file}`);
  }
  assert.deepEqual(fs.readFileSync(path.join(mirror, files[1])), originals[1], 'mirror template changed');
  console.log('PASS: routing counterfactual detected; original check and template remain unchanged');
} finally {
  fs.rmSync(mirror, { recursive: true, force: true });
}
