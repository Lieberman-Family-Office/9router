const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { once } = require('node:events');
const test = require('node:test');
const { setTimeout: delay } = require('node:timers/promises');

// Independent competing-listener oracle. The real Next packaged fixture remains required.
test('Responses upgrade has one owner; unrelated upgrades retain their listener', { timeout: 10000 }, async () => {
  const original = http.createServer;
  const previous = process.env.NINEROUTER_SKIP_BACKGROUND_REFRESH;
  process.env.NINEROUTER_SKIP_BACKGROUND_REFRESH = '1';
  delete require.cache[require.resolve('../../custom-server.js')];
  require('../../custom-server.js');
  const peers = new Set();
  let competing = 0;
  const server = http.createServer(async (_req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.end('data: {"type":"response.created","response":{"id":"owned-response","status":"in_progress"}}\n\ndata: {"type":"response.completed","response":{"id":"owned-response","status":"completed"}}\n\n');
  });
  server.on('connection', socket => { peers.add(socket); socket.once('close', () => peers.delete(socket)); });
  // Match Next's independent upgrade handler: it ends sockets for matched app routes.
  server.on('upgrade', (req, socket) => {
    competing++;
    if (req.url === '/v1/responses') socket.end();
    else socket.end('HTTP/1.1 418 Unrelated Upgrade\r\nConnection: close\r\n\r\n');
  });
  let socket;
  try {
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    // Preload the actual module before listening; attachment settles on the next event-loop turn.
    await import('../../open-sse/handlers/responsesWs/index.js');
    await delay(100);
    const { WsFrameReader, encodeTextFrame } = await import('../../open-sse/handlers/responsesWs/wsFrames.js');
    const reader = new WsFrameReader();
    const events = [];
    const req = http.request({ host: '127.0.0.1', port: server.address().port, path: '/v1/responses', headers: { Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==' } });
    req.end();
    const [, upgraded] = await once(req, 'upgrade');
    socket = upgraded;
    let ended = false;
    socket.on('close', () => { ended = true; });
    socket.on('error', () => { ended = true; });
    socket.on('data', bytes => { for (const frame of reader.push(bytes)) if (frame.opcode === 1) events.push(JSON.parse(frame.payload.toString())); });
    for (const id of ['first', 'second']) {
      socket.write(encodeTextFrame(JSON.stringify({ type: 'response.create', model: 'fixture/model', input: id, stream_id: id }), { mask: true }));
      const until = Date.now() + 3000;
      while (!events.some(event => event.type === 'response.completed' && event.stream_id === id) && Date.now() < until) {
        assert(!ended, 'Responses socket was ended by competing app-route handler');
        await delay(10);
      }
      assert(events.some(event => event.type === 'response.completed' && event.stream_id === id), 'Responses terminal missing');
    }
    assert.equal(competing, 0, 'Responses upgrade reached a second owner');
    const unrelated = net.connect({ host: '127.0.0.1', port: server.address().port });
    await once(unrelated, 'connect');
    let reply = '';
    unrelated.on('data', bytes => { reply += bytes; });
    unrelated.write('GET /unrelated HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: other\r\n\r\n');
    await once(unrelated, 'end');
    assert.match(reply, /^HTTP\/1\.1 418/);
    assert.equal(competing, 1, 'unrelated upgrade listener was suppressed');
  } finally {
    socket?.destroy();
    for (const peer of peers) peer.destroy();
    await new Promise(resolve => server.close(resolve));
    http.createServer = original;
    if (previous === undefined) delete process.env.NINEROUTER_SKIP_BACKGROUND_REFRESH;
    else process.env.NINEROUTER_SKIP_BACKGROUND_REFRESH = previous;
  }
});
