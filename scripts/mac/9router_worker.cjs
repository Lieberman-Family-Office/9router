// Pinned slot wrapper. Controller owns routing and launchd; this process owns only its child.
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { setTimeout: delay } = require('node:timers/promises');
const { performance } = require('node:perf_hooks');
const managed = require('../../src/lib/db/managed.cjs');

function configuration(file) {
  managed.privateFile(file);
  const c = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!['a', 'b'].includes(c.slot) || typeof c.version !== 'string' ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(c.version) || !Number.isSafeInteger(c.port) ||
      c.port < 1024 || c.port > 65535 || [20128, 20129].includes(c.port)) throw new Error('Invalid slot configuration');
  for (const directory of [c.runtime, c.dataDir]) managed.privateDirectory(directory);
  if (typeof c.release !== 'string' || !path.isAbsolute(c.release)) throw new Error('Invalid release path');
  managed.privateDirectory(path.join(process.env.HOME, '.9router'));
  const releases = path.join(process.env.HOME, '.9router/releases');
  const relative = path.relative(releases, c.release);
  if (!relative || relative.split(path.sep)[0] !== c.version || relative.startsWith('..') || path.isAbsolute(relative) || fs.realpathSync(c.release) !== c.release) {
    throw new Error('Release must be concrete and confined');
  }
  const directories = [releases];
  let directory = releases;
  for (const segment of relative.split(path.sep)) { directory = path.join(directory, segment); directories.push(directory); }
  directories.push(path.join(c.release, 'app'));
  for (const entry of directories) {
    const stat = fs.lstatSync(entry);
    if (!stat.isDirectory() || stat.isSymbolicLink() || stat.uid !== process.getuid() || (stat.mode & 0o022)) throw new Error('Unsafe release');
  }
  for (const entry of ['package.json', 'app/custom-server.js']) {
    const stat = fs.lstatSync(path.join(c.release, entry));
    if (!stat.isFile() || stat.isSymbolicLink() || stat.uid !== process.getuid() || (stat.mode & 0o022)) throw new Error('Unsafe release entry');
  }
  if (JSON.parse(fs.readFileSync(path.join(c.release, 'package.json'), 'utf8')).version !== c.version) throw new Error('Release version mismatch');
  for (const suffix of ['sock', 'ctl']) {
    const socket = path.join(c.runtime, `${c.slot}.${suffix}`);
    if (Buffer.byteLength(socket) > 103) throw new Error('Socket pathname too long');
    if (fs.existsSync(socket)) throw new Error('Slot socket already occupied');
  }
  return c;
}
function verifyVersion(c) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port: c.port, path: '/api/version', agent: false }, res => {
      let bytes = '';
      res.on('data', chunk => { bytes += chunk; if (Buffer.byteLength(bytes) > 4096) req.destroy(new Error('Version response oversized')); });
      res.on('error', reject);
      res.on('end', () => {
        try { if (res.statusCode !== 200 || JSON.parse(bytes).currentVersion !== c.version) throw new Error('Private version mismatch'); resolve(); }
        catch (error) { reject(error); }
      });
    });
    req.setTimeout(5000, () => req.destroy(new Error('Private version timeout')));
    req.on('error', reject);
  });
}
async function main() {
  const config = configuration(process.argv[2]);
  process.umask(0o077);
  const reservation = net.createServer();
  reservation.listen(config.port, '127.0.0.1');
  await once(reservation, 'listening');
  await new Promise(resolve => reservation.close(resolve));
  const app = spawn(process.execPath, ['--dns-result-order=ipv4first', '--max-old-space-size=6144',
    path.join(config.release, 'app/custom-server.js')], {
    cwd: path.join(config.release, 'app'), stdio: ['inherit', 'inherit', 'inherit', 'ipc'],
    env: { ...process.env, NODE_ENV: 'production', HOSTNAME: '127.0.0.1', PORT: String(config.port),
      DATA_DIR: config.dataDir, NINEROUTER_ATTACHED_SERVER: '1', NINEROUTER_MANAGED_WORKER: '1',
      NINEROUTER_SLOT: config.slot, NINEROUTER_HOTSWAP_RUNTIME: config.runtime },
  });
  let mode = 'starting';
  let intentional = false;
  let failed = false;
  let draining = false;
  let busy = false;
  let serial = 0;
  let awaySince = null;
  let lastPoll = performance.now();
  const pending = new Map();
  const pipes = new Set();
  const controlSockets = new Set();
  const active = () => {
    const route = path.join(config.runtime, 'active.sock');
    const stat = fs.lstatSync(route);
    if (!stat.isSymbolicLink() || stat.uid !== process.getuid()) throw new Error('Unknown active route');
    const target = path.resolve(config.runtime, fs.readlinkSync(route));
    if (![path.join(config.runtime, 'a.sock'), path.join(config.runtime, 'b.sock')].includes(target) || !fs.lstatSync(target).isSocket()) throw new Error('Unknown route target');
    return target === path.join(config.runtime, `${config.slot}.sock`);
  };
  const poll = () => {
    const now = performance.now();
    // ponytail: polling cannot certify a stall; restart the quiet proof after a >1s gap.
    if (now - lastPoll > 1000) awaySince = null;
    lastPoll = now;
    try {
      if (!draining || active()) awaySince = null;
      else awaySince ??= now;
    } catch { awaySince = null; }
  };
  const timer = setInterval(poll, 100);
  const bridge = net.createServer(incoming => {
    awaySince = null;
    const outgoing = net.connect({ host: '127.0.0.1', port: config.port });
    const pair = { incoming, outgoing };
    pipes.add(pair);
    const remove = () => { incoming.destroy(); outgoing.destroy(); };
    const closed = () => { if (incoming.closed && outgoing.closed) pipes.delete(pair); };
    incoming.once('error', remove); outgoing.once('error', remove);
    incoming.once('close', () => { remove(); closed(); }); outgoing.once('close', () => { remove(); closed(); });
    incoming.pipe(outgoing); outgoing.pipe(incoming);
  });
  const ipc = async (op) => {
    if (!app.connected || app.exitCode !== null) throw new Error('Unknown app work');
    const id = ++serial;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error('Unknown app work')); }, 2000);
      pending.set(id, value => { clearTimeout(timeout); resolve(value); });
      app.send({ type: '9router-managed', id, op, draining }, error => {
        if (error) { clearTimeout(timeout); pending.delete(id); reject(new Error('Unknown app work')); }
      });
    });
  };
  app.on('message', message => {
    if (message?.type === '9router-managed' && Number.isSafeInteger(message.id)) {
      const done = pending.get(message.id);
      if (done) { pending.delete(message.id); done(message.work); }
    }
  });
  const validateWork = work => {
    const keys = ['responses', 'handlers', 'upgrades', 'cleanup', 'persistence', 'refresh', 'background', 'quota', 'websocket'];
    if (!work || work.initialized !== true || work.unknown !== false ||
        keys.some(key => !Number.isSafeInteger(work[key]) || work[key] < 0)) throw new Error('Unknown app work');
    return keys.every(key => work[key] === 0);
  };
  const status = async () => {
    let appWork = null;
    if (mode !== 'stopped') { try { appWork = await ipc('status'); validateWork(appWork); } catch { appWork = null; } }
    let reported = mode;
    if (mode === 'ready') { try { if (active()) reported = 'active'; } catch {} }
    return { slot: config.slot, version: config.version, mode: reported, connections: pipes.size, appPid: app.pid, appWork };
  };
  const closeBridge = () => new Promise((resolve, reject) => bridge.close(error => error ? reject(error) : resolve()));
  const listenBridge = () => new Promise((resolve, reject) => {
    if (mode === 'failed') { reject(new Error('App failed')); return; }
    bridge.once('error', reject);
    bridge.listen(path.join(config.runtime, `${config.slot}.sock`), () => { bridge.off('error', reject); resolve(); });
  });
  const requireQuiet = () => {
    poll();
    if (!draining || awaySince === null || performance.now() - awaySince < 10000 || pipes.size || active()) {
      throw new Error('Retirement not quiescent');
    }
  };
  const operate = async op => {
    if (op === 'status') return status();
    if (!['drain', 'resume', 'stop'].includes(op) || busy || mode === 'stopped' || mode === 'failed') throw new Error('Operation refused');
    busy = true;
    try {
      if (op === 'drain') {
        draining = true; mode = 'draining'; awaySince = null;
        await ipc('drain');
      } else if (op === 'resume') {
        await verifyVersion(config);
        draining = false;
        await ipc('resume');
        mode = 'ready'; awaySince = null;
      } else {
        requireQuiet();
        const initialWork = await ipc('status');
        requireQuiet();
        if (!validateWork(initialWork)) throw new Error('App still working');
        // Seal only after the initial proof; a late accepted connection invalidates it.
        const closing = closeBridge();
        if (pipes.size) {
          // Never await a held stream here. Reopen only when close naturally completes.
          closing.then(listenBridge).catch(() => { mode = 'failed'; });
          throw new Error('Late dial refused retirement');
        }
        try {
          await closing;
          requireQuiet();
          const finalWork = await ipc('status');
          requireQuiet();
          if (!validateWork(finalWork)) throw new Error('Final quiescence unproven');
          // Requires controller exclusion of route mutations during retirement (Task 4).
          requireQuiet();
        } catch (error) { await listenBridge(); throw error; }
        intentional = true;
        const exited = once(app, 'exit');
        app.kill('SIGTERM');
        await exited;
        mode = 'stopped';
      }
      return status();
    } finally { busy = false; }
  };
  const control = net.createServer({ allowHalfOpen: true }, socket => {
    controlSockets.add(socket);
    let bytes = Buffer.alloc(0);
    let handled = false;
    // Absolute receipt deadline only: complete commands and accepted work remain unbounded.
    const receiptDeadline = setTimeout(() => socket.destroy(), 5000);
    socket.once('close', () => { clearTimeout(receiptDeadline); controlSockets.delete(socket); });
    socket.on('error', () => {});
    socket.on('data', chunk => {
      if (handled) return;
      bytes = Buffer.concat([bytes, chunk]);
      const newline = bytes.indexOf(10);
      if (bytes.length > 4096 || (newline >= 0 && newline !== bytes.length - 1)) {
        handled = true; clearTimeout(receiptDeadline);
        socket.end(JSON.stringify({ slot: config.slot, error: 'Malformed control command' }) + '\n'); return;
      }
      if (newline < 0) return;
      handled = true;
      clearTimeout(receiptDeadline);
      Promise.resolve().then(() => {
        const value = JSON.parse(bytes.toString());
        if (!value || Object.keys(value).length !== 1 || typeof value.op !== 'string') throw new Error('Malformed control command');
        return operate(value.op);
      }).then(value => socket.end(JSON.stringify(value) + '\n'), error => socket.end(JSON.stringify({ slot: config.slot, error: error.message }) + '\n'));
    });
  });
  const fail = (code, signal, error) => {
    if (intentional || failed) return;
    failed = true;
    mode = 'failed';
    process.exitCode = 1;
    try {
      fs.writeFileSync(path.join(config.runtime, `${config.slot}.failed.json`), JSON.stringify({
        slot: config.slot, version: config.version, appPid: app.pid ?? null, code, signal,
        ...(error ? { error: { code: error.code ?? null, message: error.message } } : {}),
      }) + '\n', { mode: 0o600 });
    } catch (evidenceError) { console.error('[ManagedWorker] Failure evidence write failed:', evidenceError.message); }
    clearInterval(timer);
    for (const pair of pipes) { pair.incoming.destroy(); pair.outgoing.destroy(); }
    for (const socket of controlSockets) socket.destroy();
    bridge.close(); control.close();
    if (app.exitCode === null && app.signalCode === null) app.kill('SIGTERM');
    // Only owned socket paths; failed evidence is retained for the controller.
    for (const suffix of ['sock', 'ctl']) { try { fs.unlinkSync(path.join(config.runtime, `${config.slot}.${suffix}`)); } catch {} }
  };
  app.on('error', error => fail(null, null, error));
  app.on('exit', (code, signal) => fail(code, signal));
  const shutdown = () => {
    intentional = true;
    clearInterval(timer);
    bridge.close(); control.close();
    for (const pair of pipes) { pair.incoming.destroy(); pair.outgoing.destroy(); }
    for (const socket of controlSockets) socket.destroy();
    if (app.exitCode === null) app.kill('SIGTERM');
  };
  process.once('SIGTERM', shutdown); process.once('SIGINT', shutdown);
  try {
    const until = performance.now() + 30000;
    while (true) {
      if (mode === 'failed' || app.exitCode !== null) throw new Error('App failed during startup');
      try { await verifyVersion(config); break; } catch {
        if (mode === 'failed') throw new Error('App failed during startup');
        if (performance.now() > until) throw new Error('Private app readiness unproven');
        await delay(100);
      }
    }
    validateWork(await ipc('status')); // Startup ticks may be working; readiness is not retirement.
    await listenBridge();
    control.listen(path.join(config.runtime, `${config.slot}.ctl`));
    await once(control, 'listening');
    mode = 'ready';
  } catch (error) { shutdown(); throw error; }
}
main().catch(error => { console.error('[ManagedWorker]', error.message); process.exitCode = 1; });
