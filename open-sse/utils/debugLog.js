// Debug logging utility — active in dev mode (NODE_ENV !== "production"),
// or in production when NINEROUTER_DBG_CHUNKS=1 (stream stall / chunk diagnostics).
// Outputs are tagged with [DBG:tag] for easy grep/filter
const isDev = process.env.NODE_ENV !== "production" || process.env.NINEROUTER_DBG_CHUNKS === "1";

function ts() {
  return new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dbg(tag, msg) {
  if (!isDev) return;
  console.log(`[${ts()}] 🐛 [DBG:${tag}] ${msg}`);
}

export const isDebugEnabled = isDev;
