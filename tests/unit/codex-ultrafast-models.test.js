import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ connections: [], settings: {}, disabled: {} }));
vi.mock("@/lib/localDb", () => ({
  getProviderConnections: async () => mocks.connections,
  getSettings: async () => mocks.settings,
  getCombos: async () => [],
  getCustomModels: async () => [],
  getModelAliases: async () => ({}),
}));
vi.mock("@/lib/disabledModelsDb", () => ({ getDisabledModels: async () => mocks.disabled }));
const { buildModelsList } = await import("../../src/app/api/v1/models/route.js");

beforeEach(() => {
  mocks.connections = [{ id: "ordinary", provider: "codex", authType: "oauth", isActive: true }];
  mocks.settings = { codexUltrafastConnectionId: "eligible" };
  mocks.disabled = {};
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

it("advertises only the pinned account's allowed upstream model", async () => {
  mocks.connections.push({ id: "eligible", provider: "codex", authType: "oauth", isActive: true, providerSpecificData: { enabledModels: ["gpt-6-astra"] } });
  const models = await buildModelsList(["llm"], { skipDynamicFetch: true });
  expect(models.filter(model => model.id.endsWith("-ultrafast")).map(model => model.id)).toEqual(["cx/gpt-6-astra-ultrafast"]);
});

it("does not advertise a disabled dedicated route", async () => {
  mocks.connections.push({ id: "eligible", provider: "codex", authType: "oauth", isActive: true });
  mocks.disabled = { cx: ["gpt-6-astra-ultrafast"] };
  const models = await buildModelsList(["llm"], { skipDynamicFetch: true });
  expect(models.some(model => model.id === "cx/gpt-6-astra-ultrafast")).toBe(false);
  expect(models.some(model => model.id === "cx/gpt-6.1-sol-ultrafast")).toBe(true);
});
