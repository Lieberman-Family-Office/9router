// GET /api/oauth/cursor/auto-import — current contract (route redesigned 2026-03:
// multi-path probe on every platform, better-sqlite3 → sqlite3 CLI → manual paste).
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as fsPromises from "fs/promises";
import * as childProcess from "child_process";

vi.mock("next/server", () => ({
  NextResponse: {
    json: vi.fn((body, init) => ({ status: init?.status || 200, body })),
  },
}));

vi.mock("os", () => ({
  default: { homedir: vi.fn(() => "/mock/home") },
  homedir: vi.fn(() => "/mock/home"),
}));

vi.mock("fs/promises", () => ({
  access: vi.fn(),
  constants: { R_OK: 4 },
}));

// Token extraction is exercised through strategy 2 (sqlite3 CLI via execFile, also used
// for `which cursor`). Strategy 1 loads better-sqlite3 with a module-scoped CommonJS
// require() that vi.mock cannot intercept; against the mocked db path it always fails
// (no native binding, or fileMustExist on a path that does not exist) and the route
// falls through to the CLI — so these tests are deterministic on every platform.
// Strategy 1 itself is NOT unit-covered here.
vi.mock("child_process", () => ({ execFile: vi.fn() }));

const DARWIN_DB = "/mock/home/Library/Application Support/Cursor/User/globalStorage/state.vscdb";
let GET;

function execFileReplies(handler) {
  vi.mocked(childProcess.execFile).mockImplementation((cmd, args, opts, cb) => {
    const done = typeof opts === "function" ? opts : cb;
    try { done(null, { stdout: handler(cmd, args) ?? "", stderr: "" }); } catch (e) { done(e); }
  });
}

describe("GET /api/oauth/cursor/auto-import", () => {
  const originalPlatform = process.platform;

  beforeEach(async () => {
    vi.clearAllMocks();
    execFileReplies(() => { throw new Error("not available"); });
    Object.defineProperty(process, "platform", { value: "darwin", writable: true });
    vi.resetModules();
    ({ GET } = await import("../../src/app/api/oauth/cursor/auto-import/route.js"));
  });

  afterEach(() => {
    Object.defineProperty(process, "platform", { value: originalPlatform, writable: true });
  });

  it("reports every probed location when no Cursor db is readable", async () => {
    vi.mocked(fsPromises.access).mockRejectedValue(new Error("ENOENT"));
    const { body } = await GET();
    expect(body.found).toBe(false);
    expect(body.error).toContain("Cursor database not found. Checked locations:");
    expect(body.error).toContain(DARWIN_DB);
    expect(body.error).toContain("Cursor - Insiders");
  });

  it("extracts tokens via the sqlite3 CLI, first matching key wins, JSON strings unwrapped", async () => {
    vi.mocked(fsPromises.access).mockResolvedValue();
    const queried = [];
    execFileReplies((cmd, args) => {
      if (cmd !== "sqlite3") throw new Error("unexpected");
      queried.push(args[1]);
      if (args[1].includes("'cursorAuth/accessToken'")) return '"json-token"\n';
      if (args[1].includes("'storage.serviceMachineId'")) return "cli-machine\n";
      if (args[1].includes("'storage.machineId'")) return "should-not-be-used\n";
      return "";
    });
    const { body } = await GET();
    expect(body).toEqual({ found: true, accessToken: "json-token", machineId: "cli-machine" });
    expect(queried.some((q) => q.includes("'storage.machineId'"))).toBe(false);
  });

  it("asks for a manual paste (with the db path) when neither strategy finds tokens", async () => {
    vi.mocked(fsPromises.access).mockResolvedValue();
    const { body } = await GET();
    expect(body).toEqual({ found: false, windowsManual: true, dbPath: DARWIN_DB });
  });

  it("linux: config present but Cursor not installed → skips import", async () => {
    Object.defineProperty(process, "platform", { value: "linux", writable: true });
    // First access() = db candidate (found); desktop-file probe fails; `which cursor` fails.
    vi.mocked(fsPromises.access).mockResolvedValueOnce().mockRejectedValue(new Error("ENOENT"));
    const { body } = await GET();
    expect(body.found).toBe(false);
    expect(body.error).toContain("does not appear to be installed");
  });

  it("unexpected failure → 500 with the error message", async () => {
    vi.mocked(fsPromises.access).mockResolvedValue();
    const { homedir } = await import("os");
    vi.mocked(homedir).mockImplementationOnce(() => { throw new Error("boom"); });
    const res = await GET();
    expect(res.status).toBe(500);
    expect(res.body).toEqual({ found: false, error: "boom" });
  });
});
