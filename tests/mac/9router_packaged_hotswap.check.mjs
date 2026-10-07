// Guest-only source check. The parent owns Namespace activation, evidence export, and devbox shutdown.
// CLI: node <this-file> <source> <package.tgz> <sha256> <NEW-result.json> <guest-HOME> <scope.json>
// Scope contract: packages[sha256] binds tarball, package_files, manifest_sha256, persistenceFingerprint,
// and installations = { a: <releases/version/a/lib/node_modules/9router>, b: <releases/version/b/lib/node_modules/9router> }.
// Both paths contain the same exact package/version. The controller owns every route change.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import http from 'node:http';
import { createHash, createHmac, randomBytes } from 'node:crypto';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { StringDecoder } from 'node:string_decoder';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';

const [sourceInput, packageInput, expectedDigest, resultInput, homeInput, scopeInput] = process.argv.slice(2);
assert.equal(process.argv.length, 8, 'Supply source, package, digest, new result, guest HOME, and qualification scope');
assert.equal(process.platform, 'darwin', 'macOS guest required');
assert.equal(process.arch, 'arm64', 'ARM64 guest required');
assert.match(expectedDigest, /^[a-f0-9]{64}$/, 'Exact package digest required');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const regular = file => {
  const info = fs.lstatSync(file);
  assert(info.isFile() && !info.isSymbolicLink() && info.nlink === 1, 'Regular unlinked input required');
  return fs.readFileSync(file);
};
const exists = file => { try { fs.lstatSync(file); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } };
const concrete = input => { assert(path.isAbsolute(input), 'Absolute input required'); assert.equal(fs.realpathSync(input), input, 'Concrete input required'); return input; };
const source = concrete(sourceInput), tarball = concrete(packageInput), home = concrete(homeInput), scopeFile = concrete(scopeInput);
const resultFile = path.resolve(resultInput);
assert.equal(resultFile, resultInput, 'Absolute new report path required');
assert(!exists(resultFile), 'Refuse stale fixture result');
assert.equal(sha(regular(tarball)), expectedDigest, 'Exact package digest mismatch');
const safeAncestors = file => {
  let directory = path.dirname(file);
  while (directory !== '/') {
    const info = fs.lstatSync(directory);
    assert(info.isDirectory() && !info.isSymbolicLink() && [0, process.getuid()].includes(info.uid)
      && (!(info.mode & 0o022) || (info.uid === 0 && Boolean(info.mode & 0o1000))), 'Unsafe input ancestor');
    directory = path.dirname(directory);
  }
};
for (const input of [source, tarball, home, scopeFile, resultFile]) safeAncestors(input);
const privateDir = directory => {
  const info = fs.lstatSync(directory);
  assert(info.isDirectory() && !info.isSymbolicLink() && info.uid === process.getuid() && (info.mode & 0o777) === 0o700, 'Private owned directory required');
  assert.equal(fs.realpathSync(directory), directory, 'Concrete private directory required');
};
privateDir(home);
const scopeInfo = fs.lstatSync(scopeFile);
assert(scopeInfo.uid === process.getuid() && (scopeInfo.mode & 0o777) === 0o600, 'Private qualification scope required');
const scopeBytes = regular(scopeFile), scope = JSON.parse(scopeBytes);
assert.equal(scope.protocol, 1); assert.equal(scope.phase, 'qualification');
assert(!Object.hasOwn(scope, 'result'), 'A qualification scope is not a passing receipt');
assert.match(scope.run_id, /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/);
assert.equal(home, `/Volumes/devbox/9router/qualification/${scope.run_id}/home`, 'Isolated guest HOME required');
assert.equal(process.env.HOME, home, 'Environment HOME differs from isolated input');
assert.equal(scope.home, home); assert.equal(scopeFile, path.join(home, 'scope.json'));
assert.equal(scope.source_root, source); assert(source.startsWith('/Volumes/devbox/'), 'Guest source checkout required');
assert(tarball.startsWith(path.dirname(home) + path.sep), 'Qualification-run package required');
assert.match(scope.source_commit, /^[a-f0-9]{40}$/); assert.match(scope.source_patch_sha256, /^[a-f0-9]{64}$/);
for (const key of ['devbox_id', 'instance_id']) assert(typeof scope.namespace?.[key] === 'string' && scope.namespace[key], 'Namespace execution identity required');
const runRoot = path.dirname(home), evidence = path.dirname(resultFile);
assert.equal(evidence, path.join(runRoot, 'evidence'), 'Isolated evidence directory required'); privateDir(evidence);
const runtime = `/tmp/9rq-${sha(Buffer.from(scope.run_id)).slice(0, 12)}`;
assert.equal(scope.runtime, runtime, 'Short bound runtime required');
assert(!exists(runtime), 'Refuse occupied fixture runtime');
const data = path.join(home, '.9router'), stateDir = path.join(data, 'hotswap');
assert(!exists(data), 'Refuse existing fixture database or enrollment');
const packageBinding = scope.packages?.[expectedDigest];
assert(packageBinding && typeof packageBinding === 'object', 'Exact package absent from qualification scope');
assert.equal(packageBinding.tarball, tarball, 'Scope tarball input differs');
assert.equal(fs.lstatSync(tarball).mode & 0o777, 0o600, 'Private qualification tarball required');
const installations = packageBinding.installations;
assert.deepEqual(Object.keys(installations || {}).sort(), ['a', 'b'], 'Controller must bind two same-package installations');
assert.notEqual(installations.a, installations.b, 'Distinct slot installations required');
for (const release of Object.values(installations)) {
  assert(path.isAbsolute(release) && path.normalize(release) === release && release.startsWith(path.join(data, 'releases') + path.sep), 'Confined installation required');
  assert(!exists(release), 'Refuse existing immutable installation');
}
const abort = new AbortController(), secrets = [], failures = [], children = new Set(), requests = new Set(), sockets = new Set(), sessions = [];
const transitions = [], endpointChecks = [], jobs = new Map(), pidSlots = new Map(), providerRequests = new Map(), controllerGroups = new Set(), ownedPids = new Set();
let provider, providerPort, qualified = false, measuredErrors = 0, measuredTruncations = 0, version, managed, installedHashes, publicClientSocket;
const sanitize = text => secrets.reduce((out, secret) => out.replaceAll(secret, '[REDACTED]'), String(text))
  .replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '[REDACTED]');
const report = { schema: 1, scope: 'deterministic packaged continuity and separate intentional crash recovery; not live-provider qualification',
  provider: 'deterministic-guest-fixture', success: false, tarballSha256: expectedDigest,
  fixtureSha256: sha(regular(new URL(import.meta.url))), qualificationScopeSha256: sha(scopeBytes),
  source_commit: scope.source_commit, source_patch_sha256: scope.source_patch_sha256, namespace: scope.namespace,
  runtime_sha256: scope.runtime_sha256, run_id: scope.run_id, transitions, endpointChecks, failures,
  productionCredentials: false, nativeAdapter: 'node:sqlite', liveProviderQualification: false, releaseTreesRetained: true };
