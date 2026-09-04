import { describe, expect, it } from "vitest";
import { estimateTokensFromJsonString, shouldRefuse } from "./inputTokenGate.js";

describe("inputTokenGate", () => {
  it("estimates len/4", () => {
    expect(estimateTokensFromJsonString("abcd")).toBe(1);
  });

  it("disabled at 0", () => {
    expect(shouldRefuse(999999, 0)).toBe(false);
  });
});
