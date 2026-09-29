import { defineConfig } from "vitest/config";
import { mkdtempSync } from "fs";
import { homedir, tmpdir } from "os";
import { join, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

// Tests write provider connections through the real DB layer. Without DATA_DIR
// that layer opens the LIVE ~/.9router/db — a 2026-09-29 run left 433 fake
// connections (zed "Account N", openai-compatible-* seed-N) on the dashboard.
// Default to a throwaway dir; refuse an explicit DATA_DIR that is the live one.
const LIVE_DIR = resolve(homedir(), ".9router");
const DATA_DIR = process.env.DATA_DIR || mkdtempSync(join(tmpdir(), "9router-test-"));
if (resolve(DATA_DIR) === LIVE_DIR) {
  throw new Error(`Refusing to run tests against the live data dir ${LIVE_DIR}. Unset DATA_DIR.`);
}

export default defineConfig({
  test: {
    environment: "node",
    globals: true,
    env: { DATA_DIR },
    include: ["**/*.test.js"],
    // Don't scan into git worktrees nested under .claude/ — they carry their
    // own copies of the test files but lack an installed node_modules (open-sse,
    // etc.), which makes provider imports fail during collection.
    exclude: ["**/node_modules/**", "**/.claude/**", "**/dist/**"],
    // Allow many it.concurrent cases (real provider smoke runs ~50 providers in parallel)
    maxConcurrency: 60,
    // Suppress noisy console output from handlers under test
    silent: false,
  },
  resolve: {
    // Use array form so subpath aliases (e.g. "@/lib/db/index.js") resolve correctly.
    alias: [
      { find: /^open-sse\//, replacement: resolve(__dirname, "../open-sse") + "/" },
      { find: "open-sse", replacement: resolve(__dirname, "../open-sse") },
      { find: /^@\//, replacement: resolve(__dirname, "../src") + "/" },
    ],
  },
});
