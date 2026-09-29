import { describe, expect, it } from "vitest";
import { homedir } from "os";
import { resolve } from "path";
import { DATA_DIR } from "@/lib/dataDir.js";

// Guards tests/vitest.config.js: tests must never open the live ~/.9router DB.
describe("test data dir isolation", () => {
  it("resolves DATA_DIR away from the live ~/.9router", () => {
    expect(resolve(DATA_DIR)).not.toBe(resolve(homedir(), ".9router"));
  });
});
