// a subscription model is named with the version its alias served last; a run names what served it.
import { afterEach, beforeEach, expect, it } from "vitest";
import { setLocale } from "../i18n.svelte";
import type { LlmModel } from "../types";
import { models } from "./models.svelte";

const opus = (over: Partial<LlmModel> = {}): LlmModel => ({
  id: "claude:opus", title: "Claude Opus", provider: "subscription", enabled: 1, key: true, experimental: false, builtin: true, ...over,
});
const own: LlmModel = { id: "or-x", title: "GPT-5 via OpenRouter", provider: "openrouter", enabled: 1, key: true, experimental: true, version: "" };

beforeEach(() => {
  setLocale("ru");
  models.list = [opus({ version: "claude-opus-5-5" }), own];
});
afterEach(() => models.reset());

it("a subscription model is named with the version its alias served last", () => {
  expect(models.title(opus({ version: "claude-opus-5-5" }))).toBe("Claude Opus 5.5 · подписка");
  expect(models.title(opus({ version: "claude-opus-4-5-20251101" }))).toBe("Claude Opus 4.5 · подписка");
  expect(models.name("claude:opus")).toBe("Claude Opus 5.5 · подписка");
});

it("a version the name does not read as a number is shown as it is", () => {
  expect(models.title(opus({ version: "claude-opus-next" }))).toBe("Claude Opus (claude-opus-next) · подписка");
});

it("without a version a model is named as before", () => {
  expect(models.title(opus())).toBe("Claude Opus · подписка");
  expect(models.title(opus({ version: "" }))).toBe("Claude Opus · подписка");
  expect(models.title(own)).toBe("GPT-5 via OpenRouter");
});

it("what served a run: the first model, a cabinet model by its title, and how many more", () => {
  expect(models.served(["claude-opus-5-5", "claude-haiku-4-5"])).toBe("claude-opus-5-5 +1");
  expect(models.served(["claude-sonnet-5-5"])).toBe("claude-sonnet-5-5");
  expect(models.served(["or-x"])).toBe("GPT-5 via OpenRouter");
  expect(models.served([])).toBe("");
});
