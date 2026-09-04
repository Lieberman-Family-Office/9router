import { describe, expect, it } from "vitest";
import { comboWindowStats, parseProviderModelId } from "./comboContextLength.js";

describe("parseProviderModelId", () => {
  it("splits alias/model", () => {
    expect(parseProviderModelId("cc/claude-opus-5")).toEqual({
      alias: "cc",
      modelId: "claude-opus-5",
    });
  });
  it("returns null for bare ids", () => {
    expect(parseProviderModelId("subs-coding")).toBeNull();
  });
});

describe("comboWindowStats", () => {
  it("returns max and min across legs", () => {
    const getCaps = (alias, modelId) => {
      const key = `${alias}/${modelId}`;
      return (
        {
          "cx/gpt-5.6-sol": { contextWindow: 372_000, maxOutput: 128_000 },
          "cc/claude-opus-5": { contextWindow: 1_000_000, maxOutput: 128_000 },
        }[key] || { contextWindow: 200_000, maxOutput: 64_000 }
      );
    };
    const s = comboWindowStats(["cx/gpt-5.6-sol", "cc/claude-opus-5"], getCaps);
    expect(s.maxContext).toBe(1_000_000);
    expect(s.minContext).toBe(372_000);
    expect(s.maxOutput).toBe(128_000);
    expect(s.resolved).toBe(2);
    expect(s.unresolved).toEqual([]);
  });

  it("returns null maxContext when model list empty", () => {
    const getCaps = () => ({ contextWindow: 200_000, maxOutput: 64_000 });
    expect(comboWindowStats([], getCaps).maxContext).toBeNull();
  });
});
