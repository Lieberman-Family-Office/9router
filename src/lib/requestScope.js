// One scope per client request, so REQUEST_DETAILS_MODE=metadata writes one
// requestDetails row per request instead of one per upstream attempt (account
// fallback and combo fallthrough each call chatCore again).
import { AsyncLocalStorage } from "node:async_hooks";

const storage = new AsyncLocalStorage();
let seq = 0;

export function runRequestScope(fn) {
  const id = `req-${Date.now()}-${(seq++).toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  return storage.run({ id, errorStatuses: [] }, fn);
}

export function getRequestScope() {
  return storage.getStore() || null;
}
