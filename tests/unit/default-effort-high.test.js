import { describe, expect, it } from "vitest";
import { applyThinking, DEFAULT_THINKING_INTENT as D } from "../../open-sse/translator/concerns/thinkingUnified.js";
import { CodexExecutor } from "../../open-sse/executors/codex.js";

const apply = (fmt, provider, model, body = {}) => applyThinking(fmt, model, { ...body }, provider, undefined, D);

describe("no explicit effort → high", () => {
  it.each([
    ["openai", "openai", "gpt-6.2-sol", (o) => expect(o.reasoning_effort).toBe("high")],
    ["claude", "claude", "claude-opus-5-5", (o) => expect(o.output_config.effort).toBe("high")],
    ["gemini", "gemini", "gemini-2.5-pro", (o) => expect(o.generationConfig.thinkingConfig.thinkingBudget).toBe(24576)],
  ])("%s/%s/%s", (fmt, provider, model, check) => check(apply(fmt, provider, model)));

  it.each(["gpt-6.2-astra", "gpt-6.2-sol", "gpt-5.5"])("codex executor %s", (m) => {
    expect(new CodexExecutor().transformRequest(m, { model: m, input: "hi" }, true, {}).reasoning.effort).toBe("high");
  });

  it("explicit effort wins (body and suffix)", () => {
    expect(apply("openai", "openai", "gpt-6.2-sol", { reasoning_effort: "low" }).reasoning_effort).toBe("low");
    expect(apply("claude", "claude", "claude-opus-5-5(medium)").output_config.effort).toBe("medium");
    const m = "gpt-6.2-sol";
    expect(new CodexExecutor().transformRequest(m, { model: m, input: "hi", reasoning: { effort: "low" } }, true, {}).reasoning.effort).toBe("low");
  });

  it("non-reasoning models stay untouched", () => {
    expect(apply("openai", "openai", "gpt-4o-mini")).toEqual({});
  });

  it("forced tool use skips the default (Anthropic 400s thinking + tool_choice any)", () => {
    expect(apply("claude", "claude", "claude-sonnet-4-5", { tool_choice: { type: "any" } })).not.toHaveProperty("thinking");
  });
});
