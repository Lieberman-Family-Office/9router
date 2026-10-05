import { createHash, randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import managed from "../../../src/lib/db/managed.cjs";

const quietRefreshLog = Object.freeze({ info() {}, warn() {}, error() {}, debug() {} });
export function refreshLogger(log) {
  return process.env.NINEROUTER_MANAGED_WORKER === "1" ? quietRefreshLog : log;
}

let activeRefreshOperations = 0;
export function getRefreshWorkStatus() { return { activeRefreshOperations }; }

function validResult(result, generation) {
  return result && typeof result === "object" && !Array.isArray(result) &&
    Number.isSafeInteger(generation) && generation > 0 &&
    result.refreshGeneration === generation &&
    ["accessToken", "apiKey", "token", "copilotToken"].some(key => typeof result[key] === "string" && result[key]);
}

async function managedRefresh(provider, oldToken, fn) {
  if (typeof provider !== "string" || !provider || typeof oldToken !== "string" || !oldToken) {
    throw new Error("Managed refresh requires a token identity");
  }
  const file = process.env.NINEROUTER_HOTSWAP_REFRESH_DB;
  if (!file) throw new Error("Managed refresh enrollment required");
  const waitMs = Number(process.env.NINEROUTER_HOTSWAP_REFRESH_WAIT_MS ?? 30000);
  if (!Number.isSafeInteger(waitMs) || waitMs < 1 || waitMs > 30000) throw new Error("Invalid refresh wait deadline");
  const hash = createHash("sha256");
  for (const part of [provider, oldToken]) {
    const bytes = Buffer.from(part);
    hash.update(String(bytes.length)).update(":").update(bytes);
  }
  const key = hash.digest("hex");
  const owner = randomUUID();
  const db = managed.openRefreshStore(file);
  activeRefreshOperations++;
  try {
    db.exec("BEGIN IMMEDIATE");
    let claimed;
    try {
      claimed = db.prepare("INSERT OR IGNORE INTO refresh_flights(key,owner,state,started_at) VALUES(?,?,'pending',?)")
        .run(key, owner, new Date().toISOString()).changes === 1;
      db.exec("COMMIT");
    } catch { db.exec("ROLLBACK"); throw new Error("Refresh claim refused"); }
    if (!claimed) {
      const deadline = Date.now() + waitMs;
      while (true) {
        const flight = db.prepare("SELECT state,result,generation FROM refresh_flights WHERE key=?").get(key);
        if (flight?.state === "done") {
          let result;
          try { result = JSON.parse(flight.result); } catch { throw new Error("Invalid durable refresh result"); }
          if (!validResult(result, flight.generation)) throw new Error("Invalid durable refresh result");
          return result;
        }
        if (flight?.state !== "pending") throw new Error("Uncertain refresh requires maintenance recovery");
        if (Date.now() >= deadline) throw new Error("Refresh owner unresolved; replay refused");
        await delay(25);
      }
    }
    try {
      // The issuer must execute outside every SQLite transaction.
      const response = await fn();
      if (!response || typeof response !== "object" || Array.isArray(response) || response.error ||
          !["accessToken", "apiKey", "token", "copilotToken"].some(field => typeof response[field] === "string" && response[field])) {
        throw new Error("Uncertain issuer outcome");
      }
      db.exec("BEGIN IMMEDIATE");
      try {
        const sequence = db.prepare("SELECT value FROM refresh_sequence WHERE id=1").get()?.value;
        if (!Number.isSafeInteger(sequence) || sequence < 0 || sequence >= Number.MAX_SAFE_INTEGER) {
          throw new Error("Invalid refresh sequence");
        }
        const result = JSON.parse(JSON.stringify({ ...response, refreshGeneration: sequence + 1 }));
        if (!validResult(result, sequence + 1)) throw new Error("Invalid refresh result");
        db.prepare("UPDATE refresh_sequence SET value=? WHERE id=1").run(sequence + 1);
        const saved = db.prepare("UPDATE refresh_flights SET state='done',result=?,generation=? WHERE key=? AND owner=? AND state='pending'")
          .run(JSON.stringify(result), sequence + 1, key, owner);
        if (saved.changes !== 1) throw new Error("Refresh ownership changed");
        db.exec("COMMIT");
        return result;
      } catch { db.exec("ROLLBACK"); throw new Error("Durable refresh completion failed"); }
    } catch {
      // Keep pending if even the uncertain marker cannot be written. Never reclaim.
      try { db.prepare("UPDATE refresh_flights SET state='uncertain' WHERE key=? AND owner=? AND state='pending'").run(key, owner); } catch {}
      throw new Error("Uncertain refresh requires maintenance recovery");
    }
  } finally { activeRefreshOperations--; db.close(); }
}

const REFRESH_RESULT_TTL_MS = 10_000;
const refreshDedupCache = new Map();

export async function dedupRefresh(provider, oldToken, fn, log) {
  if (process.env.NINEROUTER_MANAGED_WORKER === "1") return managedRefresh(provider, oldToken, fn);
  if (!oldToken) return fn();
  const key = `${provider}:${oldToken}`;
  const hit = refreshDedupCache.get(key);
  if (hit) {
    if (hit.promise) {
      log?.info?.("TOKEN_REFRESH", `Reusing in-flight refresh for ${provider}`);
      return hit.promise;
    }
    if (hit.expiresAt > Date.now()) {
      log?.info?.("TOKEN_REFRESH", `Reusing recent refresh result for ${provider}`);
      return hit.result;
    }
    refreshDedupCache.delete(key);
  }
  const promise = (async () => {
    try {
      const result = await fn();
      refreshDedupCache.set(key, { result, expiresAt: Date.now() + REFRESH_RESULT_TTL_MS });
      return result;
    } catch (err) {
      refreshDedupCache.delete(key);
      throw err;
    }
  })();
  refreshDedupCache.set(key, { promise });
  return promise;
}
