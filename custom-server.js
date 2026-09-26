const http = require("http");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");
const { pathToFileURL } = require("url");

const origCreate = http.createServer.bind(http);

// Per-process secret proving x-9r-real-ip was stamped below rather than sent by the client.
// A bare `next start` / `next dev` never loads this file, so it cannot produce a matching
// header even though the env var is inherited by child processes. Named like x-9r-cli-token
// so the request-detail header sanitizer redacts it too.
const PEER_TOKEN = crypto.randomBytes(24).toString("hex");
process.env.NINEROUTER_PEER_TOKEN = PEER_TOKEN;

let backgroundRefreshStarted = false;
let responsesWsStarted = false;

function startBackgroundTokenRefreshFromCustomServer() {
  if (backgroundRefreshStarted) return;
  // Unit tests require() this module without wanting the scheduler open-handle.
  if (process.env.NINEROUTER_SKIP_BACKGROUND_REFRESH === "1") return;
  if (require.main !== module && process.env.NINEROUTER_ATTACHED_SERVER !== "1") return;
  backgroundRefreshStarted = true;
  // Prefer source path (repo / standalone that still has src). Fail-open if missing
  // — initializeApp also starts the same scheduler when the Next app boots.
  const modPath = path.join(__dirname, "src", "sse", "services", "backgroundTokenRefresh.js");
  import(pathToFileURL(modPath).href)
    .then((m) => {
      try {
        m.startBackgroundTokenRefresh();
      } catch (e) {
        console.error("[BackgroundTokenRefresh] start failed:", e && e.message ? e.message : e);
      }
      const stop = () => {
        try {
          m.stopBackgroundTokenRefresh();
        } catch {
          /* ignore */
        }
      };
      process.once("SIGINT", stop);
      process.once("SIGTERM", stop);
    })
    .catch((e) => {
      // Expected in published CLI standalone (src/ not on disk). App bootstrap covers it.
      if (process.env.DEBUG_BACKGROUND_TOKEN_REFRESH) {
        console.error("[BackgroundTokenRefresh] import failed:", e && e.message ? e.message : e);
      }
    });
}

/**
 * Mid-turn steering: accept WebSocket upgrades on /v1/responses.
 * Loads open-sse ESM when present (repo/dev); falls back to ~/.9router/lib/responses-ws
 * for the published CLI install hot-patch.
 * Set NINEROUTER_SKIP_RESPONSES_WS=1 to disable (e.g. unit tests that require this module).
 */
function startResponsesWsFromCustomServer(server) {
  if (responsesWsStarted || !server) return;
  if (process.env.NINEROUTER_SKIP_RESPONSES_WS === "1") return;
  // Always attach on the live Next server. Unit tests that require() this module
  // without wanting WS should set NINEROUTER_SKIP_RESPONSES_WS=1.
  responsesWsStarted = true;
  const candidates = [
    path.join(__dirname, "open-sse", "handlers", "responsesWs", "index.js"),
    path.join(__dirname, "handlers", "responsesWs", "index.js"),
    path.join(process.env.HOME || "", ".9router", "lib", "responses-ws", "index.mjs"),
  ];
  const tryAttach = async () => {
    let lastErr = null;
    for (const modPath of candidates) {
      if (!fs.existsSync(modPath)) continue;
      try {
        const m = await import(pathToFileURL(modPath).href);
        const attach = m.attachResponsesWebSocket || m.installOnServer || m.default?.attachResponsesWebSocket;
        if (typeof attach !== "function") continue;
        const addr = server.address();
        const localPort = addr && typeof addr === "object" ? addr.port : Number(process.env.PORT) || 20128;
        attach(server, { localPort });
        console.log(`[ResponsesWS] mid-turn steering enabled on /v1/responses (port ${localPort})`);
        return;
      } catch (e) {
        lastErr = e;
      }
    }
    if (process.env.DEBUG_RESPONSES_WS || lastErr) {
      console.error("[ResponsesWS] attach skipped:", lastErr && lastErr.message ? lastErr.message : "module not found");
    }
  };
  tryAttach().catch((e) => {
    console.error("[ResponsesWS] attach failed:", e && e.message ? e.message : e);
  });
}

