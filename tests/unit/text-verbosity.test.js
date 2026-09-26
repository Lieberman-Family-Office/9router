import { describe, expect, it } from "vitest";
import { applyThinking, parseSuffix } from "../../open-sse/translator/concerns/thinkingUnified.js";
import {
  parseTextConfigToken,
  pickTextConfig,
  mergeTextConfig,
  applyTextConfigToBody,
} from "../../open-sse/translator/concerns/textConfig.js";
import { CodexExecutor } from "../../open-sse/executors/codex.js";

function wire(modelId, body = {}) {
  const base = modelId.replace(/\([^()]*\)\s*$/, "");
  const b = { model: base, input: [], ...body };
  applyThinking("openai-responses", modelId, b, "codex");
  return new CodexExecutor().transformRequest(b.model, b, true, { providerSpecificData: {} });
}

describe("text.verbosity (OpenAI deployment checklist)", () => {
  it("parseTextConfigToken accepts verbosity_* and verb_*", () => {
    expect(parseTextConfigToken("verbosity_low")).toEqual({ verbosity: "low" });
    expect(parseTextConfigToken("verbosity_medium")).toEqual({ verbosity: "medium" });
    expect(parseTextConfigToken("verbosity_high")).toEqual({ verbosity: "high" });
    expect(parseTextConfigToken("verb_low")).toEqual({ verbosity: "low" });
    expect(parseTextConfigToken("low")).toBeNull(); // effort token — not verbosity
  });

  it("parseSuffix compounds effort + verbosity without colliding with effort low", () => {
    expect(parseSuffix("gpt-6-sol(max,verbosity_low)")).toEqual({
      cleanModel: "gpt-6-sol",
      override: { mode: "level", level: "max" },
      reasoningConfig: null,
      reasoningMode: null,
      orchestration: null,
      textConfig: { verbosity: "low" },
    });
  });

  it("wires text.verbosity=low through Codex transformRequest", () => {
    const out = wire("gpt-6-sol(verbosity_low)");
    expect(out.model).toBe("gpt-6-sol");
    expect(out.text).toMatchObject({ verbosity: "low" });
    expect(out.verbosity).toBeUndefined(); // top-level stripped by Codex allowlist
  });

  it("suffix verbosity wins over body text.verbosity", () => {
    const out = wire("gpt-6-astra(verbosity_high)", {
      text: { verbosity: "low", format: { type: "text" } },
    });
    expect(out.text).toMatchObject({ verbosity: "high", format: { type: "text" } });
  });

  it("preserves body text.verbosity when no suffix token", () => {
    const out = wire("gpt-6-sol(high)", { text: { verbosity: "medium" } });
    expect(out.reasoning.effort).toBe("high");
    expect(out.text).toMatchObject({ verbosity: "medium" });
  });

  it("does not invent text.verbosity by default", () => {
    const out = wire("gpt-6-sol(high)");
    expect(out.text?.verbosity).toBeUndefined();
  });

  it("pick/merge/apply helpers", () => {
    expect(pickTextConfig({ text: { verbosity: "HIGH" } })).toEqual({ verbosity: "high" });
    expect(mergeTextConfig({ verbosity: "low" }, { verbosity: "high" })).toEqual({ verbosity: "high" });
    const body = { text: { format: { type: "json_schema" } } };
    applyTextConfigToBody(body, { verbosity: "low" });
    expect(body.text).toEqual({ format: { type: "json_schema" }, verbosity: "low" });
    expect(body.verbosity).toBe("low");
  });
});
