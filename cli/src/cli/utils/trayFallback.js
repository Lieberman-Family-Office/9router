"use strict";

/**
 * Launchd starts the CLI with --skip-update and no TTY. That combination
 * used to force tray mode. The macOS tray stalls the CLI event loop (systray
 * -86) while the listen socket stays open. NINEROUTER_NO_TRAY refuses the tray.
 */

function isNoTrayEnv(env = process.env) {
  const raw = env && env.NINEROUTER_NO_TRAY;
  if (raw == null) return false;
  const v = String(raw).trim().toLowerCase();
  return v === "1" || v === "true" || v === "yes";
}

/**
 * @param {{ skipUpdate?: boolean, trayRequested?: boolean, isTTY?: boolean, env?: NodeJS.ProcessEnv }} opts
 * @returns {{ trayMode: boolean, headless: boolean, initTray: boolean }}
 */
function resolveTrayLaunch(opts = {}) {
  const skipUpdate = !!opts.skipUpdate;
  const trayRequested = !!opts.trayRequested;
  const isTTY = !!opts.isTTY;
  const env = opts.env || process.env;

  if (isNoTrayEnv(env)) {
    return {
      trayMode: false,
      headless: skipUpdate && !isTTY,
      initTray: false,
    };
  }

  const auto = skipUpdate && !trayRequested && !isTTY;
  const trayMode = trayRequested || auto;
  return { trayMode, headless: false, initTray: true };
}

module.exports = {
  isNoTrayEnv,
  resolveTrayLaunch,
};
