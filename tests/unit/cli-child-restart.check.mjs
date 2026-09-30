import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../cli/cli.js", import.meta.url), "utf8");
const children = [];
const timers = [];
const exits = [];
vm.runInNewContext(source.slice(source.indexOf("function startServer(")) + "\nstartServer(null);", {
  Promise, Date, path, os,
  process: { env: {}, on() {}, removeAllListeners() {}, exit: code => exits.push(code) },
  console: { log() {}, error() {} },
  require: () => ({ resolveServerSpawnOptions: () => ({ stdio: "inherit", detached: false }) }),
  spawn: () => { const child = new EventEmitter(); children.push(child); return child; },
  setTimeout: (callback, delay) => timers.push({ callback, delay }),
  buildEnvWithRuntime: () => ({}),
  getDisplayHost: () => "127.0.0.1",
  waitServerReady: () => new Promise(() => {}),
  host: "127.0.0.1", DEFAULT_HOST: "0.0.0.0", port: 20128,
  RUNTIME: "node", serverPath: "server.js", standaloneDir: ".",
  showLog: true, trayMode: true, isShuttingDown: false,
  pkg: { name: "9router", version: "test" },
  MAX_RESTARTS: 2, RESTART_RESET_MS: 30000,
});
assert.equal(children.length, 1);
children[0].emit("close", null, "SIGKILL");
assert.equal(timers.length, 1, "tray startup must attach child crash recovery");
assert.equal(timers[0].delay, 1000);
timers.shift().callback();
assert.equal(children.length, 2, "a killed child must be replaced");
children[1].emit("close", null, "SIGKILL");
assert.equal(timers.length, 1, "replacement children must retain recovery handlers");
assert.equal(timers[0].delay, 2000);
assert.deepEqual(exits, []);
console.log("PASS: tray startup and replacement children recover from SIGKILL");
