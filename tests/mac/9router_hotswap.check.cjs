// Run: node tests/mac/9router_hotswap.check.cjs (installed Caddy; no app dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { spawn, execFileSync } = require('node:child_process');
const { once } = require('node:events');
const { setTimeout: delay } = require('node:timers/promises');

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const LARGE_SIZE = 4 * 1024 * 1024;
const LARGE_CHUNK = Buffer.alloc(16384, 'stream-data:0123');

// Incremental RFC 6455 framing: TCP chunks are not WebSocket frames.
function decoder(masked) {
  let pending = Buffer.alloc(0);
  return (chunk) => {
    pending = Buffer.concat([pending, chunk]);
    const frames = [];
    while (pending.length >= 2) {
      const first = pending[0];
      assert.equal(first & 0x70, 0, 'unexpected reserved WebSocket bits');
      assert.equal(Boolean(pending[1] & 0x80), masked, 'incorrect masking');
      let size = pending[1] & 0x7f;
      let offset = 2;
      if (size === 126) {
        if (pending.length < 4) break;
        size = pending.readUInt16BE(2);
        assert.ok(size >= 126, 'noncanonical frame size');
        offset = 4;
      } else if (size === 127) {
        if (pending.length < 10) break;
        const length = pending.readBigUInt64BE(2);
        assert.ok(length >= 65536n && length <= 1048576n, 'invalid frame size');
        size = Number(length);
        offset = 10;
      }
      assert.ok(size <= 1048576, 'oversized test frame');
      const opcode = first & 15;
      if (opcode >= 8) assert.ok((first & 0x80) && size <= 125, 'invalid control frame');
      const maskOffset = offset;
      if (masked) offset += 4;
      if (pending.length < offset + size) break;
      const body = Buffer.from(pending.subarray(offset, offset + size));
      if (masked) {
        for (let i = 0; i < size; i++) body[i] ^= pending[maskOffset + (i % 4)];
      }
      frames.push({ fin: Boolean(first & 0x80), opcode, body });
      pending = pending.subarray(offset + size);
    }
    return frames;
  };
}

function frame(body, masked, opcode = 1, fin = true) {
  body = Buffer.from(body);
  const width = body.length < 126 ? 2 : body.length <= 65535 ? 4 : 10;
  const header = Buffer.alloc(width + (masked ? 4 : 0));
  header[0] = (fin ? 0x80 : 0) | opcode;
  header[1] = (masked ? 0x80 : 0) | (width === 2 ? body.length : width === 4 ? 126 : 127);
  if (width === 4) header.writeUInt16BE(body.length, 2);
  if (width === 10) header.writeBigUInt64BE(BigInt(body.length), 2);
  if (masked) {
    const mask = crypto.randomBytes(4);
    mask.copy(header, width);
    body = Buffer.from(body);
    for (let i = 0; i < body.length; i++) body[i] ^= mask[i % 4];
  }
  return Buffer.concat([header, body]);
}

