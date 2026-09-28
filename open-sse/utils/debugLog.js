// Debug logging utility. Off in production unless NINEROUTER_DBG_CHUNKS is
// exactly "1". Launchd sets that variable to "0", so chunk lines stay off.
// Outputs are tagged with [DBG:tag].
const isDev = process.env.NODE_ENV !== "production" || process.env.NINEROUTER_DBG_CHUNKS === "1";

function ts() {
  return new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dbg(tag, msg) {
  if (!isDev) return;
  console.log(`[${ts()}] 🐛 [DBG:${tag}] ${msg}`);
}

export const isDebugEnabled = isDev;
