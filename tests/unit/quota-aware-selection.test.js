import { describe, expect, it, vi } from "vitest";
import {
  isQuotaExhausted,
  hasExhaustedBlockingQuota,
  normalizeQuotasToSnapshot,
  sortConnectionsByRemaining,
  createQuotaSnapshotCache,
} from "../../src/sse/services/quotaAwareSelection.js";

describe("scoped quota windows", () => {
  it.each(["gpt-5.6-sol-review", "gpt-5.6-sol-review(high)"])("uses general limits for local review %s", model => {
    for (const remaining of [0, 50]) {
      const usage = { plan: "pro", quotas: { weekly: { remaining }, review_weekly: { remaining: remaining ? 0 : 50 } } };
      expect(normalizeQuotasToSnapshot("codex", usage, model).blockingExhausted).toBe(remaining === 0);
    }
  });
  it.each(["gpt-5.3-codex-spark", "gpt-5.3-codex-spark(high)", "gpt-5.3-codex-spark-review", "gpt-5.3-codex-spark-review(xhigh)"])("admits exact Spark aliases using Pro Spark windows: %s", model => {
    for (const remaining of [0, 50]) {
      const usage = { plan: "pro", quotas: { weekly: { remaining: remaining ? 0 : 50 }, spark_weekly: { remaining }, review_weekly: { remaining: 0 } } };
      expect(normalizeQuotasToSnapshot("codex", usage, model).blockingExhausted).toBe(remaining === 0);
    }
  });
  it.each([undefined, "unknown", "plus", "team"])("retains general limits for Spark without reported Pro: %s", plan => {
    for (const remaining of [0, 50]) {
      const usage = { plan, quotas: { weekly: { remaining }, spark_weekly: { remaining: remaining ? 0 : 50 } } };
      expect(normalizeQuotasToSnapshot("codex", usage, "gpt-5.3-codex-spark").blockingExhausted).toBe(remaining === 0);
    }
  });
  it("retains general limits for missing Spark windows and future Spark models", () => {
    for (const remaining of [0, 50]) {
      const usage = { plan: "pro", quotas: { weekly: { remaining } } };
      expect(normalizeQuotasToSnapshot("codex", usage, "gpt-5.3-codex-spark").blockingExhausted).toBe(remaining === 0);
      usage.quotas.spark_weekly = { remaining: remaining ? 0 : 50 };
      expect(normalizeQuotasToSnapshot("codex", usage, "gpt-6-codex-spark").blockingExhausted).toBe(remaining === 0);
    }
  });
  it("preserves the session-only exhaustion policy", () => {
    expect(normalizeQuotasToSnapshot("codex", { plan: "pro", quotas: { spark_session: { remaining: 0 } } }, "gpt-5.3-codex-spark").blockingExhausted).toBe(false);
  });
  it("uses the latest applicable Claude reset, refusing unknown or invalid resets", () => {
    const early = "2026-10-01T01:00:00.000Z";
    const late = "2026-10-01T03:00:00.000Z";
    const usage = { quotas: { "weekly (7d)": { remaining: 0, resetAt: early }, "weekly sonnet (7d)": { remaining: 0, resetAt: late }, "weekly opus (7d)": { remaining: 0 } } };
    expect(normalizeQuotasToSnapshot("claude", usage, "claude-sonnet-4-6").blockingResetAt).toBe(late);
    for (const resetAt of [null, "invalid"]) {
      usage.quotas["weekly sonnet (7d)"].resetAt = resetAt;
      expect(normalizeQuotasToSnapshot("claude", usage, "claude-sonnet-4-6").blockingResetAt).toBeNull();
    }
    usage.quotas["weekly (7d)"].remaining = 50;
    expect(normalizeQuotasToSnapshot("claude", usage, "claude-haiku-4-5").blockingExhausted).toBe(false);
  });
});

describe("isQuotaExhausted", () => {
  it("treats remaining <= 0 as exhausted", () => {
    expect(isQuotaExhausted({ remaining: 0, total: 100 })).toBe(true);
  });
  it("treats unlimited as not exhausted", () => {
    expect(isQuotaExhausted({ unlimited: true, remaining: 0 })).toBe(false);
  });
  it("uses used/total when remaining absent", () => {
    expect(isQuotaExhausted({ used: 100, total: 100 })).toBe(true);
    expect(isQuotaExhausted({ used: 50, total: 100 })).toBe(false);
  });
});

describe("hasExhaustedBlockingQuota", () => {
  it("ignores the session key but blocks on weekly", () => {
    const quotas = {
      "session (5h)": { remaining: 0, total: 100 },
      "weekly (7d)": { remaining: 0, total: 100 },
    };
    expect(hasExhaustedBlockingQuota(quotas, "session (5h)")).toBe(true);
  });
  it("does not block when only session is exhausted", () => {
    const quotas = {
      "session (5h)": { remaining: 0, total: 100 },
      "weekly (7d)": { remaining: 40, total: 100 },
    };
    expect(hasExhaustedBlockingQuota(quotas, "session (5h)")).toBe(false);
  });
});

