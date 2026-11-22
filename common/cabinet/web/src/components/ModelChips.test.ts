// The card's "Model" and "Effort" chips — menus for whoever has `settings`, text for the rest.
import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { models } from "../lib/stores/models.svelte";
import { projects } from "../lib/stores/projects.svelte";
import type { Card } from "../lib/types";
import ModelChips from "./ModelChips.svelte";

beforeEach(() => {
  setLocale("ru");
  models.list = [
    { id: "or-x", title: "GPT-5 via OpenRouter", provider: "openrouter", enabled: 1, key: true, experimental: true },
    { id: "fw-k", title: "Kimi via Fireworks", provider: "fireworks", enabled: 0, key: false, experimental: true },
  ];
  projects.list = [{ id: "shop", name: "Shop", route: "@dev", model: "", effort: "" }];
});
afterEach(() => {
  cleanup();
  models.reset();
  projects.reset();
});

const card = (over: Partial<Card> = {}) =>
  ({ id: "KD-1", project: "shop", model: "", effort: "", revision: 3, ...over }) as Card;

it("with settings the chips are menus: a pick sends the change", async () => {
  const onchange = vi.fn();
  render(ModelChips, { card: card(), editable: true, onchange });
  const model = screen.getByRole("button", { name: "Модель" });
  expect(model.textContent).toContain("подписка Claude");
  await fireEvent.click(model);
  // the disabled model is not offered to a card that is not on it
  expect(screen.getAllByRole("option").map((o) => o.textContent?.trim())).toEqual(["подписка Claude", "GPT-5 via OpenRouter"]);
  await fireEvent.click(screen.getByRole("option", { name: "GPT-5 via OpenRouter" }));
  expect(onchange).toHaveBeenCalledWith({ model: "or-x" });
  await fireEvent.click(screen.getByRole("button", { name: "Усилие" }));
  await fireEvent.click(screen.getByRole("option", { name: "high" }));
  expect(onchange).toHaveBeenCalledWith({ effort: "high" });
});

it("without settings the values are text, and the project's default is named", () => {
  projects.list = [{ id: "shop", name: "Shop", route: "@dev", model: "or-x", effort: "low" }];
  render(ModelChips, { card: card(), editable: false, onchange: vi.fn() });
  expect(screen.queryAllByRole("button")).toEqual([]);
  expect(document.body.textContent).toContain("проекта: GPT-5 via OpenRouter");
  expect(document.body.textContent).toContain("проекта: low");
});

it("a card on a disabled model or a kind without its key is warned", () => {
  render(ModelChips, { card: card({ model: "fw-k" }), editable: true, onchange: vi.fn() });
  expect(screen.getByRole("button", { name: "Модель" }).textContent).toContain("Kimi via Fireworks");
  expect(document.body.textContent).toContain("модель выключена");
  cleanup();
  models.list = models.list.map((m) => (m.id === "fw-k" ? { ...m, enabled: 1 } : m));
  render(ModelChips, { card: card({ model: "fw-k" }), editable: true, onchange: vi.fn() });
  expect(document.body.textContent).toContain("нет ключа провайдера на хосте");
});

it("a model of the subscription is named as one and picked by its reference (KD-9266)", async () => {
  models.list = [
    { id: "claude:opus", title: "Claude Opus", provider: "subscription", enabled: 1, key: true, experimental: false, builtin: true },
    ...models.list,
  ];
  const onchange = vi.fn();
  render(ModelChips, { card: card(), editable: true, onchange });
  await fireEvent.click(screen.getByRole("button", { name: "Модель" }));
  expect(screen.getAllByRole("option").map((o) => o.textContent?.trim())).toEqual(["подписка Claude", "Claude Opus · подписка", "GPT-5 via OpenRouter"]);
  await fireEvent.click(screen.getByRole("option", { name: "Claude Opus · подписка" }));
  expect(onchange).toHaveBeenCalledWith({ model: "claude:opus" });
});

it("a card on a subscription model is not warned about a provider key: it starts on the host's own login (KD-9266)", () => {
  models.list = [
    { id: "claude:opus", title: "Claude Opus", provider: "subscription", enabled: 1, key: false, experimental: false, builtin: true },
    ...models.list,
  ];
  render(ModelChips, { card: card({ model: "claude:opus" }), editable: true, onchange: vi.fn() });
  expect(screen.getByRole("button", { name: "Модель" }).textContent).toContain("Claude Opus · подписка");
  expect(document.body.textContent).not.toContain("нет ключа провайдера на хосте");
});
