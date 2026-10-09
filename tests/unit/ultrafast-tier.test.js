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
      expect(first).toBe(prefix);
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

  it("keeps unknown tiers unknown and drops arbitrary text", () => {
    const record = __test__.toMetadataRecord({ serviceTier: { requested: "secret prompt", returned: "secret credential" } });
    expect(record.serviceTier).toEqual({ requested: null, returned: null });
    expect(JSON.stringify(record)).not.toContain("secret");
    expect(buildRequestDetail({ providerRequest: { service_tier: "ultrafast" }, providerResponse: { service_tier: "default" } }).serviceTier).toEqual({ requested: "ultrafast", returned: "default" });
  });
});
