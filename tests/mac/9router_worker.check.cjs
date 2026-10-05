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
function command(slot, op, raw) {
  assert.ok(['a', 'b'].includes(slot), 'invalid control slot');
  return new Promise((resolve, reject) => {
    const socket = net.connect(path.join(root, `${slot}.ctl`));
    let bytes = '';
    socket.setTimeout(4000, () => socket.destroy(new Error('control timeout')));
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
async function makeConfig(slot) {
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
  fs.writeFileSync(path.join(release, 'app', 'server.js'), `
const http = require('http');
const managed = require('./src/lib/db/managed.cjs');
managed.workState().initialized = true;
const held = [];
const server = http.createServer(async (req, res) => {
  if (req.url === '/api/version') return res.end(JSON.stringify({currentVersion: process.env.NINEROUTER_SLOT}));
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
async function launch(file) {
  const child = spawn(process.execPath, [worker, file], {
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
  const sse = await connect('a', '/stream');
  const ws = await connect('a', '/ws');
  await wait(async () => (await command('a', 'status')).connections === 2);
  select('b');
  await command('a', 'drain');
  assert.equal(await request(b.config.port, '/version'), 'b');
  for (const turn of ['one', 'two', 'three']) {
    ws.socket.write(turn);
    await wait(() => ws.data().includes('a:' + turn));
  }
  await assert.rejects(() => command('a', 'stop'));
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
  const stopped = await command('a', 'stop');
  assert.equal(stopped.mode, 'stopped');
  assert.equal(wa.exitCode, null, 'stopped wrapper must not exit and trigger KeepAlive');
  await assert.rejects(() => request(a.config.port, '/version'));
  await assert.rejects(() => command('b', 'status', '{bad}\n'));
  await assert.rejects(() => command('b', 'status', 'x'.repeat(4097) + '\n'));
  await assert.rejects(() => command('b', 'stop'));
  const bpid = (await command('b', 'status')).appPid;
  process.kill(bpid, 'SIGKILL'); // Only the exact isolated fake-app PID, deliberate crash test.
  await wait(() => wb.exitCode !== null);
  assert.notEqual(wb.exitCode, 0);
  assert.ok(fs.existsSync(path.join(root, 'b.failed.json')));
  fs.unlinkSync(path.join(root, 'active.sock'));
  const candidate = await launch(b.file);
  await wait(async () => { try { return (await command('b', 'status')).mode === 'ready'; } catch { assert.equal(candidate.exitCode, null, candidate.log()); } });
  process.kill((await command('b', 'status')).appPid, 'SIGKILL');
  await wait(() => candidate.exitCode !== null);
  assert.notEqual(candidate.exitCode, 0, 'failed candidate must not silently stay ready');
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
