import { describe, expect, it } from "vitest";
import {
  comboWindowStats,
  enrichComboModelEntry,
  parseProviderModelId,
} from "./comboContextLength.js";

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

  it("pins null stats and zero resolved when model list empty", () => {
    const getCaps = () => ({ contextWindow: 200_000, maxOutput: 64_000 });
    expect(comboWindowStats([], getCaps)).toEqual({
      maxContext: null,
      minContext: null,
      maxOutput: null,
      resolved: 0,
      unresolved: [],
    });
  });

  it("lands unparseable model ids in unresolved", () => {
    const getCaps = () => ({ contextWindow: 200_000, maxOutput: 64_000 });
    const s = comboWindowStats(["subs-coding", ""], getCaps);
    expect(s.unresolved).toEqual(["subs-coding", ""]);
    expect(s.resolved).toBe(0);
    expect(s.maxContext).toBeNull();
    expect(s.minContext).toBeNull();
    expect(s.maxOutput).toBeNull();
  });

  it("lands non-finite or non-positive contextWindow in unresolved without affecting max/min", () => {
    const getCaps = (alias, modelId) => {
      const key = `${alias}/${modelId}`;
      return (
        {
          "cc/good": { contextWindow: 100_000, maxOutput: 8_000 },
          "cc/nan": { contextWindow: NaN, maxOutput: 8_000 },
          "cc/zero": { contextWindow: 0, maxOutput: 8_000 },
          "cc/neg": { contextWindow: -1, maxOutput: 8_000 },
          "cc/missing": {},
        }[key] || { contextWindow: 50_000, maxOutput: 4_000 }
      );
    };
    const s = comboWindowStats(
      ["cc/good", "cc/nan", "cc/zero", "cc/neg", "cc/missing"],
      getCaps,
    );
    expect(s.unresolved).toEqual([
      "cc/nan",
      "cc/zero",
      "cc/neg",
      "cc/missing",
    ]);
    expect(s.resolved).toBe(1);
    expect(s.maxContext).toBe(100_000);
    expect(s.minContext).toBe(100_000);
    expect(s.maxOutput).toBe(8_000);
  });

  it("resolves context when maxOutput missing; maxOutput stays null if none provide it", () => {
    const getCaps = (alias, modelId) => {
      const key = `${alias}/${modelId}`;
      return (
        {
          "cc/no-out": { contextWindow: 200_000 },
          "cc/also-no-out": { contextWindow: 300_000 },
        }[key] || {}
      );
    };
    const s = comboWindowStats(["cc/no-out", "cc/also-no-out"], getCaps);
    expect(s.unresolved).toEqual([]);
    expect(s.resolved).toBe(2);
    expect(s.maxContext).toBe(300_000);
    expect(s.minContext).toBe(200_000);
    expect(s.maxOutput).toBeNull();
  });

  it("resolves context from legs missing maxOutput while still taking maxOutput from other legs", () => {
    const getCaps = (alias, modelId) => {
      const key = `${alias}/${modelId}`;
      return (
        {
          "cc/no-out": { contextWindow: 200_000 },
          "cc/with-out": { contextWindow: 150_000, maxOutput: 32_000 },
        }[key] || {}
      );
    };
    const s = comboWindowStats(["cc/no-out", "cc/with-out"], getCaps);
    expect(s.unresolved).toEqual([]);
    expect(s.resolved).toBe(2);
    expect(s.maxContext).toBe(200_000);
    expect(s.minContext).toBe(150_000);
    expect(s.maxOutput).toBe(32_000);
  });
});

describe("enrichComboModelEntry", () => {
  it("sets context_length to max leg window", () => {
    const entry = enrichComboModelEntry(
      { name: "subs-coding", models: ["cx/gpt-5.6-sol", "cc/claude-opus-5"] },
      (alias, modelId) =>
        ({
          "cx/gpt-5.6-sol": { contextWindow: 372_000, maxOutput: 128_000 },
          "cc/claude-opus-5": { contextWindow: 1_000_000, maxOutput: 128_000 },
        })[`${alias}/${modelId}`],
    );
    expect(entry.id).toBe("subs-coding");
    expect(entry.owned_by).toBe("combo");
    expect(entry.context_length).toBe(1_000_000);
    expect(entry.max_completion_tokens).toBe(128_000);
    expect(entry.capabilities.contextWindow).toBe(1_000_000);
    expect(entry.capabilities.contextWindowMin).toBe(372_000);
  });
});
