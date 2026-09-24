"use strict";

/**
 * Resolve stdio + detached for the Next standalone child.
 *
 * Historical default was `detached: true` with stderr piped to the CLI parent.
 * If the parent event loop stalls (e.g. tray spawn failure on macOS), the pipe
 * buffer fills and next-server blocks on stderr writes while the listen socket
 * stays open — the production "wedged but listening" symptom.
 *
 * Attached mode (launchd / NINEROUTER_ATTACHED_SERVER=1): same process group,
 * stderr ignored (or inherited when showLog). Detached mode: stderr goes to a
 * crash-log file fd, never a pipe back to the parent.
 */

function isAttachedEnv(env = process.env) {
  const v = env.NINEROUTER_ATTACHED_SERVER;
  return v === "1" || v === "true" || v === "yes";
}

/**
 * @param {{ showLog?: boolean, attached?: boolean, openCrashLog?: () => number|"ignore" }} opts
 * @returns {{ stdio: import("child_process").StdioOptions, detached: boolean }}
 */
function resolveServerSpawnOptions(opts = {}) {
  const showLog = !!opts.showLog;
  const attached = opts.attached != null ? !!opts.attached : isAttachedEnv();
  if (showLog) {
    return { stdio: "inherit", detached: !attached };
  }
  if (attached) {
    return { stdio: ["ignore", "ignore", "ignore"], detached: false };
  }
  let stderr = "ignore";
  if (typeof opts.openCrashLog === "function") {
    try {
      stderr = opts.openCrashLog();
    } catch {
      stderr = "ignore";
    }
  }
  if (stderr !== "ignore" && typeof stderr !== "number") {
    stderr = "ignore";
  }
  return { stdio: ["ignore", "ignore", stderr], detached: true };
}

/** Reject the old pipe-to-parent shape explicitly for tests. */
function usesParentStderrPipe(stdio) {
  if (stdio === "inherit" || stdio === "pipe" || stdio === "ignore") return stdio === "pipe";
  if (!Array.isArray(stdio)) return false;
  return stdio[2] === "pipe";
}

module.exports = {
  isAttachedEnv,
  resolveServerSpawnOptions,
  usesParentStderrPipe,
};
