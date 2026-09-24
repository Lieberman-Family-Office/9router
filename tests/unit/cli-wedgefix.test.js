import fs from "node:fs";
import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const { resolveServerSpawnOptions, usesParentStderrPipe } = require("../../cli/src/cli/utils/serverSpawnOptions.js");
const { pidFromAppProcessLine } = require("../../cli/src/cli/utils/killAppProcessMatch.js");
const cliSource = fs.readFileSync(new URL("../../cli/cli.js", import.meta.url), "utf8");

describe("CLI wedgefix: server stderr is never piped to the parent", () => {
  it.each([
    [{ attached: false }],
    [{ attached: true }],
    [{ attached: false, openCrashLog: () => 7 }],
    [{ attached: false, openCrashLog: () => { throw new Error("no fs"); } }],
    [{ showLog: true, attached: false }],
  ])("resolveServerSpawnOptions(%j) has no stderr pipe", (opts) => {
    const { stdio } = resolveServerSpawnOptions(opts);
    expect(usesParentStderrPipe(stdio)).toBe(false);
    expect(Array.isArray(stdio) ? stdio[2] : stdio).not.toBe("pipe");
  });

  it("detached mode routes stderr to the crash-log fd", () => {
    expect(resolveServerSpawnOptions({ attached: false, openCrashLog: () => 7 }))
      .toEqual({ stdio: ["ignore", "ignore", 7], detached: true });
  });

  it("cli.js spawns next-server with the resolved options, not a pipe", () => {
    expect(cliSource).toContain("stdio: spawnOpts.stdio");
    expect(cliSource).not.toMatch(/\["ignore",\s*"ignore",\s*"pipe"\]/);
    expect(cliSource).not.toMatch(/child\.stderr\.on\(/);
  });
});

describe("CLI wedgefix: startup kill is scoped to this port", () => {
  const line = (pid, cmd) => `user ${pid} 0.0 0.1 1 1 ?? S 1:00 0:00 ${cmd}`;

  it("matches this port's 9router CLI", () => {
    expect(pidFromAppProcessLine(line(111, "node /opt/homebrew/bin/9router/cli.js --port 20128"), 20128, "1")).toBe("111");
  });

  it.each([
    ["other port", "node /opt/homebrew/bin/9router/cli.js --port 21129"],
    ["bare next-server", "next-server (v16.0.0)"],
    ["unrelated node", "node /srv/other-app/server.js --port 20128"],
    ["editor mentioning 9router", "vim /x/9router/cli.js --port 20128"],
  ])("does not match %s", (_label, cmd) => {
    expect(pidFromAppProcessLine(line(222, cmd), 20128, "1")).toBeNull();
  });

  it("never matches its own pid", () => {
    expect(pidFromAppProcessLine(line(333, "node /x/9router/cli.js --port 20128"), 20128, "333")).toBeNull();
  });

  it("cli.js no longer kills bare next-server from ps", () => {
    expect(cliSource).not.toMatch(/\|\|\s*cmd\.includes\("next-server"\)/);
    expect(cliSource).toContain("pidFromAppProcessLine(line, appPort, selfPid)");
  });
});