const signalHandlers = Object.fromEntries(['SIGINT', 'SIGTERM', 'SIGHUP'].map(signal => [signal, () => abort.abort(new Error('Fixture interrupted'))]));
for (const [signal, handler] of Object.entries(signalHandlers)) process.on(signal, handler);
const batchTimer = setTimeout(() => abort.abort(new Error('Fixture batch deadline')), 900000);
let cleaning = false;
const sleep = ms => delay(ms, undefined, { signal: abort.signal });
const fault = (error, truncation = false) => { report.firstSessionFault ??= { reason: sanitize(error.message), truncation }; measuredErrors++; if (truncation) measuredTruncations++; return error; };
const sync = (program, args) => {
  const result = spawnSync(program, args, { cwd: source, env: { PATH: process.env.PATH, HOME: home }, encoding: 'utf8', timeout: 30000, maxBuffer: 33554432 });
  assert(!result.error && result.status === 0, 'Required guest observation or preparation did not run');
  return result.stdout.trim();
};
const wait = async (predicate, timeout = 120000) => {
  const until = performance.now() + timeout;
  while (performance.now() < until) {
    if (!cleaning) abort.signal.throwIfAborted();
    const value = await predicate(); if (value) return value;
    if (cleaning) await delay(100); else await sleep(100);
  }
  throw new Error('Fixture positive barrier deadline');
};
const bounded = (promise, timeout = 120000) => new Promise((resolve, reject) => {
  const finish = (fn, value) => { clearTimeout(timer); abort.signal.removeEventListener('abort', interrupted); fn(value); };
  const interrupted = () => finish(reject, abort.signal.reason);
  const timer = setTimeout(() => finish(reject, new Error('Fixture session deadline')), timeout);
  if (!cleaning) {
    abort.signal.addEventListener('abort', interrupted, { once: true });
    if (abort.signal.aborted) interrupted();
  }
  promise.then(value => finish(resolve, value), error => finish(reject, error));
});
const controllerFile = path.join(source, 'scripts/mac/9router_hotswap.py');
const controller = args => new Promise((resolve, reject) => {
  abort.signal.throwIfAborted();
  const child = spawn('python3', [controllerFile, '--qualification-scope', scopeFile, ...args],
    { cwd: source, detached: true, env: { PATH: process.env.PATH, HOME: home, TMPDIR: path.join(runRoot, 'tmp'), NINEROUTER_PROBE_MODEL: 'fixture/continuity-model' }, stdio: ['ignore', 'pipe', 'pipe'] });
  children.add(child);
  child.once('spawn', () => controllerGroups.add(child.pid));
  let output = '', overflow = false, expired = false;
  const collect = bytes => { output += bytes; if (Buffer.byteLength(output) > 1048576) { overflow = true; output = output.slice(-1048576); child.kill('SIGTERM'); } };
  child.stdout.on('data', collect); child.stderr.on('data', collect);
  const signalGroup = signal => {
    if (!child.pid) return;
    try { process.kill(-child.pid, signal); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  };
  const stop = () => signalGroup('SIGTERM');
  abort.signal.addEventListener('abort', stop, { once: true });
  const timer = setTimeout(() => { expired = true; stop(); }, 240000);
  const killTimer = setTimeout(() => { if (children.has(child)) signalGroup('SIGKILL'); }, 250000);
  child.on('error', reject);
  child.once('close', (code, signal) => {
    clearTimeout(timer); clearTimeout(killTimer); abort.signal.removeEventListener('abort', stop); children.delete(child);
    report.controllerCommands ??= [];
    report.controllerCommands.push({ command: args[0], exit_code: code, signal, diagnostic: sanitize(output).slice(-4096) });
    if (code !== 0 || signal || overflow || expired || abort.signal.aborted) reject(new Error('Bound controller command refused or incomplete'));
    else resolve(output);
  });
  if (abort.signal.aborted) stop();
});
const control = slot => new Promise((resolve, reject) => {
  privateDir(fs.realpathSync(runtime));
  const controlFile = path.join(runtime, `${slot}.ctl`), info = fs.lstatSync(controlFile);
  assert(info.isSocket() && info.uid === process.getuid() && !(info.mode & 0o077), 'Private control identity unavailable');
  const socket = net.connect({ path: controlFile, signal: abort.signal }); sockets.add(socket);
  let bytes = '';
  socket.setTimeout(5000, () => socket.destroy(new Error('Private worker status deadline')));
  socket.on('error', reject); socket.once('close', () => sockets.delete(socket));
  socket.on('connect', () => socket.end('{"op":"status"}\n'));
  socket.on('data', chunk => { bytes += chunk; if (Buffer.byteLength(bytes) > 65536) socket.destroy(new Error('Private status oversized')); });
  socket.on('end', () => { try {
    assert(bytes.endsWith('\n') && bytes.split('\n').length === 2, 'Private control response incomplete');
    resolve(JSON.parse(bytes));
  } catch (error) { reject(error); } });
});
const workKeys = ['responses', 'handlers', 'upgrades', 'cleanup', 'persistence', 'refresh', 'background', 'quota', 'websocket'];
const status = async (slot, expectedMode) => {
  const value = await control(slot);
  assert.equal(value.slot, slot); assert.equal(value.version, version);
  if (expectedMode) assert.equal(value.mode, expectedMode, 'Private slot mode differs');
  assert(Number.isSafeInteger(value.appPid) && value.appPid > 0, 'Private app PID unavailable');
  assert(Number.isSafeInteger(value.connections) && value.connections >= 0);
  assert.equal(value.appWork?.initialized, true); assert.equal(value.appWork?.unknown, false);
  assert.equal(value.appWork?.responsesWsAttached, true);
  for (const key of workKeys) assert(Number.isSafeInteger(value.appWork[key]) && value.appWork[key] >= 0, 'Private work count unavailable');
  process.kill(value.appPid, 0);
  const config = JSON.parse(regular(path.join(stateDir, `${slot}.json`)));
  assert.equal(config.slot, slot); assert.equal(config.release, installations[slot]);
  assert.equal(config.runtime, runtime); assert.equal(config.dataDir, data); assert.equal(config.version, version);
  const worker = captureJob(slot);
  assert.equal(Number(sync('/bin/ps', ['-p', String(value.appPid), '-o', 'ppid='])), worker.pid, 'Private app is not the bound job child');
  for (const [pid, previousSlot] of pidSlots) if (previousSlot === slot) pidSlots.delete(pid);
  pidSlots.set(value.appPid, slot); ownedPids.add(value.appPid);
  return value;
};
const launchJob = name => {
  const target = `gui/${process.getuid()}/${name}`;
  const observation = spawnSync('launchctl', ['print', target], { encoding: 'utf8', timeout: 10000, maxBuffer: 1048576 });
  assert(!observation.error, 'Launchd identity observation did not run');
  if (observation.status !== 0) {
    assert.equal(observation.signal, null, 'Launchd identity observation interrupted');
    assert(/Could not find service/.test(observation.stderr) && observation.stderr.includes(name), 'Launchd absence is unknown');
    return null;
  }
  const paths = [...observation.stdout.matchAll(/^\s*path = (.+)$/gm)].map(match => match[1]);
  const pids = [...observation.stdout.matchAll(/^\s*pid = (\d+)$/gm)].map(match => Number(match[1]));
  assert.equal(paths.length, 1, 'Loaded job path unavailable');
  return { target, path: paths[0], pid: pids.length === 1 ? pids[0] : null };
};
const jobPrefix = `com.lfenergy.9router-qualify-${runtime.slice('/tmp/9rq-'.length)}`;
const jobNames = { a: `${jobPrefix}-worker-a`, b: `${jobPrefix}-worker-b`, proxy: `${jobPrefix}-proxy` };
const captureJob = key => {
  const value = launchJob(jobNames[key]);
  assert(value && value.path === path.join(stateDir, `${key}.plist`) && Number.isSafeInteger(value.pid) && value.pid > 0, 'Exact managed job identity unavailable');
  jobs.set(key, value); ownedPids.add(value.pid); return value;
};
const waitJobGone = (name, ownedPath) => wait(() => {
  const remaining = launchJob(name);
  if (!remaining) return true;
  assert.equal(remaining.path, ownedPath, 'Owned launchd job identity changed');
  return false;
}, 15000);
const proxyPid = () => captureJob('proxy').pid;
const request = (endpoint, body, authorization, cookie, agent = false, port = 20128) => new Promise((resolve, reject) => {
  const bytes = body === undefined ? null : Buffer.from(JSON.stringify(body));
  const req = http.request({ host: '127.0.0.1', port, path: endpoint, method: bytes ? 'POST' : 'GET', agent, signal: abort.signal,
    headers: { ...(bytes ? { 'Content-Type': 'application/json', 'Content-Length': bytes.length } : {}),
      ...(authorization ? { Authorization: authorization } : {}), ...(cookie ? { Cookie: cookie } : {}) } }, res => {
    let size = 0; const chunks = [], clientSocket = res.socket;
    res.on('data', chunk => { size += chunk.length; if (size > 1048576) req.destroy(new Error('Fixture HTTP response oversized')); else chunks.push(chunk); });
    res.on('error', reject); res.on('aborted', () => reject(new Error('Fixture HTTP response truncated')));
    res.on('end', () => { if (!res.complete) reject(new Error('Fixture HTTP response incomplete')); else resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString(), socket: clientSocket }); });
  });
  requests.add(req); req.once('close', () => requests.delete(req));
  req.setTimeout(30000, () => req.destroy(new Error('Fixture HTTP deadline'))); req.on('error', reject); req.end(bytes);
});
const createProbe = (slot, hold = false) => {
  const id = `probe-${randomBytes(12).toString('hex')}`;
  const value = { id, expectedSlot: slot, hold, released: !hold, started: false, finished: false, slot: null, appPid: null, marker: null, response: null };
  providerRequests.set(id, value); return value;
};
const assertMarker = (probe, text) => {
  assert(probe.started && probe.finished, 'Provider completion unproven'); assert.equal(probe.slot, probe.expectedSlot, 'Provider reached wrong slot');
  assert.equal(text, `${probe.marker}:start:terminal`, 'Provider marker missing or truncated');
};
const streamState = probe => {
  const events = []; let buffer = '', text = '', created = 0, terminal = 0, done = 0;
  return {
    events, get opened() { return created === 1 && text === `${probe.marker}:start`; }, get terminal() { return terminal; },
    event(event) {
      assert(event && typeof event === 'object', 'Invalid Responses event');
      assert(!event.error && !['error', 'response.failed', 'response.incomplete'].includes(event.type), 'Responses stream error');
      events.push(event.type); assert(events.length <= 100, 'Unexpected Responses event population');
      if (event.type === 'response.created') { created++; assert.equal(event.response?.status, 'in_progress'); }
      if (event.type === 'response.output_text.delta') text += event.delta;
      if (event.type === 'response.completed') {
        terminal++; assert.equal(event.response?.status, 'completed');
        assertMarker(probe, event.response?.output?.[0]?.content?.[0]?.text);
      }
    },
    push(chunk) {
      buffer += chunk; assert(Buffer.byteLength(buffer) < 1048576, 'SSE buffer oversized');
      buffer = buffer.replaceAll('\r\n', '\n');
      let separator;
      while ((separator = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, separator); buffer = buffer.slice(separator + 2);
        const dataLines = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart());
        if (!dataLines.length) { assert(block.split('\n').every(line => !line || line.startsWith(':') || line.startsWith('event:')), 'Malformed SSE field'); continue; }
        const bytes = dataLines.join('\n'); if (bytes === '[DONE]') done++; else this.event(JSON.parse(bytes));
      }
    },
    complete(sse = false) {
      assert.equal(created, 1); assert.equal(terminal, 1, 'Responses terminal missing');
      assert.equal(text, `${probe.marker}:start:terminal`, 'Responses deltas truncated');
      if (sse) { assert.equal(done, 1, 'SSE DONE marker missing'); assert.equal(buffer.trim(), '', 'SSE final frame truncated'); }
    },
  };
};
let apiAuthorization, cookie, wsHelpers;
const openSse = probe => {
  const state = streamState(probe); let ended = false;
  const body = Buffer.from(JSON.stringify({ model: 'fixture/continuity-model', input: probe.id, stream: true }));
  let req;
  const completion = new Promise((resolve, reject) => {
    req = http.request({ host: '127.0.0.1', port: 20128, path: '/v1/responses', method: 'POST', agent: false, signal: abort.signal,
      headers: { Authorization: apiAuthorization, Accept: 'text/event-stream', 'Content-Type': 'application/json', 'Content-Length': body.length } }, res => {
      if (res.statusCode !== 200 || !String(res.headers['content-type']).includes('text/event-stream')) { req.destroy(new Error('Authenticated SSE refused')); return; }
      const decoder = new StringDecoder('utf8');
      res.on('data', chunk => { try { state.push(decoder.write(chunk)); } catch (error) { req.destroy(error); } });
      res.on('error', error => reject(fault(error, true)));
      res.on('aborted', () => reject(fault(new Error('Held SSE aborted'), true)));
      res.on('end', () => { try { state.push(decoder.end()); assert(res.complete, 'Held SSE incomplete'); state.complete(true); ended = true; resolve(); } catch (error) { reject(fault(error, true)); } });
    });
    requests.add(req); req.once('close', () => requests.delete(req)); req.on('error', error => reject(fault(error, true)));
    req.setTimeout(240000, () => req.destroy(new Error('Held SSE deadline'))); req.end(body);
  });
  let streamError = null; completion.catch(error => { streamError = error; });
  const session = { kind: 'sse', probe, state, completion, get error() { return streamError; }, get open() { return !ended && !req.destroyed; }, close: () => req.destroy() };
  sessions.push(session); return session;
};
const openWs = async (authorization = apiAuthorization) => {
  const key = randomBytes(16).toString('base64'); let socket, expectedClose = false, closed = false, turn = null, closeCode = null;
  const reader = new wsHelpers.WsFrameReader();
  const handshake = new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port: 20128, path: '/v1/responses', agent: false, signal: abort.signal,
      headers: { Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': key,
        ...(authorization ? { Authorization: authorization } : {}) } });
    requests.add(req); req.once('close', () => requests.delete(req)); req.on('error', reject);
    req.setTimeout(30000, () => req.destroy(new Error('WebSocket handshake deadline')));
    req.once('response', res => { res.resume(); reject(new Error('Real Responses WebSocket upgrade refused')); });
    req.once('upgrade', (res, upgraded, head) => {
      try {
        assert.equal(res.statusCode, 101); assert.equal(res.headers['sec-websocket-accept'], wsHelpers.acceptKey(key));
        socket = upgraded; sockets.add(socket); socket.setTimeout(0); socket.setNoDelay(true);
        const accept = bytes => {
          try {
            for (const frame of reader.push(bytes)) {
              assert(!frame.oversized && frame.payload.length < 1048576, 'WebSocket oversized frame');
              if (frame.opcode === 8) {
                closeCode = frame.payload.length >= 2 ? frame.payload.readUInt16BE(0) : null;
                assert(expectedClose && closeCode === 1000, 'Unexpected Responses WebSocket close'); socket.end(); continue;
              }
              assert.equal(frame.opcode, 1, 'Unexpected Responses WebSocket frame');
              const event = JSON.parse(frame.payload.toString()); assert(turn, 'Unsolicited Responses WebSocket event');
              if (turn.unauthorized) {
                assert.equal(event.type, 'error'); assert.equal(event.status, 401, 'Unauthenticated WebSocket turn not refused');
                turn.done = true; continue;
              }
              assert.equal(event.stream_id, turn.probe.id, 'WebSocket response belongs to another turn'); turn.state.event(event);
              if (event.type === 'response.completed') { turn.state.complete(); turn.done = true; }
            }
            assert(reader.buf.length < 1048576, 'WebSocket partial frame oversized');
          } catch (error) { turn && (turn.error = error); fault(error, true); socket.destroy(error); }
        };
        socket.on('data', accept); socket.on('error', error => { if (turn) turn.error = error; if (!expectedClose) fault(error, true); });
        socket.once('close', () => { closed = true; sockets.delete(socket); if (!expectedClose) { const error = fault(new Error('Old WebSocket truncated'), true); if (turn) turn.error = error; } });
        if (head.length) accept(head); resolve();
      } catch (error) { upgraded.destroy(); reject(error); }
    }); req.end();
  });
  await bounded(handshake, 30000);
  const session = {
    kind: 'websocket', get open() { return !closed && !socket.destroyed; }, get turn() { return turn; },
    start(probe, unauthorized = false) {
      assert(this.open && (!turn || turn.done), 'Previous WebSocket turn incomplete');
      turn = { probe, state: streamState(probe), done: false, error: null, unauthorized };
      socket.write(wsHelpers.encodeTextFrame(JSON.stringify({ type: 'response.create', model: 'fixture/continuity-model', input: probe.id, stream_id: probe.id }), { mask: true }));
    },
    async finish() { await wait(() => { if (turn?.error) throw turn.error; assert(this.open, 'Old WebSocket closed'); return turn?.done; }); },
    async close() {
      if (closed) return;
      assert(!turn || turn.done, 'Cannot close an unfinished WebSocket turn'); expectedClose = true;
      // encodeCloseFrame is server-side/unmasked. Mask the client's close payload natively.
      const mask = randomBytes(4), payload = Buffer.from([0x03, 0xe8]);
      for (let index = 0; index < payload.length; index++) payload[index] ^= mask[index % 4];
      socket.write(Buffer.concat([Buffer.from([0x88, 0x82]), mask, payload]));
      await bounded(once(socket, 'close'), 10000); assert.equal(closeCode, 1000, 'WebSocket close acknowledgement missing');
    },
    destroy() { expectedClose = true; socket.destroy(); },
  };
  sessions.push(session); return session;
};
const releaseProbe = probe => {
  assert(probe.hold && probe.started && !probe.finished && !probe.released, 'Positive held provider barrier required');
  probe.released = true; probe.finish();
};
const providerPid = async socket => {
  const rows = sync('/usr/sbin/lsof', ['-nP', '-iTCP:' + providerPort, '-sTCP:ESTABLISHED', '-Fpn']).split('\n');
  let pid = null; const owners = new Set();
  const connection = `127.0.0.1:${socket.remotePort}->127.0.0.1:${providerPort}`;
  for (const line of rows) { if (line.startsWith('p')) pid = Number(line.slice(1)); if (line === 'n' + connection && Number.isSafeInteger(pid)) owners.add(pid); }
  assert.equal(owners.size, 1, 'Provider upstream connection owner ambiguous');
  const appPid = [...owners][0];
  // A candidate can issue the controller's readiness probe before deploy returns. Resolve it privately, not by version.
  if (!pidSlots.has(appPid)) for (const candidate of ['a', 'b']) {
    if (!exists(path.join(runtime, `${candidate}.ctl`))) continue;
    await status(candidate);
  }
  const slot = pidSlots.get(appPid);
  assert(['a', 'b'].includes(slot), 'Provider upstream does not belong to a privately proven app PID'); return { slot, appPid };
};
const startProvider = async providerSecret => {
  provider = http.createServer((req, res) => {
    const fail = error => { fault(error); if (!res.headersSent) res.writeHead(500, { 'Content-Type': 'application/json' }); res.end('{"error":"fixture refused"}'); };
    try {
      assert.equal(req.socket.remoteAddress, '127.0.0.1'); assert.equal(req.headers.authorization, `Bearer ${providerSecret}`, 'Fake provider auth missing');
      if (req.method === 'GET' && req.url === '/v1/models') {
        res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ object: 'list', data: [{ id: 'continuity-model', object: 'model', owned_by: 'guest-fixture' }] })); return;
      }
      assert.equal(req.method, 'POST'); assert.equal(req.url, '/v1/responses', 'Provider endpoint differs');
      let bytes = ''; req.on('data', chunk => { bytes += chunk; if (Buffer.byteLength(bytes) > 65536) req.destroy(new Error('Provider request oversized')); });
      req.on('error', error => fault(error));
      req.on('end', async () => {
        try {
          const body = JSON.parse(bytes); assert.equal(body.model, 'continuity-model');
          const identity = await providerPid(req.socket);
          // Verified controller stream_once sends exactly this one user prompt through Chat->Responses translation.
          const readiness = Array.isArray(body.input) && body.input.length === 1 && body.input[0].role === 'user'
            && Array.isArray(body.input[0].content) && body.input[0].content.length === 1
            && body.input[0].content[0].type === 'input_text' && body.input[0].content[0].text === 'Reply with: ok';
          const probe = readiness ? createProbe(identity.slot) : providerRequests.get(body.input);
          assert(probe && !probe.started, 'Unknown or replayed provider request');
          if (readiness) { report.controllerReadinessStreams ??= 0; report.controllerReadinessStreams++; }
          Object.assign(probe, identity);
          assert.equal(probe.slot, probe.expectedSlot, 'New or pinned request reached wrong private app');
          probe.started = true; probe.response = res; probe.marker = `fixture:${probe.slot}:${probe.appPid}:${probe.id}`;
          const responseId = 'resp_' + probe.id;
          const output = () => [{ id: 'msg_' + probe.id, type: 'message', role: 'assistant', status: 'completed',
            content: [{ type: 'output_text', text: `${probe.marker}:start:terminal`, annotations: [] }] }];
          if (body.stream !== true) {
            assert(!probe.hold, 'Held provider request must stream'); probe.finished = true;
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ id: responseId, object: 'response', status: 'completed', model: body.model, output: output(),
              usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } })); return;
          }
          res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
          const event = value => res.write(`event: ${value.type}\ndata: ${JSON.stringify(value)}\n\n`);
          event({ type: 'response.created', response: { id: responseId, object: 'response', status: 'in_progress', model: body.model, output: [] } });
          event({ type: 'response.output_text.delta', response_id: responseId, output_index: 0, content_index: 0, delta: `${probe.marker}:start` });
          const heartbeat = setInterval(() => res.write(': deterministic-hold\n\n'), 1000);
          res.once('close', () => { clearInterval(heartbeat); if (!probe.finished) fault(new Error('Provider held stream truncated'), true); });
          probe.finish = () => {
            assert(probe.released && !probe.finished && !res.destroyed, 'Provider barrier release invalid'); probe.finished = true; clearInterval(heartbeat);
            event({ type: 'response.output_text.delta', response_id: responseId, output_index: 0, content_index: 0, delta: ':terminal' });
            event({ type: 'response.output_item.done', response_id: responseId, output_index: 0, item: output()[0] });
            event({ type: 'response.completed', response: { id: responseId, object: 'response', status: 'completed', model: body.model, output: output(),
              usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } } }); res.end('data: [DONE]\n\n');
          };
          if (!probe.hold) probe.finish();
        } catch (error) { fail(error); }
      });
    } catch (error) { fail(error); }
  });
  provider.on('connection', socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)); });
  provider.listen(0, '127.0.0.1'); await once(provider, 'listening'); providerPort = provider.address().port;
  assert(![20128, 20129, 21128, 21130].includes(providerPort), 'Reserved managed port refused');
};
const httpProbe = async (slot, agent = false) => {
  const probe = createProbe(slot);
  const reply = await request('/v1/responses', { model: 'fixture/continuity-model', input: probe.id, stream: false }, apiAuthorization, undefined, agent);
  assert.equal(reply.status, 200, 'New authenticated HTTP request failed');
  const body = JSON.parse(reply.body); assert.equal(body.status, 'completed'); assertMarker(probe, body.output?.[0]?.content?.[0]?.text);
  if (agent) { publicClientSocket ??= reply.socket; assert.equal(reply.socket, publicClientSocket, 'Public keepalive client socket replaced'); }
  return probe;
};
const fileHashes = directory => {
  const hashes = {};
  const walk = current => {
    for (const name of fs.readdirSync(current).sort()) {
      const file = path.join(current, name), info = fs.lstatSync(file);
      assert(!info.isSymbolicLink() && info.uid === process.getuid() && !(info.mode & 0o022), 'Unsafe package entry');
      if (info.isDirectory()) walk(file);
      else { assert(info.isFile() && info.nlink === 1, 'Non-regular package entry'); hashes[path.relative(directory, file).split(path.sep).join('/')] = sha(fs.readFileSync(file)); }
    }
  }; walk(directory); assert(Object.keys(hashes).length > 0); return hashes;
};
try {
  assert.equal(sync('/usr/sbin/sysctl', ['-n', 'kern.hv_vmm_present']), '1', 'Virtual machine required');
  const executable = name => concrete(fs.realpathSync(sync('/usr/bin/which', [name])));
  const runtimeFiles = { controller: controllerFile, deployer: path.join(source, 'scripts/mac/9router_deploy.py'),
    worker: path.join(source, 'scripts/mac/9router_worker.cjs'), managed: path.join(source, 'src/lib/db/managed.cjs'),
    caddy_template: path.join(source, 'scripts/mac/templates/9router.Caddyfile'),
    worker_template: path.join(source, 'scripts/mac/templates/com.lfenergy.9router-worker.plist'),
    proxy_template: path.join(source, 'scripts/mac/templates/com.lfenergy.9router-proxy.plist'), node: fs.realpathSync(process.execPath), caddy: executable('caddy') };
  const runtimeHashes = Object.fromEntries(Object.entries(runtimeFiles).map(([key, file]) => [key, sha(regular(file))]));
  assert.deepEqual(runtimeHashes, scope.runtime_sha256, 'Controller/runtime scope binding differs');
  assert.equal(sync(runtimeFiles.caddy, ['version']), scope.proxy_version, 'Caddy scope binding differs');
  assert.equal(sync('git', ['-C', source, 'rev-parse', 'HEAD']), scope.source_commit, 'Source checkout revision differs');
  for (const [key, name] of Object.entries(jobNames)) { assert.equal(launchJob(name), null, 'Refuse pre-existing scoped launchd job'); jobs.set(key, null); }
  for (const port of [20128, 21128, 21130]) {
    const reservation = net.createServer(); reservation.listen(port, '127.0.0.1'); await once(reservation, 'listening'); await new Promise(resolve => reservation.close(resolve));
  }
  process.umask(0o077);
  for (const directory of [data, path.join(data, 'db'), path.join(data, 'releases'), stateDir, path.join(runRoot, 'tmp')]) {
    if (!exists(directory)) fs.mkdirSync(directory, { recursive: true, mode: 0o700 }); privateDir(directory);
  }
  // Inspect every tar entry before extraction. Reject links, duplicate paths, traversal, and special files.
  const archiveCheck = 'import sys,tarfile,pathlib,json\nwith tarfile.open(sys.argv[1],"r:gz") as t:\n m=t.getmembers();names=[str(pathlib.PurePosixPath(x.name)) for x in m]\n assert m and len(names)==len(set(names))\n for x in m:\n  p=pathlib.PurePosixPath(x.name);assert not p.is_absolute() and p.parts[0]=="package" and ".." not in p.parts and (x.isfile() or x.isdir()) and (len(p.parts)>1 or x.isdir())\n print(json.dumps({"entries":len(m),"regularFiles":sum(x.isfile() for x in m),"links":0}))';
  report.packageContents = JSON.parse(sync('python3', ['-c', archiveCheck, tarball]));
  const extracted = path.join(runRoot, 'package-extraction'); assert(!exists(extracted), 'Refuse stale extraction root'); fs.mkdirSync(extracted, { mode: 0o700 });
  const staging = path.join(extracted, 'package');
  sync('/usr/bin/tar', ['-xzf', tarball, '-C', extracted]);
  assert.equal(sha(regular(tarball)), expectedDigest);
  version = JSON.parse(regular(path.join(staging, 'package.json'))).version; assert.match(version, /^[A-Za-z0-9][A-Za-z0-9._-]*$/);
  assert.equal(version, JSON.parse(regular(path.join(source, 'cli/package.json'))).version);
  for (const release of Object.values(installations)) {
    assert.equal(path.relative(path.join(data, 'releases'), release).split(path.sep)[0], version, 'Worker release version confinement differs');
    fs.mkdirSync(path.dirname(release), { recursive: true, mode: 0o700 });
    fs.cpSync(staging, release, { recursive: true, errorOnExist: true, force: false });
    assert.deepEqual(fileHashes(release), packageBinding.package_files, 'Installed package differs from bound exact tarball');
  }
  installedHashes = fileHashes(installations.a); assert.deepEqual(fileHashes(installations.b), installedHashes);
  assert.equal(sha(regular(path.join(installations.a, 'app/custom-server.js'))), sha(regular(path.join(source, 'custom-server.js'))));
  assert.equal(sha(regular(path.join(installations.a, 'app/src/lib/db/managed.cjs'))), runtimeHashes.managed);
  const require = createRequire(import.meta.url); const { DatabaseSync } = require('node:sqlite');
  managed = require(path.join(installations.a, 'app/src/lib/db/managed.cjs'));
  const manifestFile = path.join(installations.a, 'app/hotswap-manifest.json'), manifest = managed.readManifest(manifestFile);
  assert.deepEqual(manifest, await managed.createManifest(source), 'Packaged persistence manifest differs from source');
  assert.equal(sha(regular(manifestFile)), packageBinding.manifest_sha256); assert.equal(manifest.persistenceFingerprint, packageBinding.persistenceFingerprint);
  // Reuse source helpers only after proving their packaged bytes. No ws npm dependency or home module lookup.
  const wsFile = path.join(source, 'open-sse/handlers/responsesWs/wsFrames.js');
  assert.equal(sha(regular(path.join(installations.a, 'app/handlers/responsesWs/wsFrames.js'))), sha(regular(wsFile)), 'WebSocket helper package binding differs');
  wsHelpers = await import(pathToFileURL(wsFile));
  const apiKey = 'guest-' + randomBytes(32).toString('hex'), providerSecret = randomBytes(32).toString('hex'), jwtSecret = randomBytes(32).toString('hex');
  const password = randomBytes(32).toString('hex'), apiSecret = randomBytes(32).toString('hex'), machineSalt = randomBytes(32).toString('hex');
  secrets.push(apiKey, providerSecret, jwtSecret, password, apiSecret, machineSalt); apiAuthorization = `Bearer ${apiKey}`;
  const encoded = value => Buffer.from(JSON.stringify(value)).toString('base64url');
  const unsigned = encoded({ alg: 'HS256' }) + '.' + encoded({ authenticated: true, iat: Math.floor(Date.now() / 1000), exp: Math.floor(Date.now() / 1000) + 1800 });
  cookie = 'auth_token=' + unsigned + '.' + createHmac('sha256', jwtSecret).update(unsigned).digest('base64url'); secrets.push(cookie);
  await startProvider(providerSecret);
  const dbFile = path.join(data, 'db/data.sqlite'); fs.closeSync(fs.openSync(dbFile, 'wx', 0o600));
  const db = new DatabaseSync(dbFile), now = new Date().toISOString(), providerId = 'openai-compatible-responses-guest-fixture';
  try {
    for (const type of ['table', 'index', 'view', 'trigger']) for (const row of manifest.layout.filter(row => row.type === type)) db.exec(row.sql);
    db.exec('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;');
    db.prepare('INSERT INTO _meta(key,value) VALUES(?,?)').run('schemaVersion', String(manifest.migrationVersion));
    db.prepare('INSERT INTO _meta(key,value) VALUES(?,?)').run('backupSchemaVersion', String(manifest.schemaVersion));
    db.prepare('INSERT INTO settings(id,data) VALUES(1,?)').run(JSON.stringify({ requireLogin: true, requireApiKey: true,
      tunnelEnabled: false, tailscaleEnabled: false, mitmEnabled: false, enableObservability: false,
      rtkEnabled: false, headroomEnabled: false, pxpipeEnabled: false, pxpipeAutoInstall: false,
      outboundProxyEnabled: false, enableRequestLogs: false, requestDetailsMode: 'off' }));
    const providerData = { prefix: 'fixture', apiType: 'responses', baseUrl: `http://127.0.0.1:${providerPort}/v1` };
    db.prepare('INSERT INTO providerNodes(id,type,name,data,createdAt,updatedAt) VALUES(?,?,?,?,?,?)')
      .run(providerId, 'openai-compatible', 'Guest deterministic provider', JSON.stringify(providerData), now, now);
    db.prepare('INSERT INTO providerConnections(id,provider,authType,name,priority,isActive,data,createdAt,updatedAt) VALUES(?,?,?,?,?,?,?,?,?)')
      .run('guest-provider-connection', providerId, 'apikey', 'Guest deterministic provider', 1, 1,
        JSON.stringify({ apiKey: providerSecret, testStatus: 'active', providerSpecificData: { ...providerData, connectionProxyEnabled: false } }), now, now);
    db.prepare('INSERT INTO apiKeys(id,key,name,machineId,isActive,createdAt) VALUES(?,?,?,?,?,?)')
      .run('guest-api-key', apiKey, 'Guest continuity client', 'guest-fixture', 1, now);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM apiKeys WHERE isActive=1').get().count, 1);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM providerConnections WHERE isActive=1').get().count, 1);
  } finally { db.close(); }
  const env = { DATA_DIR: data, JWT_SECRET: jwtSecret, API_KEY_SECRET: apiSecret, MACHINE_ID_SALT: machineSalt,
    INITIAL_PASSWORD: password, NODE_ENV: 'production', ENABLE_REQUEST_LOGS: 'false', REQUEST_DETAILS_MODE: 'off' };
  fs.writeFileSync(path.join(data, 'env.sh'), Object.entries(env).map(([key, value]) => `export ${key}="${value}"`).join('\n') + '\n', { mode: 0o600, flag: 'wx' });
  report.version = version; report.package_files = installedHashes; report.manifest_sha256 = packageBinding.manifest_sha256;
  report.installations = installations; report.persistenceFingerprint = manifest.persistenceFingerprint;
  try {
    await controller(['enroll', '--release', installations.a, '--digest', expectedDigest, '--acknowledge-maintenance']);
  } catch (error) {
    report.enrollmentModels = [];
    for (const [port, authenticated] of [[21128, false], [20128, false], [20128, true]]) {
      try {
        const reply = await request('/v1/models', undefined, authenticated ? apiAuthorization : undefined, undefined, false, port);
        const body = JSON.parse(reply.body);
        report.enrollmentModels.push({ port, authenticated, status: reply.status, modelCount: Array.isArray(body.data) ? body.data.length : null });
      } catch (diagnostic) { report.enrollmentModels.push({ port, authenticated, error: sanitize(diagnostic.message) }); }
    }
    throw error;
  }
  captureJob('a'); const initial = await status('a', 'active'); report.initialStatus = initial; const originalProxyPid = proxyPid();
  const noAuth = await request('/v1/responses', { model: 'fixture/continuity-model', input: 'unauthenticated', stream: false });
  assert.equal(noAuth.status, 401, 'API-key requirement unavailable');
  const unauthenticatedWs = await openWs(null); const unauthorizedProbe = createProbe('a');
  unauthenticatedWs.start(unauthorizedProbe, true); await unauthenticatedWs.finish(); await unauthenticatedWs.close();
  assert.equal(unauthorizedProbe.started, false, 'Unauthorized WebSocket reached provider');
  const models = await request('/v1/models', undefined, apiAuthorization); assert.equal(models.status, 200);
  assert(JSON.parse(models.body).data.some(model => model.id.endsWith('/continuity-model')), 'Seeded compatible provider model lookup unavailable');
  report.auth = { apiKeyRequired: true, unauthorizedHttpStatus: noAuth.status, unauthorizedWsTurnStatus: 401, deterministicOnly: true };
  // These small checks supplement, but do not replace, the deeper Task 4b side-effect fixture.
  for (const endpoint of ['update', 'shutdown']) for (const authenticated of [false, true]) {
    const before = await status('a', 'active'), reply = await request('/api/version/' + endpoint, {}, undefined, authenticated ? cookie : undefined);
    assert.equal(reply.status, authenticated ? 409 : 401, 'Packaged managed updater refusal differs');
    await sleep(1000); const after = await status('a', 'active'); assert.equal(after.appPid, before.appPid); assert.equal(proxyPid(), originalProxyPid);
    endpointChecks.push({ endpoint, authenticated, status: reply.status, app_pid_before: before.appPid, app_pid_after: after.appPid });
  }
  const agent = new http.Agent({ keepAlive: true, maxSockets: 1 });
  let retainedOldWs;
  try {
    await httpProbe('a', agent); // Preserve this public client keepalive agent across both controller transactions.
    for (const [from, to, command] of [['a', 'b', 'deploy-installed'], ['b', 'a', 'rollback']]) {
      const errorsBefore = measuredErrors, truncationsBefore = measuredTruncations;
      const item = { from, to, new_http_completed: 0, old_sse_open_before: 0, old_ws_open_before: 0,
        old_sse_terminal: 0, old_ws_turns_after: 0, errors: 0, truncations: 0, proxy_pid_before: proxyPid(), proxy_pid_after: null };
      transitions.push(item);
      const before = await status(from, 'active'), sseProbe = createProbe(from, true), wsProbe = createProbe(from, true);
      const sse = openSse(sseProbe), ws = await openWs(); ws.start(wsProbe);
      await wait(() => {
        if (sse.error) throw sse.error;
        if (ws.turn.error) throw ws.turn.error;
        assert(sse.open && ws.open, 'Held session closed before cutover');
        return sseProbe.started && wsProbe.started && sse.state.opened && ws.turn.state.opened && sse.open && ws.open;
      });
      const held = await status(from, 'active'); assert.equal(held.appPid, before.appPid); assert(held.connections >= 2, 'Old bridge barriers not positively open');
      assert(held.appWork.responses >= 1 && held.appWork.websocket >= 1, 'Old app work barriers not positively open');
      item.old_sse_open_before = 1; item.old_ws_open_before = 1; item.old_app_pid = held.appPid; item.old_connections_before = held.connections;
      if (command === 'deploy-installed') await controller([command, installations[to], '--digest', expectedDigest]);
      else await controller([command]);
      captureJob(to); const target = await status(to, 'active'), old = await status(from, 'draining');
      assert.equal(old.appPid, before.appPid, 'Healthy old app replaced'); assert.notEqual(target.appPid, old.appPid, 'Target private app identity not distinct');
      item.target_app_pid = target.appPid; item.target_release = installations[to];
      for (let index = 0; index < 100; index++) {
        const probe = await httpProbe(to, agent); assert.equal(probe.appPid, target.appPid);
        item.first_new_provider_marker ??= probe.marker; item.last_new_provider_marker = probe.marker; item.new_http_completed++;
      }
      assert(sse.open && ws.open && !sseProbe.finished && !wsProbe.finished, 'Old session barrier released before new target served');
      item.new_target_served_before_release = true;
      releaseProbe(sseProbe); releaseProbe(wsProbe);
      await bounded(sse.completion); await ws.finish(); item.old_sse_terminal = sse.state.terminal;
      item.old_sse_provider_marker = sseProbe.marker; item.old_ws_held_provider_marker = wsProbe.marker;
      item.old_ws_after_provider_markers = [];
      for (let index = 0; index < 2; index++) {
        const probe = createProbe(from); ws.start(probe); await ws.finish(); assert.equal(probe.appPid, before.appPid);
        item.old_ws_after_provider_markers.push(probe.marker); item.old_ws_turns_after++;
      }
      item.proxy_pid_after = proxyPid(); assert.equal(item.proxy_pid_after, item.proxy_pid_before); assert.equal(item.proxy_pid_after, originalProxyPid);
      item.errors = measuredErrors - errorsBefore; item.truncations = measuredTruncations - truncationsBefore;
      assert.equal(item.errors, 0); assert.equal(item.truncations, 0);
      if (from === 'a') retainedOldWs = ws; // Keep A occupied and healthy for controller rollback, not retirement/reinstallation.
      else { await ws.close(); await retainedOldWs.close(); }
    }
  } finally { agent.destroy(); }
  assert.equal(transitions.length, 2); assert.equal(measuredErrors, 0); assert.equal(measuredTruncations, 0);
  // All healthy sessions finish before the deliberate failure population begins.
  for (const session of sessions) if (session.kind === 'websocket') assert(!session.open, 'Owned healthy WebSocket still open before recovery');
  await wait(async () => {
    for (const slot of ['a', 'b']) {
      const value = await status(slot, slot === 'a' ? 'active' : 'draining');
      if (value.connections !== 0 || workKeys.some(key => value.appWork[key] !== 0)) return false;
    }
    return true;
  });
  const beforeCrash = await status('a', 'active'); const ownedJob = captureJob('a'), ownedPlistDigest = sha(regular(ownedJob.path));
  assert.equal(proxyPid(), originalProxyPid); process.kill(beforeCrash.appPid, 'SIGKILL');
  const recovery = { intentional_crashes: 1, recovered_requests: 0, app_pid_before: beforeCrash.appPid, app_pid_after: null,
    pid_source: 'private-worker-status', healthy_continuity_population: false, managed_job: jobNames.a };
  report.recovery = recovery;
  // The wrapper records the child failure. KeepAlive may already restore this exact job; never touch a baseline label.
  await wait(() => {
    try { process.kill(beforeCrash.appPid, 0); return false; } catch (error) { if (error.code !== 'ESRCH') throw error; }
    if (!exists(path.join(runtime, 'a.failed.json'))) return false;
    const failed = JSON.parse(regular(path.join(runtime, 'a.failed.json')));
    assert.equal(failed.appPid, beforeCrash.appPid); assert.equal(failed.slot, 'a'); return true;
  }, 30000);
  let alreadyRecovered = null;
  try { const value = await status('a', 'active'); if (value.appPid !== beforeCrash.appPid) alreadyRecovered = value; } catch {}
  if (!alreadyRecovered) {
    const loaded = launchJob(jobNames.a);
    assert(loaded && loaded.path === ownedJob.path, 'Recovery managed job identity changed');
    assert.equal(sha(regular(ownedJob.path)), ownedPlistDigest, 'Recovery managed plist changed');
    sync('launchctl', ['bootout', ownedJob.target]); await waitJobGone(jobNames.a, ownedJob.path);
    await wait(() => !exists(path.join(runtime, 'a.sock')) && !exists(path.join(runtime, 'a.ctl')), 15000);
    sync('launchctl', ['bootstrap', `gui/${process.getuid()}`, ownedJob.path]);
  }
  recovery.restoration = alreadyRecovered ? 'owned-job-KeepAlive' : 'owned-job-bootout-bootstrap';
  const afterCrash = await wait(async () => {
    try { const value = await status('a', 'active'); return value.appPid !== beforeCrash.appPid ? value : false; }
    catch (error) { if (abort.signal.aborted) throw error; return false; }
  });
  captureJob('a'); recovery.app_pid_after = afterCrash.appPid;
  const recovered = await httpProbe('a'); assert.equal(recovered.appPid, afterCrash.appPid); recovery.recovered_requests = 1;
  assert.equal(proxyPid(), originalProxyPid); recovery.proxy_pid_preserved = true;
  for (const release of Object.values(installations)) assert.deepEqual(fileHashes(release), installedHashes, 'Referenced immutable release changed');
  assert.equal(sha(regular(tarball)), expectedDigest); assert(regular(scopeFile).equals(scopeBytes), 'Qualification scope mutated');
  report.population = { healthyTransitions: 2, newAuthenticatedHttpCompleted: transitions.reduce((sum, item) => sum + item.new_http_completed, 0),
    oldSseTerminals: 2, oldWsFreshTurnsAfter: 4, managedEndpointRefusals: endpointChecks.length, intentionalCrashes: 1, skipped: 0 };
  qualified = true;
} catch (error) {
  failures.push({ phase: 'fixture', type: error.name, reason: sanitize(error.message) });
} finally {
  clearTimeout(batchTimer); cleaning = true;
  // Do not reset guest state while owned streams or activating controller children still run.
  for (const probe of providerRequests.values()) if (probe.hold && probe.started && !probe.finished) {
    try { if (!probe.released) releaseProbe(probe); } catch (error) { failures.push({ phase: 'session-cleanup', reason: sanitize(error.message) }); }
  }
  for (const session of sessions) {
    try {
      if (session.kind === 'sse') { if (session.probe.started) await bounded(session.completion, 15000); else session.close(); }
      else { if (session.open && session.turn && !session.turn.done) await session.finish(); await session.close(); }
    } catch (error) {
      failures.push({ phase: 'session-cleanup', reason: sanitize(error.message) });
      session.kind === 'sse' ? session.close() : session.destroy();
    }
  }
  const closingRequests = [...requests].map(req => req.closed ? Promise.resolve() : new Promise(resolve => req.once('close', resolve)));
  for (const req of requests) req.destroy();
  try { await bounded(Promise.all(closingRequests), 10000); }
  catch (error) { failures.push({ phase: 'session-cleanup', reason: sanitize(error.message) }); }
  for (const group of controllerGroups) {
    try {
      const alive = () => { try { process.kill(-group, 0); return true; } catch (error) { if (error.code === 'ESRCH') return false; throw error; } };
      if (alive()) process.kill(-group, 'SIGTERM');
      let until = performance.now() + 10000;
      while (alive() && performance.now() < until) await delay(100);
      if (alive()) process.kill(-group, 'SIGKILL');
      until = performance.now() + 5000;
      while (alive() && performance.now() < until) await delay(100);
      assert(!alive(), 'Owned controller group remains');
    } catch (error) { failures.push({ phase: 'controller-cleanup', reason: sanitize(error.message) }); }
  }
  if (children.size) failures.push({ phase: 'controller-cleanup', reason: 'Owned controller handle remains' });
  const closingSockets = [...sockets].map(socket => socket.closed ? Promise.resolve() : new Promise(resolve => socket.once('close', resolve)));
  for (const socket of sockets) socket.destroy();
  try {
    await bounded(Promise.all(closingSockets), 10000);
    if (provider) await bounded(new Promise((resolve, reject) => provider.close(error => error ? reject(error) : resolve())), 10000);
  } catch (error) { failures.push({ phase: 'session-cleanup', reason: sanitize(error.message) }); }
  report.fixtureSessionsClosed = requests.size === 0 && sockets.size === 0 && children.size === 0;
  if (!report.fixtureSessionsClosed) failures.push({ phase: 'session-cleanup', reason: 'Owned fixture sessions remain' });
  if (report.fixtureSessionsClosed && qualified) {
    try {
      await wait(async () => {
        for (const slot of ['a', 'b']) {
          const value = await status(slot);
          if (value.connections !== 0 || workKeys.some(key => value.appWork[key] !== 0)) return false;
        }
        return true;
      }, 30000);
      report.ownedAppWorkQuiescent = true;
    } catch (error) { report.ownedAppWorkQuiescent = false; failures.push({ phase: 'session-cleanup', reason: sanitize(error.message) }); }
  }
  // Bootout only bound test jobs. Retain every release tree; do not infer that a journal reference is disposable.
  for (const key of ['b', 'a', 'proxy']) {
    try {
      const name = jobNames[key], loaded = launchJob(name);
      if (!loaded) continue;
      assert.equal(loaded.path, path.join(stateDir, `${key}.plist`), 'Cleanup job owner differs');
      assert(jobs.has(key), 'Fixture did not reserve this job identity');
      ownedPids.add(loaded.pid);
      sync('launchctl', ['bootout', loaded.target]); await waitJobGone(name, loaded.path);
    } catch (error) { failures.push({ phase: 'job-cleanup', reason: sanitize(error.message) }); }
  }
  try {
    await wait(() => [...ownedPids].every(pid => {
      if (!Number.isSafeInteger(pid) || pid <= 0) return true;
      try { process.kill(pid, 0); return false; } catch (error) { if (error.code === 'ESRCH') return true; throw error; }
    }), 15000);
    report.ownedManagedProcessesExited = true;
  } catch (error) { report.ownedManagedProcessesExited = false; failures.push({ phase: 'job-cleanup', reason: sanitize(error.message) }); }
  report.managedJobsClosed = !failures.some(failure => failure.phase === 'job-cleanup');
  report.devboxCleanupOwnedByParent = true; report.credentialFilesRetainedPrivately = true;
  report.errors = measuredErrors; report.truncations = measuredTruncations;
  report.continuity = { provider: report.provider, transitions };
  try { report.tarballSha256After = sha(regular(tarball)); assert.equal(report.tarballSha256After, expectedDigest); }
  catch (error) { failures.push({ phase: 'digest-final', reason: sanitize(error.message) }); }
  report.success = qualified && failures.length === 0 && report.fixtureSessionsClosed && report.managedJobsClosed;
  for (const [signal, handler] of Object.entries(signalHandlers)) process.removeListener(signal, handler);
  fs.writeFileSync(resultFile, sanitize(JSON.stringify(report, null, 2)) + '\n', { mode: 0o600, flag: 'wx' });
  console.log(JSON.stringify({ provider: report.provider, transitions: transitions.length,
    new_authenticated_http_completed: transitions.reduce((sum, item) => sum + item.new_http_completed, 0), success: report.success }));
  process.exitCode = report.success ? 0 : 2;
}
// ponytail: deterministic provider requests use PID-bound guest TCP observations, not upstream credential qualification.
// Add real-provider auth evidence separately. The parent verifies Namespace shutdown twice before any overall verdict.