async function main() {
  execFileSync(process.execPath, [path.join(__dirname, '9router_worker.check.cjs')], { stdio: 'inherit' });
  const root = fs.mkdtempSync(path.join(os.tmpdir(), '9r-swap-'));
  fs.chmodSync(root, 0o700);
  const servers = [];
  const sockets = new Set();
  const agent = new http.Agent({ keepAlive: true, maxSockets: 1 });
  const apps = {};
  let proxy;
  let proxyLog = '';
  let closing = false;
  let fail;
  const failure = new Promise((_, reject) => { fail = reject; });
  const deadline = setTimeout(() => fail(new Error('isolated routing check exceeded 30 seconds')), 30000);
  const interrupted = () => fail(new Error('isolated routing check interrupted'));
  process.on('SIGINT', interrupted);
  process.on('SIGTERM', interrupted);
  const track = (socket) => {
    if (closing) socket.destroy();
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
    socket.on('error', () => {});
    return socket;
  };
  const listen = async (server, address) => {
    assert.equal(closing, false, 'check ended before listening');
    if (typeof address === 'string') assert.ok(Buffer.byteLength(address) <= 103, 'Unix socket path too long');
    servers.push(server);
    server.on('connection', track);
    server.listen(address);
    await once(server, 'listening');
    return server.address();
  };
  const wait = async (predicate, label) => {
    const until = Date.now() + 5000;
    while (!predicate()) {
      assert.equal(closing, false, `check ended while waiting: ${label}`);
      assert.ok(Date.now() < until, `timeout: ${label}`);
      await delay(10);
    }
  };
  const request = (port, url = '/version', options = {}) => new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: url, ...options }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('error', reject);
      res.on('end', () => resolve({
        status: res.statusCode, headers: res.headers,
        body: Buffer.concat(chunks), socket: req.socket,
      }));
    });
    req.on('error', reject);
    req.setTimeout(5000, () => req.destroy(new Error('HTTP timeout')));
    req.end(options.body);
  });
  const openStream = async (port, url) => {
    let response;
    let body = '';
    const req = http.get({ host: '127.0.0.1', port, path: url, agent: false });
    const done = new Promise((resolve, reject) => {
      req.on('response', (res) => {
        response = res;
        res.on('data', (chunk) => { body += chunk; });
        res.on('end', resolve);
        res.on('error', reject);
      });
      req.on('error', reject);
    });
    done.catch(() => {});
    req.setTimeout(10000, () => req.destroy(new Error('stream timeout')));
    await wait(() => body.includes('-start\n\n'), `${url} initial event`);
    assert.equal(response.statusCode, 200);
    assert.equal(response.headers['content-type'], 'text/event-stream');
    return { req, done, body: () => body };
  };
  const openWebSocket = async (port) => {
    const socket = track(net.connect(port, '127.0.0.1'));
    await once(socket, 'connect');
    let headers = Buffer.alloc(0);
    let upgraded = false;
    const frames = [];
    const parse = decoder(false);
    const key = crypto.randomBytes(16).toString('base64');
    socket.on('data', (chunk) => {
      try {
        if (!upgraded) {
          headers = Buffer.concat([headers, chunk]);
          assert.ok(headers.length <= 16384, 'oversized upgrade response');
          const end = headers.indexOf('\r\n\r\n');
          if (end < 0) return;
          const text = headers.subarray(0, end).toString();
          assert.match(text, /^HTTP\/1\.1 101 Switching Protocols\r\n/);
          const expected = crypto.createHash('sha1').update(key + GUID).digest('base64');
          assert.match(text, /\r\nupgrade: websocket/i);
          assert.match(text, /\r\nconnection: upgrade/i);
          assert.ok(text.toLowerCase().includes(`sec-websocket-accept: ${expected}`.toLowerCase()));
          chunk = headers.subarray(end + 4);
          upgraded = true;
        }
        frames.push(...parse(chunk));
      } catch (error) { fail(error); }
    });
    socket.write('GET /ws HTTP/1.1\r\nHost: arbitrary.invalid\r\nUpgrade: websocket\r\n' +
      `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`);
    await wait(() => upgraded, 'WebSocket handshake');
    const receive = async (opcode, expected) => {
      await wait(() => frames.length > 0, 'WebSocket frame');
      const received = frames.shift();
      assert.equal(received.opcode, opcode);
      assert.equal(received.fin, true);
      assert.deepEqual(received.body, Buffer.from(expected));
    };
    return {
      async echo(slot, text) {
        const bytes = frame(text, true);
        // Deliberately split the header, mask and payload across writes.
        for (const part of [bytes.subarray(0, 1), bytes.subarray(1, 5), bytes.subarray(5)]) {
          socket.write(part);
          await delay(5);
        }
        await receive(1, `${slot}:${text}`);
      },
      async fragmented(slot) {
        socket.write(Buffer.concat([frame('frag', true, 1, false), frame('ping', true, 9), frame('ment', true, 0)]));
        await receive(10, 'ping');
        await receive(1, `${slot}:fragment`);
      },
      async close() {
        const code = Buffer.from([3, 232]); // Normal closure (1000).
        socket.write(frame(code, true, 8));
        await receive(8, code);
        await wait(() => socket.destroyed, 'WebSocket close handshake');
      },
    };
  };
  try {
    await Promise.race([failure, (async () => {
      for (const size of [1, 130, 66000]) {
        const parse = decoder(true);
        const encoded = frame(Buffer.alloc(size, 120), true);
        const decoded = [];
        for (let i = 0; i < encoded.length; i += 3) decoded.push(...parse(encoded.subarray(i, i + 3)));
        assert.equal(decoded.length, 1);
        assert.deepEqual(decoded[0].body, Buffer.alloc(size, 120));
      }
      console.log('PASS: incremental WebSocket parser handles split headers, masks and extended lengths');
      for (const name of ['a', 'b']) {
        const state = { streams: [], canceled: 0, posts: 0, drops: 0 };
        const app = http.createServer((req, res) => {
          res.setHeader('X-Test-Slot', name);
          if (req.url === '/stream' || req.url === '/cancel') {
            res.writeHead(200, { 'Content-Type': 'text/event-stream' });
            res.write(`data: ${name}-start\n\n`);
            if (req.url === '/stream') state.streams.push(res);
            else res.once('close', () => { state.canceled++; });
            return;
          }
          if (req.url === '/large') {
            res.writeHead(206, { 'Content-Type': 'application/octet-stream', 'X-Test-Header': 'unchanged' });
            let sent = 0;
            const pump = () => {
              if (res.destroyed) return;
              if (sent === LARGE_SIZE) return res.end();
              sent += LARGE_CHUNK.length;
              if (res.write(LARGE_CHUNK)) setImmediate(pump);
              else res.once('drain', pump);
            };
            pump();
            return;
          }
          const chunks = [];
          req.on('data', (chunk) => chunks.push(chunk));
          req.on('error', () => {});
          req.on('end', () => {
            if (req.method === 'POST') state.posts++;
            if (req.url === '/drop') { state.drops++; res.destroy(); return; }
            if (req.url === '/version') return res.end(name);
            res.writeHead(418, { 'Content-Type': 'application/json', 'X-Test-Header': 'unchanged' });
            res.end(JSON.stringify({ slot: name, method: req.method, url: req.url,
              body: Buffer.concat(chunks).toString('base64'), headers: req.headers }));
          });
        });
        app.on('upgrade', (req, socket, head) => {
          try {
            assert.equal(req.url, '/ws');
            assert.equal(req.headers['sec-websocket-version'], '13');
            assert.equal(Buffer.from(req.headers['sec-websocket-key'], 'base64').length, 16);
            const accept = crypto.createHash('sha1').update(req.headers['sec-websocket-key'] + GUID).digest('base64');
            socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n' +
              `Connection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
            const parse = decoder(true);
            let fragments = null;
            const consume = (chunk) => {
              try {
                for (const incoming of parse(chunk)) {
                  if (incoming.opcode === 8) { socket.end(frame(incoming.body, false, 8)); continue; }
                  if (incoming.opcode === 9) { socket.write(frame(incoming.body, false, 10)); continue; }
                  assert.ok(incoming.opcode === 1 || incoming.opcode === 0, 'unsupported echo opcode');
                  if (incoming.opcode === 1) {
                    assert.equal(fragments, null, 'overlapping fragmented message');
                    fragments = [];
                  } else assert.notEqual(fragments, null, 'unexpected continuation');
                  fragments.push(incoming.body);
                  if (incoming.fin) {
                    socket.write(frame(`${name}:${Buffer.concat(fragments).toString()}`, false));
                    fragments = null;
                  }
                }
              } catch (error) { socket.destroy(); fail(error); }
            };
            socket.on('data', consume);
            consume(head);
          } catch (error) { socket.destroy(); fail(error); }
        });
        const address = await listen(app, { host: '127.0.0.1', port: 0 });
        assert.notEqual(address.port, 20128, 'fake app must not use production port');
        const bridge = net.createServer((incoming) => {
          const outgoing = track(net.connect(address.port, '127.0.0.1'));
          incoming.on('error', () => outgoing.destroy());
          outgoing.on('error', () => incoming.destroy());
          incoming.once('close', () => outgoing.destroy());
          // Let pipe flush queued response bytes before ending the client socket.
          incoming.pipe(outgoing);
          outgoing.pipe(incoming);
        });
        await listen(bridge, path.join(root, `${name}.sock`));
        apps[name] = { state, port: address.port };
      }
      const reservation = net.createServer();
      const { port } = await listen(reservation, { host: '127.0.0.1', port: 0 });
      assert.notEqual(port, 20128, 'test must not use production port');
      await new Promise((resolve, reject) => reservation.close((error) => error ? reject(error) : resolve()));
      const active = path.join(root, 'active.sock');
      fs.symlinkSync(path.join(root, 'a.sock'), active);
      const select = (slot) => {
        assert.ok(['a', 'b'].includes(slot));
        const target = path.join(root, `${slot}.sock`);
        assert.ok(fs.lstatSync(target).isSocket());
        const temporary = path.join(root, 'next.sock');
        fs.symlinkSync(target, temporary);
        try { fs.renameSync(temporary, active); }
        finally { fs.rmSync(temporary, { force: true }); }
      };
      const config = path.join(root, 'Caddyfile');
      const template = fs.readFileSync(path.join(__dirname, '../../scripts/mac/templates/9router.Caddyfile'), 'utf8');
      assert.equal(template.split('http://:20128').length, 2, 'expected one production listener placeholder');
      fs.mkdirSync(path.join(root, 'assets'));
      fs.writeFileSync(path.join(root, 'assets', 'old-build.js'), 'immutable old chunk');
      fs.writeFileSync(config, template.replace('http://:20128', `http://:${port}`)
        .replaceAll('__RUNTIME__', root).replaceAll('__STATE__', root), { mode: 0o600 });
      const caddy = process.env.CADDY_BIN || (fs.existsSync('/opt/homebrew/bin/caddy') ? '/opt/homebrew/bin/caddy' : 'caddy');
      const isolatedEnv = { ...process.env, HOME: root, XDG_DATA_HOME: root, XDG_CONFIG_HOME: root };
      execFileSync(caddy, ['validate', '--config', config, '--adapter', 'caddyfile'], {
        timeout: 5000, stdio: 'pipe', env: isolatedEnv,
      });
      proxy = spawn(caddy, ['run', '--config', config, '--adapter', 'caddyfile'], {
        env: isolatedEnv,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      proxy.on('error', fail);
      for (const output of [proxy.stdout, proxy.stderr]) output.on('data', (chunk) => { proxyLog = (proxyLog + chunk).slice(-16000); });
      proxy.once('exit', (code, signal) => {
        if (!closing) fail(new Error(`isolated Caddy exited (${code}, ${signal}): ${proxyLog}`));
      });
      const originalPid = proxy.pid;
      const get = () => request(port, '/version', { agent, headers: { Host: 'arbitrary.invalid' } });
      const readyUntil = Date.now() + 5000;
      let first;
      while (!first) {
        try { first = await get(); }
        catch (error) { if (Date.now() >= readyUntil) throw error; await delay(20); }
      }
      assert.equal(first.body.toString(), 'a');
      const clientSocket = first.socket;
      const assetBefore = await request(port, '/_next/static/old-build.js');
      assert.equal(assetBefore.status, 200);
      assert.equal(assetBefore.headers['cache-control'], 'public, max-age=31536000, immutable');
      assert.equal(assetBefore.body.toString(), 'immutable old chunk');
      // Only the protected HTTP-ingress Unix listener may trust stamped XFF.
      const stamped = '100.64.0.7';
      const forwarded = await request(port, '/echo', { socketPath: path.join(root, 'http-ingress.sock'),
        headers: { 'X-Forwarded-For': stamped, 'X-Real-IP': '203.0.113.66', 'X-9r-Real-Ip': '203.0.113.66', 'X-9r-Peer-Token': 'forged' } });
      assert.equal(JSON.parse(forwarded.body).headers['x-9r-real-ip'], stamped,
        'protected HTTP ingress must retain authenticated upstream client identity');
      const forged = await request(port, '/echo', { headers: { 'X-Forwarded-For': stamped, 'X-Real-IP': stamped } });
      assert.notEqual(JSON.parse(forged.body).headers['x-9r-real-ip'], stamped,
        'raw TCP ingress must never trust client-supplied XFF');
      const streamA = await openStream(port, '/stream');
      const wsA = await openWebSocket(port);
      await wsA.echo('a', 'before-switch');
      select('b');
      for (let n = 0; n < 100; n++) {
        const result = await get();
        assert.equal(result.body.toString(), 'b', 'new HTTP requests must route to B after switching');
        assert.equal(result.socket, clientSocket, 'client keepalive connection changed');
      }
      const assetAfter = await request(port, '/_next/static/old-build.js');
      assert.equal(assetAfter.status, assetBefore.status);
      assert.deepEqual(assetAfter.body, assetBefore.body);
      console.log('PASS: 100 requests route to B over the original client keepalive socket; arbitrary Host and retained static asset accepted');
      await wsA.echo('a', 'after-switch-' + 'x'.repeat(130));
      await wsA.fragmented('a');
      const streamB = await openStream(port, '/stream');
      const wsB = await openWebSocket(port);
      await wsB.echo('b', 'before-rollback');

      const body = Buffer.from([0, 255, 1, 13, 10, ...Buffer.from('exact POST body')]);
      const url = '/echo?encoded=a%2Fb&repeat=1&repeat=2';
      const headers = { Host: 'unrelated.invalid', 'Content-Type': 'application/octet-stream',
        'Content-Length': String(body.length), 'X-Test-Request': 'preserved' };
      const beforePosts = apps.b.state.posts;
      const direct = await request(apps.b.port, url, { method: 'POST', headers, body });
      const proxied = await request(port, url, { method: 'POST', headers, body, agent });
      assert.equal(apps.b.state.posts - beforePosts, 2, 'one direct and one proxied POST, no replay');
      assert.equal(proxied.status, direct.status);
      for (const key of ['content-type', 'x-test-slot', 'x-test-header']) assert.equal(proxied.headers[key], direct.headers[key]);
      const directData = JSON.parse(direct.body);
      const proxyData = JSON.parse(proxied.body);
      for (const key of ['slot', 'method', 'url', 'body']) assert.deepEqual(proxyData[key], directData[key]);
      for (const key of Object.keys(headers).map((key) => key.toLowerCase())) assert.equal(proxyData.headers[key], directData.headers[key]);
      assert.equal(proxyData.body, body.toString('base64'));
      const forged = await request(port, '/headers', { headers: {
        Host: 'forged.invalid', 'X-Forwarded-For': '203.0.113.99', 'X-Forwarded-Host': 'evil.invalid',
        'X-Forwarded-Proto': 'https', 'X-Real-IP': '203.0.113.99', 'X-9r-Real-Ip': '203.0.113.99',
        'X-9r-Peer-Token': 'fake-test-value', 'X-9r-Via-Proxy': '1',
      } });
      const observed = JSON.parse(forged.body).headers;
      assert.equal(observed['x-forwarded-for'], '127.0.0.1');
      assert.equal(observed['x-forwarded-host'], 'forged.invalid');
      assert.equal(observed['x-forwarded-proto'], 'http');
      for (const key of ['x-real-ip', 'x-9r-real-ip', 'x-9r-peer-token', 'x-9r-via-proxy']) assert.equal(observed[key], undefined);
      const beforeDrops = apps.b.state.drops;
      const dropped = await request(port, '/drop', { method: 'POST', headers, body });
      assert.equal(dropped.status, 502);
      assert.equal(apps.b.state.drops - beforeDrops, 1, 'failed upstream POST must never replay');
      console.log('PASS: exact POST body/query/Host/request headers and non-200 response; forged IP headers sanitized; no POST replay');

      const largeDirect = await request(apps.b.port, '/large');
      const largeProxy = await request(port, '/large');
      assert.equal(largeProxy.status, largeDirect.status);
      for (const key of ['content-type', 'x-test-slot', 'x-test-header']) assert.equal(largeProxy.headers[key], largeDirect.headers[key]);
      assert.equal(largeProxy.body.length, LARGE_SIZE);
      assert.deepEqual(largeProxy.body, largeDirect.body);
      const hash = crypto.createHash('sha256').update(largeProxy.body).digest('hex');
      console.log(`PASS: 4 MiB streamed response matches direct app bytes/status/headers; sha256=${hash}`);
      for (const target of [apps.b.port, port]) {
        const before = apps.b.state.canceled;
        const stream = await openStream(target, '/cancel');
        stream.req.destroy();
        await assert.rejects(stream.done);
        await wait(() => apps.b.state.canceled === before + 1, 'upstream observes client cancellation');
      }
      console.log('PASS: direct and proxied client cancellation close the upstream response');

      select('a');
      for (let n = 0; n < 10; n++) {
        const result = await get();
        assert.equal(result.body.toString(), 'a');
        assert.equal(result.socket, clientSocket);
      }
      await wsA.echo('a', 'after-rollback');
      await wsB.echo('b', 'after-rollback');
      for (const name of ['a', 'b']) for (const res of apps[name].state.streams) res.end(`data: ${name}-done\n\n`);
      await Promise.all([streamA.done, streamB.done]);
      assert.equal(streamA.body(), 'data: a-start\n\ndata: a-done\n\n');
      assert.equal(streamB.body(), 'data: b-start\n\ndata: b-done\n\n');
      await Promise.all([wsA.close(), wsB.close()]);
      assert.equal(proxy.pid, originalPid);
      assert.equal(proxy.exitCode, null);
      assert.equal(proxy.signalCode, null);
      process.kill(originalPid, 0);
      console.log('PASS: switch back to A on same client socket and live Caddy PID; A/B SSE and WebSockets preserve their original release');
    })()]);
  } finally {
    closing = true;
    clearTimeout(deadline);
    agent.destroy();
    for (const socket of sockets) socket.destroy();
    const cleanup = servers.map((server) => new Promise((resolve) => server.close(resolve)));
    if (proxy && proxy.exitCode === null && proxy.signalCode === null) {
      const exited = once(proxy, 'exit');
      proxy.kill('SIGTERM');
      const killTimer = setTimeout(() => proxy.kill('SIGKILL'), 2000);
      cleanup.push(exited.finally(() => clearTimeout(killTimer)));
    }
    let cleanupTimer;
    try {
      await Promise.race([
        Promise.all(cleanup),
        new Promise((_, reject) => {
          cleanupTimer = setTimeout(() => reject(new Error(`cleanup timed out; isolated evidence retained at ${root}`)), 5000);
        }),
      ]);
      fs.rmSync(root, { recursive: true, force: true });
    } finally {
      clearTimeout(cleanupTimer);
      process.removeListener('SIGINT', interrupted);
      process.removeListener('SIGTERM', interrupted);
    }
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
