import { describe, expect, it } from "vitest";
import { getExecutor } from "../../open-sse/executors/index.js";

const send = (body, provider = "openai") =>
  getExecutor(provider).transformRequest(body.model, structuredClone(body), true, {});

describe("openai provider: max_tokens → max_completion_tokens", () => {
  it.each(["gpt-6.2-sol", "gpt-6-astra", "gpt-5.5", "gpt-4o-mini"])("%s", (model) => {
    const out = send({ model, max_tokens: 16, messages: [] });
    expect(out.max_completion_tokens).toBe(16);
    expect(out).not.toHaveProperty("max_tokens");
  });

  it("an explicit max_completion_tokens wins over max_tokens", () => {
    const out = send({ model: "gpt-6.2-sol", max_tokens: 16, max_completion_tokens: 64, messages: [] });
    expect(out.max_completion_tokens).toBe(64);
    expect(out).not.toHaveProperty("max_tokens");
  });

  it("other OpenAI-compatible providers keep max_tokens", () => {
    const out = send({ model: "llama-3.3-70b", max_tokens: 16, messages: [] }, "groq");
    expect(out.max_tokens).toBe(16);
    expect(out).not.toHaveProperty("max_completion_tokens");
  });
});
