// Run: node tests/mac/9router_worker.check.cjs. Real workers, isolated fake releases.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { setTimeout: delay } = require('node:timers/promises');
const worker = path.resolve(__dirname, '../../scripts/mac/9router_worker.cjs');
const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), '9rw-')));
fs.chmodSync(root, 0o700);
const children = [];
const sockets = [];
const wait = async (fn, seconds = 5) => {
  const until = Date.now() + seconds * 1000;
  while (true) {
    const result = await fn();
    if (result) return result;
    assert.ok(Date.now() < until, 'test wait expired');
    await delay(25);
  }
};
function command(slot, op, raw, timeout = 4000) {
  assert.ok(['a', 'b'].includes(slot), 'invalid control slot');
  return new Promise((resolve, reject) => {
    const socket = net.connect(path.join(root, `${slot}.ctl`));
    let bytes = '';
    socket.setTimeout(timeout, () => socket.destroy(new Error('control timeout')));
    socket.on('error', reject);
    socket.on('connect', () => socket.end(raw ?? JSON.stringify({ op }) + '\n'));
    socket.on('data', chunk => { bytes += chunk; });
    socket.on('end', () => {
      try {
        assert.ok(Buffer.byteLength(bytes) <= 4096);
        const value = JSON.parse(bytes.trim());
        assert.equal(value.slot, slot);
        assert.ok(!value.error, value.error);
        assert.ok(Number.isSafeInteger(value.connections) && value.connections >= 0);
        resolve(value);
      } catch (e) { reject(e); }
    });
  });
}
function request(port, url) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path: url, agent: false }, res => {
      let body = '';
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => resolve(body));
      res.on('error', reject);
    });
    req.on('error', reject);
  });
}
function select(slot) {
  const temporary = path.join(root, 'next.sock');
  fs.symlinkSync(path.join(root, `${slot}.sock`), temporary);
  fs.renameSync(temporary, path.join(root, 'active.sock'));
}
async function makeConfig(slot, attachment = 'exports.attachResponsesWebSocket = () => {};') {
  const reservation = net.createServer();
  reservation.listen(0, '127.0.0.1');
  await once(reservation, 'listening');
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const release = path.join(root, '.9router', 'releases', slot);
  fs.mkdirSync(path.join(release, 'app'), { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(release, 'package.json'), JSON.stringify({ version: slot }));
  fs.copyFileSync(path.resolve(__dirname, '../../custom-server.js'), path.join(release, 'app', 'custom-server.js'));
  fs.mkdirSync(path.join(release, 'app', 'src/lib/db'), { recursive: true });
  fs.copyFileSync(path.resolve(__dirname, '../../src/lib/db/managed.cjs'), path.join(release, 'app', 'src/lib/db/managed.cjs'));
  fs.mkdirSync(path.join(release, 'app', 'handlers/responsesWs'), { recursive: true });
  const attachmentFile = path.join(release, 'app', 'handlers/responsesWs/index.js');
  if (attachment === null) fs.rmSync(attachmentFile, { force: true });
  else fs.writeFileSync(attachmentFile, attachment);
  fs.writeFileSync(path.join(release, 'app', 'server.js'), `
const http = require('http');
const fs = require('fs');
const path = require('path');
const managed = require('./src/lib/db/managed.cjs');
const statusHold = path.join(process.env.NINEROUTER_HOTSWAP_RUNTIME, process.env.NINEROUTER_SLOT + '.status-hold');
const send = process.send.bind(process);
const waiting = [];
let sealedStatusCount = 0;
process.send = (message, ...args) => {
  const hold = fs.existsSync(statusHold) ? fs.readFileSync(statusHold, 'utf8') : null;
  if (hold !== 'sealed') sealedStatusCount = 0;
  const sealed = hold === 'sealed' && ++sealedStatusCount >= 2;
  if (message.type === '9router-managed' && fs.existsSync(statusHold) &&
      fs.readFileSync(statusHold, 'utf8') === (sealed ? 'sealed' : 'open')) {
    waiting.push(() => send(message, ...args));
    fs.writeFileSync(statusHold + '.waiting', 'held');
    return true;
  }
  return send(message, ...args);
};
managed.workState().initialized = true;
process.once('SIGTERM', () => {
  const delayed = fs.existsSync(path.join(process.env.NINEROUTER_HOTSWAP_RUNTIME, process.env.NINEROUTER_SLOT + '.exit-delay'));
  setTimeout(() => process.exit(0), delayed ? 5500 : 0);
});
  const held = [];
const server = http.createServer(async (req, res) => {
  if (req.url === '/api/version') {
    try { await managed.awaitResponsesWsReady(); } catch { res.statusCode = 503; return res.end('unready'); }
    return res.end(JSON.stringify({currentVersion: process.env.NINEROUTER_SLOT}));
  }
  if (req.url === '/status-release') { fs.unlinkSync(statusHold); fs.unlinkSync(statusHold + '.waiting'); for (const finish of waiting.splice(0)) finish(); return res.end('ok'); }
  if (req.url === '/unknown') { managed.workState().unknown = true; return res.end('ok'); }
  if (req.url === '/known') { Object.defineProperty(managed.workState(), 'unknown', { value: false, writable: true, configurable: true, enumerable: true }); return res.end('ok'); }
  if (req.url === '/final-unknown') { Object.defineProperty(managed.workState(), 'unknown', { configurable: true, enumerable: true, get: () => !require('fs').existsSync(require('path').join(process.env.NINEROUTER_HOTSWAP_RUNTIME, process.env.NINEROUTER_SLOT + '.sock')) }); return res.end('ok'); }
  if (req.url === '/release') { for (const finish of held.splice(0)) finish(); return res.end('ok'); }
  if (req.url === '/cleanup') {
    const end = managed.beginWork('cleanup'); held.push(end);
    res.writeHead(200, {'Content-Type':'text/event-stream'}); res.write('data: start\\n\\n'); return;
  }
  if (req.url === '/stream') {
    res.writeHead(200, {'Content-Type':'text/event-stream'}); res.write('data: start\\n\\n'); held.push(() => res.end('data: done\\n\\n')); return;
  }
  if (req.url === '/handler') {
    res.write('start'); await new Promise(resolve => held.push(resolve)); return res.end('done');
  }
  res.end(process.env.NINEROUTER_SLOT);
});
server.on('upgrade', (req, socket) => {
  socket.on('end', () => socket.end());
  socket.write('HTTP/1.1 101 Switching Protocols\\r\\nConnection: Upgrade\\r\\nUpgrade: websocket\\r\\n\\r\\n');
  socket.on('data', async chunk => {
    const body = await (await fetch('http://127.0.0.1:' + process.env.PORT + '/version')).text();
    socket.write(body + ':' + chunk);
  });
});
server.listen(Number(process.env.PORT), '127.0.0.1');
`);
  const config = { slot, version: slot, release, port, runtime: root, dataDir: root };
  const file = path.join(root, `${slot}.json`);
  fs.writeFileSync(file, JSON.stringify(config), { mode: 0o600 });
  return { config, file };
}
async function launch(file, preload) {
  const child = spawn(process.execPath, [...(preload ? ['--require', preload] : []), worker, file], {
    env: { ...process.env, HOME: root, NINEROUTER_SKIP_BACKGROUND_REFRESH: '1', NINEROUTER_SKIP_RESPONSES_WS: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.push(child);
  let log = '';
  child.stdout.on('data', chunk => { log += chunk; });
  child.stderr.on('data', chunk => { log += chunk; });
  child.log = () => log;
  return child;
}
async function connect(slot, url) {
  const socket = net.connect(path.join(root, `${slot}.sock`));
  sockets.push(socket);
  let data = '';
  socket.on('data', chunk => { data += chunk; });
  socket.on('error', () => {});
  await once(socket, 'connect');
  socket.write(`GET ${url} HTTP/1.1\r\nHost: test.invalid\r\nConnection: ${url === '/ws' ? 'Upgrade' : 'close'}\r\n${url === '/ws' ? 'Upgrade: websocket\r\n' : ''}\r\n`);
  await wait(() => data.includes(url === '/ws' ? '101 Switching' : 'start'));
  return { socket, data: () => data };
}
async function main() {
  assert.ok(fs.existsSync(worker), 'Task 2 pinned worker missing');
  await assert.rejects(() => command('a', 'status'));
  assert.throws(() => command('invalid', 'stop'));
  const a = await makeConfig('a');
  const b = await makeConfig('b');
  const wa = await launch(a.file);
  await wait(async () => { try { return (await command('a', 'status')).mode === 'ready'; } catch { assert.equal(wa.exitCode, null, wa.log()); } });
  select('a');
  assert.equal((await command('a', 'status')).mode, 'active');
  const wb = await launch(b.file);
  await wait(async () => { try { return (await command('b', 'status')).mode === 'ready'; } catch { assert.equal(wb.exitCode, null, wb.log()); } });
  const before = await command('a', 'status');
  assert.notEqual(before.appPid, (await command('b', 'status')).appPid);
  const idle = net.connect(path.join(root, 'a.sock'));
  sockets.push(idle); idle.on('error', () => {});
  let idleResponse = '';
  idle.on('data', chunk => { idleResponse += chunk; });
  await once(idle, 'connect');
  idle.write('GET /version HTTP/1.1\r\nHost: test.invalid\r\nConnection: keep-alive\r\n\r\n');
  await wait(() => idleResponse.includes('200 OK'));
  const sse = await connect('a', '/stream');
  const ws = await connect('a', '/ws');
  await wait(async () => (await command('a', 'status')).connections === 3);
  select('b');
  await command('a', 'drain');
  await wait(() => idle.closed, 1);
  assert.equal(sse.socket.destroyed, false, 'draining idle sockets must preserve SSE');
  assert.equal(ws.socket.destroyed, false, 'draining idle sockets must preserve WebSocket');
  assert.equal(await request(b.config.port, '/version'), 'b');
  for (const turn of ['one', 'two', 'three']) {
    ws.socket.write(turn);
    await wait(() => ws.data().includes('a:' + turn));
  }
  await assert.rejects(() => command('a', 'stop'));
  const incomplete = net.connect(path.join(root, 'a.ctl'));
  sockets.push(incomplete);
  incomplete.on('error', () => {});
  incomplete.resume();
  await once(incomplete, 'connect');
  const receivedAt = Date.now();
  incomplete.write('{');
  const trickle = setInterval(() => incomplete.write(' '), 250);
  try {
    await wait(() => incomplete.closed, 7);
    assert.ok(Date.now() - receivedAt < 6500, 'trickled incomplete command obeys absolute receipt deadline');
  } finally { clearInterval(trickle); }
  assert.equal(sse.socket.destroyed, false, 'receipt deadline does not close healthy SSE');
  assert.equal(ws.socket.destroyed, false, 'receipt deadline does not close healthy WebSocket');
  ws.socket.write('after-receipt-deadline');
  await wait(() => ws.data().includes('a:after-receipt-deadline'));
  const racing = await connect('a', '/cleanup'); // Previously selected dial is admitted after switch.
  racing.socket.destroy();
  await request(a.config.port, '/release');
  await wait(() => sse.data().includes('done'));
  ws.socket.destroy();
  sse.socket.destroy();
  await wait(async () => (await command('a', 'status')).connections === 0);
  const handler = await connect('a', '/handler');
  handler.socket.destroy();
  await wait(async () => (await command('a', 'status')).connections === 0);
  assert.ok((await command('a', 'status')).appWork.handlers > 0, 'aborted response retains handler completion');
  await request(a.config.port, '/release');
  await wait(async () => (await command('a', 'status')).appWork.handlers === 0);
  const cleanup = await connect('a', '/cleanup');
  cleanup.socket.destroy();
  await wait(async () => (await command('a', 'status')).connections === 0);
  assert.ok((await command('a', 'status')).appWork.cleanup > 0);
  await delay(10500);
  await assert.rejects(() => command('a', 'stop'));
  await request(a.config.port, '/release');
  await request(a.config.port, '/unknown');
  await assert.rejects(() => command('a', 'stop'));
  await request(a.config.port, '/known');
  assert.equal((await command('a', 'resume')).mode, 'ready');
  select('a');
  assert.equal((await command('a', 'status')).mode, 'active');
  select('b');
  await command('a', 'drain');
  await assert.rejects(() => command('a', 'stop'));
  await delay(10500);
  await request(a.config.port, '/final-unknown');
  await assert.rejects(() => command('a', 'stop'));
  assert.equal(await request(a.config.port, '/known'), 'ok', 'unknown final count must preserve child');
  assert.ok(fs.lstatSync(path.join(root, 'a.sock')).isSocket(), 'unknown final count restores admission');
  const holdFile = path.join(root, 'a.status-hold');
  async function retirementRace(stage, race) {
    await delay(10500);
    // The sealed fixture must hold the final status, not the initial open-socket status.
    fs.writeFileSync(holdFile, stage);
    const retirement = command('a', 'stop');
    const refused = assert.rejects(retirement, /Retirement not quiescent/);
    await wait(() => fs.existsSync(holdFile + '.waiting'));
    await race();
    await request(a.config.port, '/status-release');
    await refused;
    assert.equal(wa.exitCode, null, 'race must retain owned app and wrapper');
    assert.equal(await request(a.config.port, '/version'), 'a');
    assert.ok(fs.lstatSync(path.join(root, 'a.sock')).isSocket(), 'race restores or preserves admission');
    await assert.rejects(() => command('a', 'stop'), /Retirement not quiescent/);
  }
  await retirementRace('open', async () => {
    const late = net.connect(path.join(root, 'a.sock'));
    sockets.push(late);
    let body = '';
    late.on('data', chunk => { body += chunk; });
    await once(late, 'connect');
    late.write('GET /version HTTP/1.1\r\nHost: test.invalid\r\nConnection: close\r\n\r\n');
    await once(late, 'close');
    const payload = body.slice(body.indexOf('\r\n\r\n') + 4);
    assert.ok(body.includes('200 OK') && (payload === 'a' || payload === '1\r\na\r\n0\r\n\r\n'), 'late accepted request completes during status wait');
  });
  await retirementRace('open', async () => { select('a'); });
  select('b');
  await retirementRace('sealed', async () => {
    select('a'); // Sealed target is now invalid: ownership proof must fail closed.
  });
  select('b');
  await retirementRace('sealed', async () => {
    process.kill(wa.pid, 'SIGSTOP'); // Exact owned fixture wrapper: interrupt the quiet observation clock.
    try { await delay(1250); } finally { process.kill(wa.pid, 'SIGCONT'); }
  });
  await delay(10500);
  fs.writeFileSync(path.join(root, 'a.exit-delay'), 'delay owned fixture shutdown');
  const stopped = await command('a', 'stop', undefined, 10000);
  assert.equal(stopped.mode, 'stopped', 'accepted stop survives >5s app exit wait without a receipt deadline');
  assert.equal(wa.exitCode, null, 'stopped wrapper must not exit and trigger KeepAlive');
  await assert.rejects(() => request(a.config.port, '/version'));
  await assert.rejects(() => command('b', 'status', '{bad}\n'));
  await assert.rejects(() => command('b', 'status', 'x'.repeat(4097) + '\n'));
  await assert.rejects(() => command('b', 'stop'));
  const bpid = (await command('b', 'status')).appPid;
  const crashControls = [];
  for (const input of ['', '{', JSON.stringify({ op: 'status' }) + '\n']) {
    const socket = net.connect(path.join(root, 'b.ctl'));
    sockets.push(socket); crashControls.push(socket);
    socket.on('error', () => {});
    socket.resume();
    await once(socket, 'connect');
    if (input.includes('\n')) fs.writeFileSync(path.join(root, 'b.status-hold'), 'open');
    if (input) socket.write(input);
  }
  await wait(() => fs.existsSync(path.join(root, 'b.status-hold.waiting')));
  const crashTrickle = setInterval(() => crashControls[1].write(' '), 100);
  try {
    process.kill(bpid, 'SIGKILL'); // Only the exact isolated fake-app PID, deliberate crash test.
    await wait(() => wb.exitCode !== null, 3);
    await wait(() => crashControls.every(socket => socket.closed), 1);
  } finally { clearInterval(crashTrickle); }
  assert.notEqual(wb.exitCode, 0, 'idle, trickled and accepted pending controls cannot retain failed wrapper');
  const crashEvidence = JSON.parse(fs.readFileSync(path.join(root, 'b.failed.json'), 'utf8'));
  assert.equal(crashEvidence.appPid, bpid);
  assert.equal(crashEvidence.signal, 'SIGKILL');
  assert.equal(fs.existsSync(path.join(root, 'b.sock')), false);
  assert.equal(fs.existsSync(path.join(root, 'b.ctl')), false);
  fs.unlinkSync(path.join(root, 'b.status-hold')); fs.unlinkSync(path.join(root, 'b.status-hold.waiting'));
  fs.unlinkSync(path.join(root, 'active.sock'));
  const candidate = await launch(b.file);
  await wait(async () => { try { return (await command('b', 'status')).mode === 'ready'; } catch { assert.equal(candidate.exitCode, null, candidate.log()); } });
  process.kill((await command('b', 'status')).appPid, 'SIGKILL');
  await wait(() => candidate.exitCode !== null);
  assert.notEqual(candidate.exitCode, 0, 'failed candidate must not silently stay ready');
  // An unrelated listener may claim the released reservation, but cannot prove the child owns it.
  const impostor = http.createServer((_req, res) => res.end(JSON.stringify({ currentVersion: 'b' })));
  await new Promise(resolve => impostor.listen(b.config.port, '127.0.0.1', resolve));
  const impersonated = await launch(b.file);
  await wait(() => impersonated.exitCode !== null);
  assert.notEqual(impersonated.exitCode, 0);
  assert.equal(fs.existsSync(path.join(root, 'b.sock')), false, 'impostor listener never exposes a bridge');
  await new Promise(resolve => impostor.close(resolve));
  const spawnFault = path.join(root, 'spawn-fault.cjs');
  const missingExecutable = path.join(root, 'missing-node-executable');
  fs.writeFileSync(spawnFault, `
const cp = require('node:child_process');
const spawn = cp.spawn;
cp.spawn = (_executable, args, options) => spawn(${JSON.stringify(missingExecutable)}, args, options);
`);
  fs.unlinkSync(path.join(root, 'b.failed.json'));
  const spawnFailedWorker = await launch(b.file, spawnFault);
  await wait(() => spawnFailedWorker.exitCode !== null);
  assert.notEqual(spawnFailedWorker.exitCode, 0, 'spawn failure refuses readiness');
  const spawnEvidence = JSON.parse(fs.readFileSync(path.join(root, 'b.failed.json'), 'utf8'));
  assert.equal(spawnEvidence.slot, 'b');
  assert.equal(spawnEvidence.version, 'b');
  assert.equal(spawnEvidence.appPid, null, 'unspawned child has no fabricated PID');
  assert.equal(spawnEvidence.error.code, 'ENOENT', 'real asynchronous spawn error retains controlled failure evidence');
  assert.equal(spawnEvidence.code, null);
  assert.equal(spawnEvidence.signal, null);
  assert.equal(fs.statSync(path.join(root, 'b.failed.json')).mode & 0o777, 0o600);
  assert.equal(fs.existsSync(path.join(root, 'b.sock')), false);
  assert.equal(fs.existsSync(path.join(root, 'b.ctl')), false);
  assert.equal(spawnFailedWorker.log().includes('Unhandled \'error\' event'), false);
  assert.equal((await command('a', 'status')).mode, 'stopped', 'spawn failure does not affect other owned slot');
  const attachmentStarted = path.join(root, 'ws-started');
  const attachmentRelease = path.join(root, 'ws-release');
  const delayed = await makeConfig('b', `
exports.attachResponsesWebSocket = async () => {
  const fs = require('fs'); const path = require('path');
  const root = process.env.NINEROUTER_HOTSWAP_RUNTIME;
  fs.writeFileSync(path.join(root, 'ws-started'), 'started');
  await new Promise(resolve => {
    const timer = setInterval(() => { if (fs.existsSync(path.join(root, 'ws-release'))) { clearInterval(timer); resolve(); } }, 10);
  });
};`);
  const delayedWorker = await launch(delayed.file);
  await wait(() => fs.existsSync(attachmentStarted));
  assert.equal(fs.existsSync(path.join(root, 'b.ctl')), false, 'attachment wait cannot expose ready worker controls');
  let versionReady = false;
  const versionPending = request(delayed.config.port, '/api/version').then(body => { versionReady = true; return body; });
  await delay(100);
  assert.equal(versionReady, false, 'private version waits for promise-returning attachment');
  fs.writeFileSync(attachmentRelease, 'release');
  assert.equal(JSON.parse(await versionPending).currentVersion, 'b');
  await wait(async () => { try { return (await command('b', 'status')).mode === 'ready'; } catch { assert.equal(delayedWorker.exitCode, null, delayedWorker.log()); } });
  delayedWorker.kill('SIGTERM'); await once(delayedWorker, 'exit');
  const homeModule = path.join(root, '.9router/lib/responses-ws/index.mjs');
  fs.mkdirSync(path.dirname(homeModule), { recursive: true });
  fs.writeFileSync(homeModule, `import fs from 'node:fs'; fs.writeFileSync(${JSON.stringify(path.join(root, 'home-loaded'))}, 'loaded'); export const attachResponsesWebSocket = () => {};`);
  for (const attachment of [null, 'exports.notAnAttachment = true;', 'exports.attachResponsesWebSocket = async () => { throw new Error("attachment failed"); };']) {
    const refusedConfig = await makeConfig('b', attachment);
    const refusedWorker = await launch(refusedConfig.file);
    await wait(async () => { try { return (await request(refusedConfig.config.port, '/api/version')) === 'unready'; } catch { assert.equal(refusedWorker.exitCode, null, refusedWorker.log()); } });
    assert.equal(fs.existsSync(path.join(root, 'b.ctl')), false, 'missing/invalid/failed attachment cannot expose readiness');
    assert.equal(fs.existsSync(path.join(root, 'home-loaded')), false, 'managed mode never imports home fallback');
    refusedWorker.kill('SIGTERM'); await once(refusedWorker, 'exit');
  }
  const invalid = path.join(root, 'invalid.json');
  fs.writeFileSync(invalid, JSON.stringify({ ...b.config, slot: 'bad' }), { mode: 0o600 });
  const bad = await launch(invalid);
  await wait(() => bad.exitCode !== null);
  assert.notEqual(bad.exitCode, 0);
  console.log('PASS: real pinned workers preserve SSE/WS/private self-fetch; late dials, cleanup, unknown work, quiet interval, resume, stop wrapper, crashes and invalid control/config');
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  for (const socket of sockets) socket.destroy();
  for (const child of children) {
    if (child.exitCode === null) { child.kill('SIGTERM'); await once(child, 'exit'); }
  }
  fs.rmSync(root, { recursive: true, force: true });
});