// Wrap Next standalone HTTP server: derive client IP from the TCP socket
// (unspoofable) and strip client-supplied forwarding headers so downstream
// rate-limiting keys on the real peer address instead of attacker-controlled XFF.
http.createServer = (...args) => {
  const handler = args.find((a) => typeof a === "function");
  const rest = args.filter((a) => typeof a !== "function");
  if (!handler) return origCreate(...args);
  const wrapped = (req, res) => {
    const socketIp = req.socket && req.socket.remoteAddress ? req.socket.remoteAddress : "";
    const xff = req.headers["x-forwarded-for"];
    const xRealIp = req.headers["x-real-ip"];
    const viaProxy = !!(xff || xRealIp);
    const isLoopbackProxy = socketIp === "127.0.0.1" || socketIp === "::1" || socketIp === "::ffff:127.0.0.1";
    // Trust forwarding headers only when the TCP peer is a local reverse proxy.
    // Direct/public sockets remain keyed by the unspoofable peer address.
    const proxyIp = xRealIp || (xff ? String(xff).split(",")[0].trim() : "");
    const ip = isLoopbackProxy && proxyIp ? proxyIp : socketIp;
    delete req.headers["x-9r-real-ip"];
    delete req.headers["x-forwarded-for"];
    delete req.headers["x-9r-via-proxy"];
    delete req.headers["x-9r-peer-token"];
    req.headers["x-9r-real-ip"] = ip;
    req.headers["x-9r-peer-token"] = PEER_TOKEN;
    if (viaProxy) req.headers["x-9r-via-proxy"] = "1";
    return handler(req, res);
  };
  const server = origCreate(...rest, wrapped);
  server.once("listening", () => {
    startBackgroundTokenRefreshFromCustomServer();
    startResponsesWsFromCustomServer(server);
  });
  const origEmit = server.emit;
  // JBR 25 sends h2c upgrades that the HTTP/1.1 server would otherwise close.
  // Responses WebSocket upgrades (`Upgrade: websocket`) fall through to origEmit
  // and are handled by attachResponsesWebSocket listeners.
  server.emit = function (event, ...eventArgs) {
    const [req, socket, head] = eventArgs;
    if (event !== "upgrade" || String(req.headers.upgrade || "").toLowerCase() !== "h2c") {
      return origEmit.call(this, event, ...eventArgs);
    }

    const contentLength = Number(req.headers["content-length"] || 0);
    if (!Number.isSafeInteger(contentLength) || contentLength < 0) {
      socket.destroy();
      return true;
    }
    const chunks = [head];
    let received = head.length;
    const serve = () => {
      // Replay the upgraded request through the existing HTTP/1.1 handler.
      const replay = new http.IncomingMessage(socket);
      Object.assign(replay, { method: req.method, url: req.url, headers: req.headers, complete: true });
      if (received) replay.push(Buffer.concat(chunks, received).subarray(0, contentLength));
      replay.push(null);
      const res = new http.ServerResponse(replay);
      res.shouldKeepAlive = false;
      res.assignSocket(socket);
      res.once("finish", () => socket.end());
      Promise.resolve().then(() => wrapped(replay, res)).catch((error) => {
        console.error("Failed to downgrade h2c request", error);
        socket.destroy();
      });
    };
    if (received >= contentLength) serve();
    else {
      socket.on("data", function readBody(chunk) {
        chunks.push(chunk);
        received += chunk.length;
        if (received < contentLength) return;
        socket.off("data", readBody);
        serve();
      });
      socket.resume();
    }
    delete req.headers.upgrade;
    delete req.headers["http2-settings"];
    req.headers.connection = "close";
    return true;
  };
  return server;
};

if (require.main === module) {
  const standalone = path.join(__dirname, "server.js");
  if (fs.existsSync(standalone)) {
    require(standalone);
  } else {
    // Repo checkout has no standalone build next to us. `next start` builds its HTTP
    // server in-process, so the wrapper above still sanitizes every request.
    const nextBin = require.resolve("next/dist/bin/next");
    process.argv = [process.argv[0], nextBin, "start", ...process.argv.slice(2)];
    require(nextBin);
  }
}
