import fs from "node:fs";
import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const { isNoTrayEnv, resolveTrayLaunch } = require("../../cli/src/cli/utils/trayFallback.js");
const cliSource = fs.readFileSync(new URL("../../cli/cli.js", import.meta.url), "utf8");

describe("NINEROUTER_NO_TRAY", () => {
  it.each(["1", "true", "yes", " TRUE "])("treats %j as set", (value) => {
    expect(isNoTrayEnv({ NINEROUTER_NO_TRAY: value })).toBe(true);
  });

  it.each([undefined, "", "0", "false", "no"])("treats %j as unset", (value) => {
    expect(isNoTrayEnv({ NINEROUTER_NO_TRAY: value })).toBe(false);
  });

  it("launchd --skip-update with no TTY stays headless and does not start the tray", () => {
    expect(resolveTrayLaunch({
      skipUpdate: true,
      trayRequested: false,
      isTTY: false,
      env: { NINEROUTER_NO_TRAY: "1" },
    })).toEqual({ trayMode: false, headless: true, initTray: false });
  });

  it("explicit --tray still loses to NINEROUTER_NO_TRAY", () => {
    expect(resolveTrayLaunch({
      skipUpdate: true,
      trayRequested: true,
      isTTY: false,
      env: { NINEROUTER_NO_TRAY: "1" },
    }).initTray).toBe(false);
  });

  it("keeps the no-TTY tray fallback when the env is unset", () => {
    expect(resolveTrayLaunch({
      skipUpdate: true,
      trayRequested: false,
      isTTY: false,
      env: {},
    })).toEqual({ trayMode: true, headless: false, initTray: true });
  });

  it("does not force tray on a TTY", () => {
    expect(resolveTrayLaunch({
      skipUpdate: true,
      trayRequested: false,
      isTTY: true,
      env: {},
    })).toEqual({ trayMode: false, headless: false, initTray: true });
  });

  it("cli.js uses the headless path instead of initTray when NO_TRAY is set", () => {
    const headless = cliSource.indexOf("if (launch.headless)");
    const trayIcon = cliSource.indexOf("initTrayIcon();");
    expect(headless).toBeGreaterThan(0);
    expect(headless).toBeLessThan(trayIcon);
    expect(cliSource).toContain("resolveTrayLaunch");
    expect(cliSource).not.toContain("skipUpdate && !trayMode && !process.stdin.isTTY");
  });
});
