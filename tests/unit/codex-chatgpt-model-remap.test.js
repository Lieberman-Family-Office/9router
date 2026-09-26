import { describe, expect, it } from "vitest";
import {
  CODEX_CHATGPT_DEFAULT_MODEL,
  resolveCodexChatGptModel,
} from "../../open-sse/providers/codexChatGptModels.js";

describe("resolveCodexChatGptModel", () => {
  it("passes through ChatGPT-supported ids", () => {
    for (const id of ["gpt-5.5", "gpt-6-sol", "gpt-6-astra", "gpt-5.6-sol"]) {
      expect(resolveCodexChatGptModel(id)).toEqual({ model: id, remappedFrom: null });
    }
  });

  it("keeps effort suffixes on allowed models", () => {
    expect(resolveCodexChatGptModel("gpt-6-sol(high)")).toEqual({
      model: "gpt-6-sol(high)",
      remappedFrom: null,
    });
  });

  it("remaps OpenAI Platform / Codex-CLI defaults to gpt-5.5", () => {
    for (const id of ["gpt-5", "gpt-5.4", "gpt-4o", "o3", "o4-mini", "openai-gpt"]) {
      expect(resolveCodexChatGptModel(id)).toEqual({
        model: CODEX_CHATGPT_DEFAULT_MODEL,
        remappedFrom: id,
      });
    }
  });

  it("defaults empty model", () => {
    expect(resolveCodexChatGptModel("")).toEqual({
      model: CODEX_CHATGPT_DEFAULT_MODEL,
      remappedFrom: null,
    });
  });
});
