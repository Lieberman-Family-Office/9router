// REQUEST_DETAILS_MODE=metadata: requestDetails rows are written even with
// ENABLE_REQUEST_LOGS=false, carry status/latency/tokens/error metadata, and
// never store prompt/response bodies or credentials.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";

const saved = { DATA_DIR: process.env.DATA_DIR, ENABLE_REQUEST_LOGS: process.env.ENABLE_REQUEST_LOGS, REQUEST_DETAILS_MODE: process.env.REQUEST_DETAILS_MODE };
let tempDir;
let db;
let adapter;

const PROMPT = "PLANTED-PROMPT-TEXT";
const SECRET = "sk-planted-0123456789";

beforeAll(async () => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "9router-details-meta-"));
  process.env.DATA_DIR = tempDir;
  process.env.ENABLE_REQUEST_LOGS = "false";
  process.env.REQUEST_DETAILS_MODE = "metadata";
  vi.resetModules();
  db = await import("@/lib/db/index.js");
  await db.initDb();
  await db.updateSettings({ observabilityBatchSize: 1 });
  const { getAdapter } = await import("@/lib/db/driver.js");
  adapter = await getAdapter();
});

afterAll(() => {
  if (tempDir) fs.rmSync(tempDir, { recursive: true, force: true });
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

async function save(detail) {
  await db.saveRequestDetail(detail);
  await new Promise((r) => setTimeout(r, 150));
}

describe("requestDetails metadata-only mode", () => {
  it("writes an error row despite ENABLE_REQUEST_LOGS=false, with status + redacted error text and no bodies", async () => {
    await save({
      id: "meta-err", provider: "openai", model: "gpt-x", status: "error",
      latency: { ttft: 0, total: 12 }, tokens: { prompt_tokens: 0, completion_tokens: 0 },
      request: { model: "gpt-x", stream: false, messages: [{ role: "user", content: PROMPT }], headers: { authorization: `Bearer ${SECRET}` } },
      providerRequest: { messages: [{ role: "user", content: PROMPT }] },
      providerResponse: { echoed: PROMPT },
      response: { status: 401, error: `invalid key Bearer ${SECRET} for ${SECRET}`, thinking: null },
    });
    const got = await db.getRequestDetailById("meta-err");
    expect(got.metadataOnly).toBe(true);
    expect(got.status).toBe("error");
    expect(got.response.status).toBe(401);
    expect(got.response.error).toContain("[REDACTED]");
    expect(got.request).toEqual({ model: "gpt-x", stream: false });
    expect(got.providerRequest).toBeUndefined();
    expect(got.providerResponse).toBeUndefined();

    const raw = adapter.get(`SELECT data FROM requestDetails WHERE id = ?`, ["meta-err"]).data;
    expect(raw).not.toContain(SECRET);
    expect(raw).not.toContain(PROMPT);
  });

  it("writes a success row without response content", async () => {
    await save({
      id: "meta-ok", provider: "openai", model: "gpt-x", status: "success",
      latency: { ttft: 5, total: 9 }, tokens: { prompt_tokens: 3, completion_tokens: 4 },
      request: { model: "gpt-x", stream: true, messages: [{ role: "user", content: PROMPT }] },
      response: { content: `answer ${PROMPT}`, thinking: "t", finish_reason: "stop", type: "streaming" },
    });
    const got = await db.getRequestDetailById("meta-ok");
    expect(got.tokens).toEqual({ prompt_tokens: 3, completion_tokens: 4 });
    expect(got.response).toEqual({ finish_reason: "stop", type: "streaming" });
    const raw = adapter.get(`SELECT data FROM requestDetails WHERE id = ?`, ["meta-ok"]).data;
    expect(raw).not.toContain(PROMPT);
  });

  it("redactSecrets masks common credential shapes", async () => {
    const { __test__ } = await import("@/lib/db/repos/requestDetailsRepo.js");
    const out = __test__.redactSecrets("Bearer abc.def sk-abcdEFGH1234 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig AIzaSyA1234567890abcdefghij ghp_abcdefghijklmnopqrstuvwx");
    expect(out).toBe("Bearer [REDACTED] [REDACTED] [REDACTED] [REDACTED] [REDACTED]");
  });
});
