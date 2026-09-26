import { describe, expect, it } from "vitest";
import { getCapabilitiesForModel } from "../../open-sse/providers/capabilities.js";

describe("gpt-6 Codex vision capabilities", () => {
  it.each(["gpt-6-astra", "gpt-6-sol", "cx/gpt-6-astra", "cx/gpt-6-sol"])(
    "%s reports vision under codex (provider override must not strip images)",
    (model) => {
      const caps = getCapabilitiesForModel("codex", model.includes("/") ? model.split("/").pop() : model);
      expect(caps.vision).toBe(true);
      expect(caps.reasoning).toBe(true);
      expect(caps.pdf).toBe(true);
      expect(caps.contextWindow).toBeGreaterThanOrEqual(372000);
    },
  );

  it("gpt-6 pattern covers non-codex providers without a provider override", () => {
    const caps = getCapabilitiesForModel("openai", "gpt-6-astra");
    expect(caps.vision).toBe(true);
    expect(caps.reasoning).toBe(true);
  });
});
