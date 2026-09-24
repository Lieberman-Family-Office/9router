"use strict";

/**
 * Decide whether a `ps`/`wmic` process line is a 9router instance for appPort.
 * Never match bare "next-server" without port evidence — that kills other
 * instances (e.g. a lab on :21129 wiping prod on :20128).
 */
function cliMentionsPort(cmd, appPort) {
  const p = String(appPort);
  const c = String(cmd).toLowerCase();
  // Only explicit CLI port flags — never bare ":PORT" (too easy to false-match).
  return (
    c.includes(`--port ${p}`) ||
    c.includes(`--port=${p}`) ||
    c.includes(`-p ${p}`) ||
    c.includes(`-p=${p}`)
  );
}

function isOwnNineRouterCli(cmd, appPort) {
  const c = String(cmd).toLowerCase();
  if (!(c.includes("node") && c.includes("9router"))) return false;
  if (!(c.includes("cli.js") || c.includes("/9router") || c.includes("\\9router"))) return false;
  return cliMentionsPort(c, appPort);
}

/**
 * @param {string} line lowercase or mixed process listing line
 * @param {number|string} appPort
 * @param {string} selfPid
 * @returns {string|null} pid to kill, or null
 */
function pidFromAppProcessLine(line, appPort, selfPid) {
  const cmd = String(line).toLowerCase();
  if (!isOwnNineRouterCli(cmd, appPort)) return null;
  const parts = String(line).trim().split(/\s+/);
  // ps aux: USER PID ... — pid is index 1
  // csv from powershell handled separately by caller
  const pid = parts[1];
  if (!pid || Number.isNaN(Number(pid))) return null;
  if (String(pid) === String(selfPid)) return null;
  return String(pid);
}

module.exports = {
  cliMentionsPort,
  isOwnNineRouterCli,
  pidFromAppProcessLine,
};
