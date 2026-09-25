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
  it("writes an error row despite ENABLE_REQUEST_LOGS=false, with status and no error text or bodies", async () => {
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
    expect(got.response).toEqual({ status: 401 });
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

  it("keeps only type/code from a JSON upstream error, never its message (the S18 shape)", async () => {
    // Echo is partial and re-cased, which no text filter over the message would catch.
    const echo = `invalid key Bearer ${SECRET} for request '${PROMPT.slice(3).toLowerCase()}'`;
    await save({
      id: "meta-echo", provider: "openai", model: "gpt-x", status: "error",
      request: { model: "gpt-x", stream: false, messages: [{ role: "user", content: PROMPT }] },
      response: { status: 401, error: JSON.stringify({ error: { type: "authentication_error", code: "invalid_api_key", message: echo } }) },
    });
    const got = await db.getRequestDetailById("meta-echo");
    expect(got.response).toEqual({ status: 401, errorType: "authentication_error", errorCode: "invalid_api_key" });
    const raw = adapter.get(`SELECT data FROM requestDetails WHERE id = ?`, ["meta-echo"]).data;
    expect(raw.toLowerCase()).not.toContain(PROMPT.slice(3).toLowerCase());
    expect(raw).not.toContain(SECRET);
  });

  it("writes one row per client request across account fallback, with the attempt count", async () => {
    const { runRequestScope } = await import("@/lib/requestScope.js");
    const before = adapter.get(`SELECT COUNT(*) AS c FROM requestDetails`).c;
    // One request: a 401 on accounts A and B, then success on C.
    await runRequestScope(async () => {
      for (const conn of ["acct-a", "acct-b"]) {
        await save({ provider: "openai", model: "gpt-x", connectionId: conn, status: "error",
          request: { model: "gpt-x", stream: false }, response: { status: 401, error: "bad key" } });
      }
      await save({ provider: "openai", model: "gpt-x", connectionId: "acct-c", status: "success",
        request: { model: "gpt-x", stream: false }, response: { finish_reason: "stop" } });
    });
    // A second, separate request that fails once.
    await runRequestScope(() => save({ provider: "openai", model: "gpt-x", connectionId: "acct-a", status: "error",
      request: { model: "gpt-x", stream: false }, response: { status: 500, error: "boom" } }));

    const rows = adapter.all(`SELECT data FROM requestDetails ORDER BY timestamp ASC`).slice(before).map((r) => JSON.parse(r.data));
    expect(rows).toHaveLength(2);
    const first = rows.find((r) => r.status === "success");
    expect(first).toMatchObject({ connectionId: "acct-c", attempts: 3, attemptStatuses: [401, 401] });
    const second = rows.find((r) => r.status === "error");
    expect(second).toMatchObject({ attempts: 1, attemptStatuses: [500], response: { status: 500 } });
  });

  it("errorIdentifiers keeps only identifier-shaped type/code and drops everything else", async () => {
    const { __test__ } = await import("@/lib/db/repos/requestDetailsRepo.js");
    const ids = __test__.errorIdentifiers;
    expect(ids(`plain text mentioning ${PROMPT}`)).toEqual({});
    expect(ids({ type: "rate_limit_error", code: 429 })).toEqual({ type: "rate_limit_error", code: 429 });
    expect(ids(JSON.stringify({ type: "error", error: { type: "invalid_request_error" } }))).toEqual({ type: "invalid_request_error" });
    // A free-text "type"/"code" (spaces, quotes, or too long) is not an identifier and is dropped.
    expect(ids({ error: { type: `bad ${PROMPT}`, code: "x".repeat(65) } })).toEqual({});
    expect(ids({ error: { type: true, code: 1.5 } })).toEqual({});
  });
});
