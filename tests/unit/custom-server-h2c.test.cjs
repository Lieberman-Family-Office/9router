const assert = require("node:assert/strict");
const http = require("node:http");
const net = require("node:net");
const test = require("node:test");

for (const framing of ["split", "coalesced", "chunked", "empty"]) {
  test(`serves ${framing} h2c POST requests as HTTP/1.1`, { timeout: 5_000 }, async () => {
    const originalCreateServer = http.createServer;
    delete require.cache[require.resolve("../../custom-server.js")];
    require("../../custom-server.js");

    const sockets = new Set();
    let client;
    const body = framing === "empty" ? "" : '{"model":"test","stream":true}';
    const server = http.createServer(async (req, res) => {
      assert.equal(req.url, "/v1/chat/completions");
      assert.equal(req.headers.upgrade, undefined);
      assert.equal(req.headers["http2-settings"], undefined);
      assert.equal(req.headers.connection, "close");
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      assert.equal(Buffer.concat(chunks).toString("utf8"), body);
      res.setHeader("Content-Type", "text/event-stream");
      res.end("data: [DONE]\n\n");
    });
    server.on("connection", (socket) => {
      sockets.add(socket);
      socket.once("close", () => sockets.delete(socket));
    });
    server.on("upgrade", (_req, socket) => socket.destroy());

    try {
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", resolve);
      });
      const port = server.address().port;
      const response = await new Promise((resolve, reject) => {
        const chunks = [];
        const socket = client = net.createConnection({ host: "127.0.0.1", port }, () => {
          const headers = [
            "POST /v1/chat/completions HTTP/1.1",
            `Host: 127.0.0.1:${port}`,
            "Connection: Upgrade, HTTP2-Settings",
            "Upgrade: h2c",
            "HTTP2-Settings: AAEAAEAAAAIAAAAAAAMAAAAAAAQBAAAAAAUAAEAAAAYABgAA",
            framing === "chunked" ? "Transfer-Encoding: chunked" : `Content-Length: ${Buffer.byteLength(body)}`,
            "Content-Type: application/json",
            "",
            "",
          ].join("\r\n");
          if (framing === "coalesced" || framing === "empty") socket.write(headers + body);
          else {
            socket.write(headers);
            setTimeout(() => socket.write(framing === "chunked"
              ? `${Buffer.byteLength(body).toString(16)}\r\n${body}\r\n0\r\n\r\n` : body), 25);
          }
        });
        socket.setTimeout(2_000, () => {
          socket.destroy();
          reject(new Error("h2c fallback response timed out"));
        });
        socket.on("data", (chunk) => chunks.push(chunk));
        socket.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
        socket.on("error", reject);
      });

      assert.match(response, /^HTTP\/1\.1 200 OK\r\n/);
      assert.match(response, /\r\nContent-Type: text\/event-stream\r\n/i);
      assert.match(response, /\r\nConnection: close\r\n/i);
      assert.match(response, /\r\n\r\ndata: \[DONE\]\n\n$/);
    } finally {
      client?.destroy();
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      http.createServer = originalCreateServer;
    }
  });
}

test("preserves non-h2c upgrade selection", { timeout: 5_000 }, async () => {
  const originalCreateServer = http.createServer;
  delete require.cache[require.resolve("../../custom-server.js")];
  require("../../custom-server.js");
  const sockets = new Set();
  let selected = 0;
  let upgraded = 0;
  const server = http.createServer({ shouldUpgradeCallback(req) {
    selected++;
    return req.headers.upgrade === "websocket";
  } }, (_req, res) => res.end("ordinary request"));
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  server.on("upgrade", (req, socket) => {
    upgraded++;
    assert.equal(req.headers.upgrade, "websocket");
    socket.end("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n");
  });
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    for (const protocol of ["websocket", "other"]) {
      const response = await new Promise((resolve, reject) => {
        const chunks = [];
        const socket = net.createConnection({ host: "127.0.0.1", port: server.address().port }, () => {
          socket.write(`GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close, Upgrade\r\nUpgrade: ${protocol}\r\n\r\n`);
        });
        socket.setTimeout(2_000, () => { socket.destroy(); reject(new Error("upgrade selection timed out")); });
        socket.on("data", (chunk) => chunks.push(chunk));
        socket.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
        socket.on("error", reject);
      });
      assert.match(response, protocol === "websocket" ? /^HTTP\/1\.1 101/ : /^HTTP\/1\.1 200/);
    }
    assert.equal(selected, 2);
    assert.equal(upgraded, 1);
  } finally {
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
    http.createServer = originalCreateServer;
  }
});
