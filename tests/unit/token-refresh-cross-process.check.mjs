import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const self = fileURLToPath(import.meta.url);
if (process.argv[2] === 'child') {
  const { dedupRefresh } = await import('../../open-sse/services/tokenRefresh/dedup.js');
  const token = process.argv[3];
  const provider = process.argv[4] || 'fake-issuer';
  try {
    const result = await dedupRefresh(provider, token, async () => {
      process.send({ op: 'issue' });
      const response = await new Promise(resolve => process.once('message', resolve));
      return provider === 'copilot' ? { token: 'fake-copilot', expiresAt: response.expiry }
        : { accessToken: 'fake-access', refreshToken: `fake-rotated-${token}` };
    });
    process.send({ op: 'result', result });
  } catch {
    process.send({ op: 'refused' });
  }
} else {
  const root = mkdtempSync(path.join(tmpdir(), '9r-refresh-'));
  const privateDir = path.join(root, 'private');
  mkdirSync(privateDir, { mode: 0o700 });
  const store = path.join(privateDir, 'refresh.sqlite');
  let calls = 0;
  const children = new Set();
  function child(token, hold = false, provider = 'fake-issuer', expiry = Math.floor(Date.now() / 1000) + 3600) {
    const proc = fork(self, ['child', token, provider], {
      env: { ...process.env, HOME: root, DATA_DIR: root,
        NINEROUTER_MANAGED_WORKER: '1', NINEROUTER_HOTSWAP_REFRESH_DB: store,
        NINEROUTER_HOTSWAP_REFRESH_WAIT_MS: '300' },
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
    });
    children.add(proc);
    let output = '';
    proc.stdout.on('data', data => { output += data; });
    proc.stderr.on('data', data => { output += data; });
    let issued;
    const issuing = new Promise(resolve => { issued = resolve; });
    const result = new Promise((resolve, reject) => {
      const deadline = setTimeout(() => { proc.kill(); reject(new Error('child deadline')); }, 5000);
      proc.on('message', message => {
        if (message.op === 'issue') {
          calls++;
          issued();
          if (!hold) setTimeout(() => proc.connected && proc.send({ op: 'respond', expiry }), 40);
        } else {
          clearTimeout(deadline);
          resolve(message);
        }
      });
      proc.on('exit', () => { children.delete(proc); clearTimeout(deadline); });
      proc.on('error', reject);
    });
    return { proc, result, issuing, output: () => output };
  }
  try {
    // Before implementation, the actual old primitive needs no coordination file.
    // After implementation, enrollment explicitly creates it; workers never do.
    const managed = await import('../../src/lib/db/managed.cjs').catch(() => null);
    if (managed) managed.default.enrollRefreshStore(store);
    const a = child('fake-old');
    const b = child('fake-old');
    const [first, second] = await Promise.all([a.result, b.result]);
    assert.equal(calls, 1, 'cross-process issuer calls must equal one');
    assert.equal(first.op, 'result');
    assert.deepEqual(first, second);
    const late = await child('fake-old').result;
    assert.deepEqual(late, first);
    assert.equal(calls, 1, 'completed result must survive child restarts');
    assert.ok(Number.isSafeInteger(first.result.refreshGenerations.oauth));
    const doomed = child('fake-crash', true);
    await doomed.issuing;
    doomed.proc.kill('SIGTERM');
    await new Promise(resolve => doomed.proc.once('exit', resolve));
    const successor = await child('fake-crash').result;
    assert.equal(successor.op, 'refused', 'uncertain owner death must refuse replay');
    assert.equal(calls, 2, 'owner death must not send another issuer call');
    for (const proc of [a, b]) assert.ok(!proc.output().includes('fake-access'), 'credential output forbidden');
    const expired = await child('fake-gh', false, 'copilot', 1).result;
    const beforeRenewal = calls;
    const [renewedA, renewedB] = await Promise.all([
      child('fake-gh', false, 'copilot').result, child('fake-gh', false, 'copilot').result,
    ]);
    assert.equal(calls, beforeRenewal + 1, 'expired Copilot replacement must serialize across processes');
    assert.deepEqual(renewedA, renewedB);
    assert.ok(renewedA.result.refreshGenerations.copilot > expired.result.refreshGenerations.copilot);
    console.log('GREEN: two-process single-use refresh, durable result/generation, owner-death refusal, expired Copilot replacement');
  } finally {
    for (const proc of children) proc.kill('SIGTERM');
    rmSync(root, { recursive: true, force: true });
  }
}
