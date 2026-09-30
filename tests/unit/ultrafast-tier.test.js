import { describe, it, expect, vi } from "vitest";
import { DefaultExecutor } from "../../open-sse/executors/default.js";
import { createSSEStream } from "../../open-sse/utils/stream.js";
import { buildRequestDetail } from "../../open-sse/handlers/chatCore/requestDetail.js";
import { __test__ } from "../../src/lib/db/repos/requestDetailsRepo.js";
import { FORMATS } from "../../open-sse/translator/formats.js";
import { initTranslators } from "../../open-sse/translator/index.js";

const credentials = { providerSpecificData: { prefix: "openai-ultrafast", apiType: "responses", baseUrl: "https://api.openai.com/v1" } };
const executor = new DefaultExecutor("openai-compatible-responses-test");

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

  it("keeps unknown tiers unknown and drops arbitrary text", () => {
    const record = __test__.toMetadataRecord({ serviceTier: { requested: "secret prompt", returned: "secret credential" } });
    expect(record.serviceTier).toEqual({ requested: null, returned: null });
    expect(JSON.stringify(record)).not.toContain("secret");
    expect(buildRequestDetail({ providerRequest: { service_tier: "ultrafast" }, providerResponse: { service_tier: "default" } }).serviceTier).toEqual({ requested: "ultrafast", returned: "default" });
  });
});
