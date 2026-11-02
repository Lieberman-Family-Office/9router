import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ connections: [], settings: {}, disabled: {}, custom: [], aliases: {}, live: [] }));
vi.mock("@/lib/localDb", () => ({
  getProviderConnections: async () => mocks.connections,
  getSettings: async () => mocks.settings,
  getCombos: async () => [],
  getCustomModels: async () => mocks.custom,
  getModelAliases: async () => mocks.aliases,
}));
vi.mock("@/models", () => ({
  getProviderConnectionById: async () => mocks.connections[0],
  getCustomModels: async () => mocks.custom,
  getModelAliases: async () => mocks.aliases,
  setModelAlias: vi.fn(), addCustomModel: vi.fn(),
}));
vi.mock("@/lib/disabledModelsDb", () => ({ getDisabledModels: async () => mocks.disabled }));
vi.mock("open-sse/services/copilotModels.js", () => ({ resolveCopilotModels: async () => ({ models: mocks.live }) }));
const { buildModelsList } = await import("../../src/app/api/v1/models/route.js");

beforeEach(() => {
  mocks.connections = [{ id: "ordinary", provider: "codex", authType: "oauth", isActive: true }];
  mocks.settings = { codexUltrafastConnectionId: "eligible" };
  mocks.disabled = {};
  mocks.custom = [];
  mocks.aliases = {};
  mocks.live = [];
});

it.each(["static", "enabled", "live", "custom", "alias"])("does not advertise removed models from %s catalogs", async source => {
  if (source === "static") mocks.connections = [];
  if (source === "enabled") mocks.connections[0].providerSpecificData = { enabledModels: ["gpt-5.4-mini", "gpt-6-astra"] };
  if (source === "custom") mocks.custom = [{ id: "gpt-5.4-mini-review", providerAlias: "cx" }];
  if (source === "alias") mocks.aliases = { title: "cx/gpt-5.4-mini(high)" };
  if (source === "live") {
    mocks.connections = [{ id: "copilot", provider: "github", isActive: true }];
    mocks.live = [{ id: "gpt-5.4-mini" }, { id: "gpt-5.4" }];
  }
  const models = await buildModelsList(["llm"]);
  expect(models.length).toBeGreaterThan(0);
  expect(models.some(model => model.id.includes("gpt-5.4-mini"))).toBe(false);
  expect(models.some(model => model.id.endsWith(source === "live" ? "/gpt-5.4" : "/gpt-6-astra"))).toBe(true);
});

afterEach(() => vi.unstubAllGlobals());

it.each(["codex", "github", "openai-compatible-test", "anthropic-compatible-test"])("filters removed models from %s discovery", async provider => {
  mocks.connections = [{ id: "test", provider, accessToken: "test-token", apiKey: "test-key", providerSpecificData: { baseUrl: "https://models.test/v1" } }];
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ data: [
    { id: "gpt-5.4-mini", capabilities: { type: "chat" } },
    { id: "gpt-6-astra", capabilities: { type: "chat" } },
  ] })));
  const { GET } = await import("../../src/app/api/providers/[id]/models/route.js");
  const response = await GET(null, { params: Promise.resolve({ id: "test" }) });
  expect(response.status).toBe(200);
  const { models } = await response.json();
  expect(models.some(model => model.id === "gpt-6-astra")).toBe(true);
  expect(models.some(model => model.id.includes("gpt-5.4-mini"))).toBe(false);
});

it("hides removed custom entries, aliases, and suggestions and refuses re-adding them", async () => {
  mocks.custom = [{ id: "gpt-5.4-mini", providerAlias: "cx" }, { id: "gpt-6-astra", providerAlias: "cx" }];
  mocks.aliases = { title: "cx/gpt-5.4-mini", good: "cx/gpt-6-astra" };
  const custom = await import("../../src/app/api/models/custom/route.js");
  const aliases = await import("../../src/app/api/models/alias/route.js");
  const dashboard = await import("../../src/app/api/models/route.js");
  expect((await (await custom.GET()).json()).models.map(model => model.id)).toEqual(["gpt-6-astra"]);
  expect((await (await aliases.GET()).json()).aliases).toEqual({ good: "cx/gpt-6-astra" });
  expect((await (await dashboard.GET()).json()).models.some(model => model.model.includes("gpt-5.4-mini"))).toBe(false);
  const request = () => ({ json: async () => ({ id: "gpt-5.4-mini", providerAlias: "cx", alias: "title", model: "cx/gpt-5.4-mini" }) });
  for (const write of [custom.POST, aliases.PUT, dashboard.PUT]) expect((await write(request())).status).toBe(400);
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ data: mocks.custom })));
  const suggested = await import("../../src/app/api/providers/suggested-models/route.js");
  const response = await suggested.GET(new Request("http://localhost/api/providers/suggested-models?type=opencode-go&url=https://models.test"));
  expect((await response.json()).data.map(model => model.id)).toEqual(["gpt-6-astra"]);
});

