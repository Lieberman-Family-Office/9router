/**
 * Attach Responses WebSocket upgrade handling to an HTTP server.
 * Additive: does not remove existing upgrade listeners (preserves h2c / tests).
 */

import { completeWsHandshake } from "./wsFrames.js";
import { createResponsesWsSession } from "./session.js";

/**
 * @param {import("node:http").Server} server
 * @param {object} [opts]
 * @param {number} [opts.localPort]
 * @param {(model: string) => { provider: string }} [opts.resolveRoute]
 */
export function attachResponsesWebSocket(server, opts = {}) {
  const localPort = opts.localPort || Number(process.env.PORT) || 20128;

  const fetchLocalResponses = async (path, headers, body) => {
    const url = `http://127.0.0.1:${localPort}${path}`;
    return fetch(url, {
      method: "POST",
      headers: {
        ...headers,
        host: `127.0.0.1:${localPort}`,
      },
      body: JSON.stringify(body),
    });
  };

  const onUpgrade = (req, socket, head) => {
    const url = req.url || "";
    const pathOnly = url.split("?")[0];
    const upgrade = String(req.headers.upgrade || "").toLowerCase();
    if (upgrade !== "websocket") return false;
    if (pathOnly !== "/v1/responses" && pathOnly !== "/responses") return false;
    if (!completeWsHandshake(req, socket, head)) return true;
    createResponsesWsSession({
      socket,
      req,
      fetchLocalResponses,
      resolveRoute: opts.resolveRoute || defaultResolveRoute,
    });
    return true;
  };

  // Prepend without removing other listeners (h2c emit path + tests keep working).
  server.on("upgrade", (req, socket, head) => {
    onUpgrade(req, socket, head);
  });

  return { onUpgrade };
}

function defaultResolveRoute(model) {
  const m = String(model || "");
  if (/^(cx|codex)\//i.test(m) || /codex/i.test(m)) return { provider: "codex" };
  if (/^openai\//i.test(m)) return { provider: "openai" };
  if (/gpt-6/i.test(m)) return { provider: "codex" };
  return { provider: "openai" };
}

export function installOnServer(server, opts) {
  return attachResponsesWebSocket(server, opts);
}

export default { attachResponsesWebSocket, installOnServer };
