const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

test('managed h2c drain IPC retains responses and deferred persistence', { timeout: 10_000 }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), '9r-h2c-'));
  const source = path.resolve(__dirname, '../..');
  let child;
  let socket;
  try {
    for (const file of ['custom-server.js', 'src/lib/db/managed.cjs']) {
      fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
      fs.copyFileSync(path.join(source, file), path.join(root, file));
    }
    // Fake attachment isolates HTTP/IPC accounting, not packaged WebSocket behavior.
    const ws = path.join(root, 'open-sse/handlers/responsesWs');
    fs.mkdirSync(ws, { recursive: true });
    fs.writeFileSync(path.join(ws, 'index.js'), 'exports.attachResponsesWebSocket = async () => {};\n');
    const program = `
      const http = require('node:http');
      const managed = require('./src/lib/db/managed.cjs');
      require('./custom-server.js');
      let endResponse, endPersistence;
      const server = http.createServer(async (req, res) => {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const done = managed.beginWork('persistence');
        const response = new Promise(resolve => { endResponse = resolve; });
        const persistence = new Promise(resolve => { endPersistence = resolve; });
        res.setHeader('Content-Type', 'text/event-stream');
        res.write('data: start\\n\\n');
        process.send({ fixture: 'request', body: Buffer.concat(chunks).toString() });
        await response;
        res.end('data: [DONE]\\n\\n');
        await persistence;
        done();
        process.send({ fixture: 'persistence-done' });
      });
      process.on('message', message => {
        if (message.fixture === 'end-response') endResponse();
        if (message.fixture === 'end-persistence') endPersistence();
      });
      server.listen(0, '127.0.0.1', async () => {
        await managed.awaitResponsesWsReady();
        managed.workState().initialized = true;
        process.send({ fixture: 'ready', port: server.address().port });
      });
    `;
    child = spawn(process.execPath, ['-e', program], {
      cwd: root,
      env: { ...process.env, NINEROUTER_MANAGED_WORKER: '1' },
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
    });
    let output = '';
    child.stdout.on('data', chunk => { output += chunk; });
    child.stderr.on('data', chunk => { output += chunk; });
    const message = predicate => new Promise((resolve, reject) => {
      const cleanup = () => { child.off('message', receive); child.off('exit', exited); clearTimeout(timer); };
      const receive = value => { if (predicate(value)) { cleanup(); resolve(value); } };
      const exited = () => { cleanup(); reject(new Error(`Fixture exited: ${output}`)); };
      const timer = setTimeout(() => { cleanup(); reject(new Error(`Fixture IPC timed out: ${output}`)); }, 2_000);
      child.on('message', receive);
      child.once('exit', exited);
    });
    let id = 0;
    const control = async op => {
      const current = ++id;
      const pending = message(value => value.type === '9router-managed' && value.id === current);
      child.send({ type: '9router-managed', id: current, op });
      return (await pending).work;
    };
    const ready = await message(value => value.fixture === 'ready');
    const request = message(value => value.fixture === 'request');
    socket = net.createConnection({ host: '127.0.0.1', port: ready.port });
    socket.setTimeout(5_000, () => socket.destroy(new Error('Fixture HTTP timed out')));
    let response = '';
    socket.on('data', chunk => { response += chunk; });
    const ended = once(socket, 'end');
    ended.catch(() => {});
    await once(socket, 'connect');
    const body = '{"stream":true}';
    socket.write(`POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade, HTTP2-Settings\r\nUpgrade: h2c\r\nHTTP2-Settings: AAEAAEAAAAIAAAAA\r\nContent-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`);
    assert.equal((await request).body, body);
    const draining = await control('drain');
    assert.equal(draining.initialized, true);
    assert.equal(draining.unknown, false);
    assert.equal(draining.draining, true);
    assert.equal(draining.responses, 1);
    assert.equal(draining.handlers, 1);
    assert.equal(draining.persistence, 1);
    assert.equal(draining.upgrades, 0);
    child.send({ fixture: 'end-response' });
    await ended;
    assert.match(response, /^HTTP\/1\.1 200 OK\r\n/);
    assert.match(response, /data: start\n\n/);
    assert.match(response, /data: \[DONE\]\n\n/);
    const pending = await control('status');
    assert.equal(pending.responses, 0);
    assert.equal(pending.handlers, 1);
    assert.equal(pending.persistence, 1);
    const completed = message(value => value.fixture === 'persistence-done');
    child.send({ fixture: 'end-persistence' });
    await completed;
    const idle = await control('status');
    for (const kind of ['responses', 'handlers', 'persistence', 'upgrades']) assert.equal(idle[kind], 0);
    assert.equal(idle.draining, true);
    assert.equal(idle.unknown, false);
  } finally {
    socket?.destroy();
    if (child && child.exitCode === null && child.signalCode === null) {
      const exited = once(child, 'exit');
      child.kill('SIGKILL');
      await exited;
    }
    fs.rmSync(root, { recursive: true, force: true });
  }
});
