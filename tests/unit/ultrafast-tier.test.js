import { afterEach, describe, it, expect, vi } from "vitest";
import { BaseExecutor } from "../../open-sse/executors/base.js";
import { CodexExecutor } from "../../open-sse/executors/codex.js";
import { DefaultExecutor } from "../../open-sse/executors/default.js";
import { createSSEStream } from "../../open-sse/utils/stream.js";
import { buildRequestDetail } from "../../open-sse/handlers/chatCore/requestDetail.js";
import { __test__ } from "../../src/lib/db/repos/requestDetailsRepo.js";
import { FORMATS } from "../../open-sse/translator/formats.js";
import { initTranslators } from "../../open-sse/translator/index.js";

const credentials = { providerSpecificData: { prefix: "openai-ultrafast", apiType: "responses", baseUrl: "https://api.openai.com/v1" } };
const executor = new DefaultExecutor("openai-compatible-responses-test");
afterEach(() => vi.restoreAllMocks());

describe("dedicated Ultrafast route", () => {
  it("forces the tier only on the dedicated model and official Responses endpoint", () => {
    for (const tier of [undefined, "default", "priority"]) {
      expect(executor.transformRequest("gpt-6-astra", { service_tier: tier }, true, credentials).service_tier).toBe("ultrafast");
    }
    for (const override of [{ prefix: "other" }, { apiType: "chat" }, { baseUrl: "https://example.com/v1" }]) {
      const c = { providerSpecificData: { ...credentials.providerSpecificData, ...override } };
      expect(executor.transformRequest("gpt-6-astra", {}, true, c).service_tier).toBeUndefined();
    }
    expect(executor.transformRequest("gpt-other", {}, true, credentials).service_tier).toBeUndefined();
    expect(new DefaultExecutor("openai").transformRequest("gpt-6-astra", {}, true, credentials).service_tier).toBeUndefined();
  });

  it.each(["ultrafast", "default", null])("records the actual upstream tier %s through Cursor streaming translation", async (tier) => {
    await initTranslators();
    const done = vi.fn();
    const transform = createSSEStream({ targetFormat: FORMATS.OPENAI_RESPONSES, sourceFormat: FORMATS.OPENAI, onStreamComplete: done });
    const response = { id: "resp_test", model: "gpt-6-astra", status: "completed", output: [], usage: { input_tokens: 1, output_tokens: 1 }, ...(tier ? { service_tier: tier } : {}) };
    const created = { type: "response.created", response: { id: "resp_test", service_tier: "ultrafast" } };
    const bytes = new TextEncoder().encode(`event: response.created\ndata: ${JSON.stringify(created)}\n\nevent: response.completed\ndata: ${JSON.stringify({ type: "response.completed", response })}\n\n`);
    const source = new ReadableStream({ start(c) { c.enqueue(bytes.slice(0, 17)); c.enqueue(bytes.slice(17)); c.close(); } });
    await new Response(source.pipeThrough(transform)).text();
    expect(done).toHaveBeenCalledTimes(1);
    const detail = buildRequestDetail({ providerRequest: { service_tier: "ultrafast" }, response: done.mock.calls[0][0] });
    expect(__test__.toMetadataRecord(detail).serviceTier).toEqual({ requested: "ultrafast", returned: tier });
  });

  it("does not attest an early tier when the Responses stream never completes", async () => {
    await initTranslators();
    const done = vi.fn();
    const transform = createSSEStream({ targetFormat: FORMATS.OPENAI_RESPONSES, sourceFormat: FORMATS.OPENAI, onStreamComplete: done });
    const created = { type: "response.created", response: { id: "resp_test", service_tier: "ultrafast" } };
    const source = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(`event: response.created\ndata: ${JSON.stringify(created)}\n\n`)); c.close(); } });
    await new Response(source.pipeThrough(transform)).text();
    expect(done).toHaveBeenCalledTimes(1);
    expect(done.mock.calls[0][0].service_tier).toBeNull();
  });

  it.each(["ultrafast", "priority", null, "incomplete"])("requires the terminal subscription tier %s at the Codex transport boundary", async tier => {
    const events = [
      { type: "response.created", response: { id: "resp_test", service_tier: "ultrafast" } },
      { type: "response.output_text.delta", delta: "OK" },
      { type: "response.completed", response: { id: "resp_test", model: "gpt-6-astra", status: "completed", ...(tier ? { service_tier: tier } : {}), output: [], usage: { input_tokens: 1, output_tokens: 1 } } },
    ];
    const selectedEvents = tier === "incomplete" ? events.slice(0, -1) : events;
    const bytes = new TextEncoder().encode(selectedEvents.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""));
    const source = new ReadableStream({ start(controller) {
      controller.enqueue(bytes.slice(0, 17)); controller.enqueue(bytes.slice(17)); controller.close();
    } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source, { headers: { "Content-Type": "text/event-stream" } }), transformedBody: { service_tier: "ultrafast" } });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    if (tier === "ultrafast") expect(await result.response.text()).toContain('"type":"response.completed"');
    else await expect(result.response.text()).rejects.toThrow(/Ultrafast/);
  });

  it.each(["response.done", "response.incomplete", "[DONE]"])("refuses unverified Ultrafast terminal %s before a client can accept it", async terminal => {
    const prefix = 'data: {"type":"response.output_text.delta","delta":"OK"}\n\n';
    const terminalText = terminal === "[DONE]" ? "data: [DONE]\n\n" : `data: ${JSON.stringify({ type: terminal, response: { id: "resp_test", status: terminal === "response.incomplete" ? "incomplete" : "completed", service_tier: "priority" } })}\n\n`;
    let sent = false;
    const source = new ReadableStream({ pull(controller) {
      if (!sent) { sent = true; controller.enqueue(new TextEncoder().encode(prefix + terminalText)); }
    } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source), transformedBody: { service_tier: "ultrafast" } });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    const reader = result.response.body.getReader();
    try {
      const first = new TextDecoder().decode((await reader.read()).value);
      const dataLines = first.split("\n").filter(line => line.startsWith("data:"));
      expect(dataLines).toHaveLength(1);
      expect(JSON.parse(dataLines[0].slice(5))).toEqual({ type: "response.output_text.delta", delta: "OK" });
      await expect(reader.read()).rejects.toThrow(/Ultrafast/);
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  });

  it("buffers an incomplete terminal line until its Ultrafast tier is known", async () => {
    const chunks = [
      'data: {"type":"response.output_text.delta","delta":"OK"}\n\n',
      'data: {"type":"response.completed","response":{"service_tier":"pri',
      'ority","status":"completed"}}\n\n',
    ];
    let index = 0;
    const source = new ReadableStream({ pull(controller) {
      if (index < chunks.length) controller.enqueue(new TextEncoder().encode(chunks[index++]));
      else controller.close();
    } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source), transformedBody: { service_tier: "ultrafast" } });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    const reader = result.response.body.getReader();
    try {
      expect(new TextDecoder().decode((await reader.read()).value)).toContain("response.output_text.delta");
      await expect(reader.read()).rejects.toThrow(/Ultrafast/);
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  });

  it("refuses conflicting repeated SSE event names before forwarding them", async () => {
    const frame = 'event: response.output_text.delta\nevent: response.completed\ndata: {"type":"response.output_text.delta","delta":"OK"}\n\n';
    const source = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(frame)); } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source) });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    const reader = result.response.body.getReader();
    try { await expect(reader.read()).rejects.toThrow(/Ultrafast/); }
    finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  });

  it("canonicalizes multiline terminal JSON for downstream line parsers", async () => {
    const frame = 'event: response.completed\ndata: {"type":"response.completed",\ndata: "response":{"id":"resp_test","status":"completed","service_tier":"ultrafast"}}\n\n';
    const source = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(frame)); controller.close(); } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source) });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    const text = await result.response.text();
    const dataLines = text.split("\n").filter(line => line.startsWith("data:"));
    expect(dataLines).toHaveLength(1);
    expect(JSON.parse(dataLines[0].slice(5)).response.service_tier).toBe("ultrafast");
  });

  it("does not forward duplicate successful terminal events", async () => {
    const terminal = 'data: {"type":"response.completed","response":{"status":"completed","service_tier":"ultrafast"}}\n\n';
    const source = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(terminal + terminal)); controller.close(); } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source) });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    const reader = result.response.body.getReader();
    try {
      expect(new TextDecoder().decode((await reader.read()).value)).toContain("response.completed");
      await expect(reader.read()).rejects.toThrow(/Ultrafast/);
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  });

  it("cancels upstream while a terminal frame is still buffered", async () => {
    const cancel = vi.fn();
    const parts = ['data: {"type":"response.output_text.delta","delta":"OK"}\n\n', 'data: {"type":"response.completed","response":'];
    let index = 0;
    const source = new ReadableStream({ pull(controller) { if (index < parts.length) controller.enqueue(new TextEncoder().encode(parts[index++])); }, cancel });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source) });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    const reader = result.response.body.getReader();
    expect(new TextDecoder().decode((await reader.read()).value)).toContain("output_text.delta");
    await reader.cancel("client closed"); reader.releaseLock();
    await vi.waitFor(() => expect(cancel).toHaveBeenCalledOnce());
  });

  it.each([
    { type: "response.failed", response: { status: "failed" } },
    { type: "error", error: { message: "upstream failed" } },
    { type: "other", response: { status: "failed" } },
    { type: "response.completed", response: { status: "failed", service_tier: "ultrafast" } },
    { type: "response.done", response: { status: "failed", service_tier: "ultrafast" } },
  ])("refuses failed or contradictory Ultrafast terminal $type/$response.status before forwarding", async terminal => {
    const source = new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode(`data: {"type":"response.output_text.delta","delta":"OK"}\n\ndata: ${JSON.stringify(terminal)}\n\n`));
    } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source) });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    const reader = result.response.body.getReader();
    try {
      expect(new TextDecoder().decode((await reader.read()).value)).toContain("output_text.delta");
      await expect(reader.read()).rejects.toThrow(/Ultrafast/);
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  });

  it.each(['data: {"type":"error","error":{"message":"late failure"}}\n\n', 'data: [DONE]\n\ndata: [DONE]\n\n'])("refuses extra Ultrafast terminals after confirmed completion (%s)", async tail => {
    const completed = 'data: {"type":"response.completed","response":{"status":"completed","service_tier":"ultrafast"}}\n\n';
    const source = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(completed + tail)); controller.close(); } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source) });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    await expect(result.response.text()).rejects.toThrow(/Ultrafast/);
  });

  it("preserves comment heartbeats on the verified Ultrafast stream", async () => {
    const prefix = 'data: {"type":"response.output_text.delta","delta":"OK"}\n\n';
    const source = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(prefix + ': keepalive\n\n')); } });
    vi.spyOn(BaseExecutor.prototype, "execute").mockResolvedValue({ response: new Response(source) });
    const result = await new CodexExecutor().execute({ model: "gpt-6-astra-ultrafast", body: { input: "OK" }, credentials: {} });
    const reader = result.response.body.getReader();
    try {
      expect(new TextDecoder().decode((await reader.read()).value)).toContain("output_text.delta");
      const next = reader.read();
      const observed = await Promise.race([next, new Promise(resolve => setTimeout(() => resolve(null), 50))]);
      expect(observed).not.toBeNull();
      expect(new TextDecoder().decode(observed.value)).toBe(': keepalive\n\n');
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  });

  it("waits for asynchronous cancellation of the Ultrafast peek replacement", async () => {
    let release;
    const cleanup = new Promise(resolve => { release = resolve; });
    const cancel = vi.fn(() => cleanup);
    const source = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"response.output_text.delta","delta":"OK"}\n\n')); }, cancel });
    const peek = await new CodexExecutor()._peekSseTransientError(new Response(source));
    const reader = peek.replacementBody.getReader();
    await reader.read();
    let settled = false;
    const pending = reader.cancel("client closed").then(() => { settled = true; });
    try {
      await vi.waitFor(() => expect(cancel).toHaveBeenCalledOnce());
      await Promise.resolve();
      expect(settled).toBe(false);
    } finally { release(); await pending; reader.releaseLock(); }
  });

  it("propagates rejected cancellation of the Ultrafast peek replacement", async () => {
    const source = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"response.output_text.delta","delta":"OK"}\n\n')); }, cancel: () => Promise.reject(new Error("Ultrafast cleanup refused")) });
    const peek = await new CodexExecutor()._peekSseTransientError(new Response(source));
    const reader = peek.replacementBody.getReader();
    await reader.read();
    try { await expect(reader.cancel("client closed")).rejects.toThrow("Ultrafast cleanup refused"); }
    finally { reader.releaseLock(); }
  });

  it("keeps unknown tiers unknown and drops arbitrary text", () => {
    const record = __test__.toMetadataRecord({ serviceTier: { requested: "secret prompt", returned: "secret credential" } });
    expect(record.serviceTier).toEqual({ requested: null, returned: null });
    expect(JSON.stringify(record)).not.toContain("secret");
    expect(buildRequestDetail({ providerRequest: { service_tier: "ultrafast" }, providerResponse: { service_tier: "default" } }).serviceTier).toEqual({ requested: "ultrafast", returned: "default" });
  });
});