describe("normalizeQuotasToSnapshot", () => {
  it("prefers Claude session (5h) for remainingFraction", () => {
    const snap = normalizeQuotasToSnapshot("claude", {
      quotas: {
        "session (5h)": { remaining: 25, total: 100, remainingPercentage: 25 },
        "weekly (7d)": { remaining: 90, total: 100, remainingPercentage: 90 },
      },
    });
    expect(snap.remainingFraction).toBeCloseTo(0.25, 5);
    expect(snap.blockingExhausted).toBe(false);
  });
  it("marks blockingExhausted when weekly is empty", () => {
    const snap = normalizeQuotasToSnapshot("claude", {
      quotas: {
        "session (5h)": { remaining: 50, total: 100, remainingPercentage: 50 },
        "weekly (7d)": { remaining: 0, total: 100, remainingPercentage: 0, resetAt: "2026-09-03T12:00:00.000Z" },
      },
    });
    expect(snap.blockingExhausted).toBe(true);
    expect(snap.blockingResetAt).toBe("2026-09-03T12:00:00.000Z");
  });
  it("prefers Codex session key", () => {
    const snap = normalizeQuotasToSnapshot("codex", {
      quotas: {
        session: { remaining: 10, total: 100 },
        weekly: { remaining: 80, total: 100 },
      },
    });
    expect(snap.remainingFraction).toBeCloseTo(0.1, 5);
  });
});

describe("sortConnectionsByRemaining", () => {
  it("orders higher remainingFraction first", () => {
    const sorted = sortConnectionsByRemaining([
      { id: "low", backoffLevel: 0, lastUsedAt: "2026-09-01T00:00:00.000Z", _quotaSnapshot: { remainingFraction: 0.1 } },
      { id: "high", backoffLevel: 0, lastUsedAt: "2026-09-01T00:00:00.000Z", _quotaSnapshot: { remainingFraction: 0.9 } },
    ]);
    expect(sorted.map((c) => c.id)).toEqual(["high", "low"]);
  });
  it("tie-breaks lower backoff then older lastUsedAt", () => {
    const sorted = sortConnectionsByRemaining([
      { id: "b", backoffLevel: 2, lastUsedAt: "2026-09-01T02:00:00.000Z", _quotaSnapshot: { remainingFraction: 0.5 } },
      { id: "a", backoffLevel: 0, lastUsedAt: "2026-09-01T01:00:00.000Z", _quotaSnapshot: { remainingFraction: 0.5 } },
    ]);
    expect(sorted[0].id).toBe("a");
  });
  it("sorts unknown snapshots after known positive remaining", () => {
    const sorted = sortConnectionsByRemaining([
      { id: "unknown", backoffLevel: 0, _quotaSnapshot: { remainingFraction: null, unknown: true } },
      { id: "known", backoffLevel: 0, _quotaSnapshot: { remainingFraction: 0.2 } },
    ]);
    expect(sorted.map((c) => c.id)).toEqual(["known", "unknown"]);
  });
});

describe("createQuotaSnapshotCache", () => {
  it("bounds account entries and expires idle values", async () => {
    let now = 10;
    const cache = createQuotaSnapshotCache({ maxEntries: 2, staleOkMs: 100, now: () => now });
    for (const id of ["a", "b", "c"]) await cache.getOrFetch(id, async () => ({ quotas: {} }), "fingerprint");
    expect(cache.get("a")).toBeNull();
    expect(cache.get("b")).not.toBeNull();
    now += 101;
    expect(cache.get("b")).toBeNull();
    expect(cache.get("c")).toBeNull();
  });

  it("invalidates rotation without allowing the old flight to overwrite the new value", async () => {
    const cache = createQuotaSnapshotCache();
    let resolveOld;
    const old = cache.getOrFetch("account", () => new Promise(resolve => { resolveOld = resolve; }), "old-fingerprint");
    await cache.getOrFetch("account", async () => ({ quotas: { weekly: { remaining: 90 } } }), "new-fingerprint");
    resolveOld({ quotas: { weekly: { remaining: 0 } } });
    await old;
    const fetcher = vi.fn();
    expect((await cache.getOrFetch("account", fetcher, "new-fingerprint")).quotas.weekly.remaining).toBe(90);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("singleflights concurrent fetches", async () => {
    let calls = 0;
    const cache = createQuotaSnapshotCache({ ttlMs: 60_000, now: () => 1_000_000 });
    const fetcher = async () => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 20));
      return { remainingFraction: 0.5, fetchedAt: 1_000_000 };
    };
    const [a, b] = await Promise.all([
      cache.getOrFetch("c1", fetcher),
      cache.getOrFetch("c1", fetcher),
    ]);
    expect(calls).toBe(1);
    expect(a.remainingFraction).toBe(0.5);
    expect(b.remainingFraction).toBe(0.5);
  });

  it("returns last good snapshot within staleOkMs after fetcher throws", async () => {
    let now = 1_000_000;
    const cache = createQuotaSnapshotCache({
      ttlMs: 1,
      staleOkMs: 60_000,
      now: () => now,
    });
    await cache.getOrFetch("c1", async () => ({ remainingFraction: 0.7, fetchedAt: now }));
    now = 1_000_050; // past ttl
    const snap = await cache.getOrFetch("c1", async () => {
      throw new Error("usage down");
    });
    expect(snap.remainingFraction).toBe(0.7);
    expect(snap.stale).toBe(true);
  });
});
