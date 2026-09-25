import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// cursorModels.js talks HTTP/2 (agent.api5.cursor.sh is h2-only), not fetch.
// Mock http2.connect so no test reaches the real Cursor endpoint.
const h2 = vi.hoisted(() => ({ reply: { status: 200, body: new Uint8Array() }, requests: [] }));
vi.mock("http2", () => {
  const connect = vi.fn((origin) => {
    const client = new (require("node:events").EventEmitter)();
    client.close = vi.fn();
    client.request = vi.fn((headers) => {
      const req = new (require("node:events").EventEmitter)();
      req.end = (body) => {
        h2.requests.push({ origin, headers, body });
        setImmediate(() => {
          req.emit("response", { ":status": h2.reply.status });
          if (h2.reply.body.length) req.emit("data", Buffer.from(h2.reply.body));
          req.emit("end");
        });
      };
      return req;
    });
    return client;
  });
  return { default: { connect }, connect };
});

import {
  clearCursorModelCache,
  parseCursorUsableModels,
  resolveCursorModels,
} from "../../open-sse/services/cursorModels.js";

function varint(value) {
  const bytes = [];
  while (value >= 0x80) {
    bytes.push((value & 0x7f) | 0x80);
    value >>>= 7;
  }
  bytes.push(value);
  return Uint8Array.from(bytes);
}

function field(fieldNumber, value) {
  return Uint8Array.from([(fieldNumber << 3) | 2, ...varint(value.length), ...value]);
}

function text(value) {
  return new TextEncoder().encode(value);
}

function concat(...parts) {
  const size = parts.reduce((sum, part) => sum + part.length, 0);
  const result = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

function model(id, name) {
  return field(1, concat(field(1, text(id)), field(4, text(name))));
}

describe("Cursor live model catalog", () => {
  beforeEach(() => {
    clearCursorModelCache();
    h2.requests.length = 0;
    h2.reply = { status: 200, body: new Uint8Array() };
  });

  afterEach(() => {
    clearCursorModelCache();
  });

  it("decodes the GetUsableModels protobuf response", () => {
    const payload = concat(
      model("default", "Auto"),
      model("gpt-5.3-codex", "GPT 5.3 Codex"),
      model("gpt-5.3-codex", "Duplicate"),
    );

    expect(parseCursorUsableModels(payload)).toEqual([
      { id: "default", name: "Auto" },
      { id: "gpt-5.3-codex", name: "GPT 5.3 Codex" },
    ]);
  });

  it("fetches the account-specific catalog and caches it", async () => {
    h2.reply = { status: 200, body: concat(model("claude-4.6-opus", "Claude 4.6 Opus")) };
    const credentials = {
      accessToken: "cursor-token",
      providerSpecificData: { machineId: "machine-id" },
    };

    await expect(resolveCursorModels(credentials)).resolves.toEqual({
      models: [{ id: "claude-4.6-opus", name: "Claude 4.6 Opus" }],
    });
    await expect(resolveCursorModels(credentials)).resolves.toEqual({
      models: [{ id: "claude-4.6-opus", name: "Claude 4.6 Opus" }],
    });

    // Second call served from cache: exactly one HTTP/2 request.
    expect(h2.requests).toHaveLength(1);
    const [{ origin, headers }] = h2.requests;
    expect(origin).toBe("https://agent.api5.cursor.sh");
    expect(headers).toEqual(expect.objectContaining({
      ":method": "POST",
      ":path": "/agent.v1.AgentService/GetUsableModels",
      "content-type": "application/proto",
      accept: "application/proto",
    }));
  });

  it("fails open when the Cursor catalog request fails", async () => {
    h2.reply = { status: 403, body: new TextEncoder().encode("no") };

    await expect(resolveCursorModels({
      accessToken: "cursor-token",
      providerSpecificData: { machineId: "machine-id" },
    })).resolves.toBeNull();
  });
});
