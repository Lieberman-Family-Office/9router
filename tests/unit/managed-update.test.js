import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const effects = vi.hoisted(() => ({
  killAppProcesses: vi.fn(async () => {}),
  spawnUpdaterAndExit: vi.fn(),
}));
vi.mock("@/lib/appUpdater", () => effects);
vi.mock("next/server", () => ({
  NextResponse: { json: (body, options = {}) => Response.json(body, options) },
}));
import { POST as update } from "../../src/app/api/version/update/route.js";
import { POST as shutdown } from "../../src/app/api/version/shutdown/route.js";

let exit;

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  exit = vi.spyOn(process, "exit").mockImplementation(() => undefined);
  vi.stubEnv("NODE_ENV", "production");
  vi.stubEnv("NINEROUTER_MANAGED_WORKER", "1");
});

afterEach(() => {
  vi.clearAllTimers();
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

describe("managed release endpoints", () => {
  for (const environment of ["production", "development"]) {
    for (const [name, post] of [["update", update], ["shutdown", shutdown]]) {
      it(`${name} refuses managed ${environment} calls before process side effects`, async () => {
        vi.stubEnv("NODE_ENV", environment);

        const schedule = vi.spyOn(globalThis, "setTimeout");
        const response = await post();

        expect(response.status).toBe(409);
        expect(await response.json()).toEqual({
          success: false,
          message: "Managed releases use 9router_deploy.py with a qualified tarball.",
        });
        expect(effects.killAppProcesses).not.toHaveBeenCalled();
        expect(effects.spawnUpdaterAndExit).not.toHaveBeenCalled();
        expect(schedule).not.toHaveBeenCalled();
        expect(vi.getTimerCount()).toBe(0);
        expect(exit).not.toHaveBeenCalled();
      });
    }
  }
});

describe("unmanaged release endpoints", () => {
  it("retains production update behavior with no managed flag", async () => {
    vi.stubEnv("NINEROUTER_MANAGED_WORKER", undefined);
    effects.killAppProcesses.mockRejectedValueOnce(new Error("cleanup unavailable"));

    const response = await update();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      success: true,
      message: "Updater started. This app will exit shortly.",
    });
    expect(effects.killAppProcesses).toHaveBeenCalledTimes(1);
    expect(effects.spawnUpdaterAndExit).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    expect(exit).not.toHaveBeenCalled();

    vi.stubEnv("NINEROUTER_MANAGED_WORKER", "0");
    expect((await update()).status).toBe(200);
    expect(effects.killAppProcesses).toHaveBeenCalledTimes(2);
    expect(effects.spawnUpdaterAndExit).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
    expect(exit).not.toHaveBeenCalled();
  });

  it("retains the development update refusal", async () => {
    vi.stubEnv("NINEROUTER_MANAGED_WORKER", "0");
    vi.stubEnv("NODE_ENV", "development");

    const response = await update();

    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({
      success: false,
      message: "Update is only available in production build (9router CLI)",
    });
    expect(effects.killAppProcesses).not.toHaveBeenCalled();
    expect(effects.spawnUpdaterAndExit).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    expect(exit).not.toHaveBeenCalled();
  });

  for (const environment of ["production", "development"]) {
    it(`retains the ${environment} shutdown timer behavior`, async () => {
      vi.stubEnv("NINEROUTER_MANAGED_WORKER", environment === "production" ? undefined : "0");
      vi.stubEnv("NODE_ENV", environment);
      if (environment === "development") {
        effects.killAppProcesses.mockRejectedValueOnce(new Error("cleanup unavailable"));
      }

      const response = await shutdown();

      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({
        success: true,
        message: "Shutting down for manual update...",
      });
      expect(effects.killAppProcesses).toHaveBeenCalledTimes(1);
      expect(effects.spawnUpdaterAndExit).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(1);
      expect(exit).not.toHaveBeenCalled();

      vi.advanceTimersByTime(499);
      expect(exit).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(1);

      vi.advanceTimersByTime(1);
      expect(exit).toHaveBeenCalledTimes(1);
      expect(exit).toHaveBeenCalledWith(0);
      expect(vi.getTimerCount()).toBe(0);
    });
  }
});