it("rejects removed object-form alias targets without rejecting supported targets", async () => {
  const aliases = await import("../../src/app/api/models/alias/route.js");
  const { setModelAlias } = await import("@/models");
  setModelAlias.mockClear();
  const request = model => ({ json: async () => ({ alias: "title", model: { provider: "codex", model } }) });
  expect((await aliases.PUT(request("gpt-5.4-mini"))).status).toBe(400);
  expect(setModelAlias).not.toHaveBeenCalled();
  expect((await aliases.PUT(request("gpt-6-astra"))).status).toBe(200);
  expect(setModelAlias).toHaveBeenCalledTimes(1);
});

it("does not expose retired alias names on supported dashboard models", async () => {
  mocks.aliases = { "cx/gpt-6-astra": "gpt-5.4-mini", "cx/custom-retained": "cx/gpt-5.4-mini-review" };
  mocks.custom = [{ id: "custom-retained", providerAlias: "cx" }];
  const { GET } = await import("../../src/app/api/models/route.js");
  const { models } = await (await GET()).json();
  expect(models.find(model => model.fullModel === "cx/gpt-6-astra").alias).toBe("gpt-6-astra");
  expect(models.find(model => model.fullModel === "cx/custom-retained").alias).toBe("custom-retained");
});

it.each(["unconfigured", "missing", "inactive", "apikey", "eligible"])("advertises dedicated Ultrafast IDs only for a configured active OAuth account (%s)", async state => {
  if (state === "unconfigured") mocks.settings = {};
  if (!["missing", "unconfigured"].includes(state)) mocks.connections.push({
    id: "eligible", provider: "codex", authType: state === "apikey" ? "apikey" : "oauth",
    isActive: state !== "inactive", providerSpecificData: { enabledModels: ["gpt-6-astra", "gpt-6.1-sol"] },
  });
  const models = await buildModelsList(["llm"], { skipDynamicFetch: true });
  const dedicated = models.filter(model => model.id.endsWith("-ultrafast")).map(model => model.id).sort();
  expect(dedicated).toEqual(state === "eligible" ? ["cx/gpt-6-astra-ultrafast", "cx/gpt-6.1-sol-ultrafast"] : []);
  expect(models.some(model => model.id === "cx/gpt-6-astra")).toBe(true);
});

it("advertises the union of allowed models across configured active OAuth accounts", async () => {
  mocks.settings = { codexUltrafastConnectionIds: ["first", "second", "inactive", "apikey"] };
  mocks.connections.push(
    { id: "first", provider: "codex", authType: "oauth", isActive: true, providerSpecificData: { enabledModels: ["gpt-6-astra"] } },
    { id: "second", provider: "codex", authType: "oauth", isActive: true, providerSpecificData: { enabledModels: ["gpt-6.1-sol"] } },
    { id: "inactive", provider: "codex", authType: "oauth", isActive: false },
    { id: "apikey", provider: "codex", authType: "apikey", isActive: true },
  );
  const models = await buildModelsList(["llm"], { skipDynamicFetch: true });
  expect(models.filter(model => model.id.endsWith("-ultrafast")).map(model => model.id).sort())
    .toEqual(["cx/gpt-6-astra-ultrafast", "cx/gpt-6.1-sol-ultrafast"]);
  mocks.connections = mocks.connections.filter(connection => !["first", "second"].includes(connection.id));
  const unavailable = await buildModelsList(["llm"], { skipDynamicFetch: true });
  expect(unavailable.some(model => model.id.endsWith("-ultrafast"))).toBe(false);
});

it("advertises only the pinned account's allowed upstream model", async () => {
  mocks.connections.push({ id: "eligible", provider: "codex", authType: "oauth", isActive: true, providerSpecificData: { enabledModels: ["gpt-6-astra"] } });
  const models = await buildModelsList(["llm"], { skipDynamicFetch: true });
  expect(models.filter(model => model.id.endsWith("-ultrafast")).map(model => model.id)).toEqual(["cx/gpt-6-astra-ultrafast"]);
});

it.each(["only-pinned", "explicit-first"])("includes usable dedicated IDs with an upstream-only first allowlist (%s)", async state => {
  const pinned = { id: "eligible", provider: "codex", authType: "oauth", isActive: true, providerSpecificData: { enabledModels: ["gpt-6-astra", "gpt-6.1-sol"] } };
  mocks.connections = state === "only-pinned" ? [pinned] : [{ ...mocks.connections[0], providerSpecificData: { enabledModels: ["gpt-6-astra"] } }, pinned];
  const models = await buildModelsList(["llm"], { skipDynamicFetch: true });
  expect(models.filter(model => model.id.endsWith("-ultrafast")).map(model => model.id).sort()).toEqual(["cx/gpt-6-astra-ultrafast", "cx/gpt-6.1-sol-ultrafast"]);
});

it("does not advertise a disabled dedicated route", async () => {
  mocks.connections.push({ id: "eligible", provider: "codex", authType: "oauth", isActive: true });
  mocks.disabled = { cx: ["gpt-6-astra-ultrafast"] };
  const models = await buildModelsList(["llm"], { skipDynamicFetch: true });
  expect(models.some(model => model.id === "cx/gpt-6-astra-ultrafast")).toBe(false);
  expect(models.some(model => model.id === "cx/gpt-6.1-sol-ultrafast")).toBe(true);
});
