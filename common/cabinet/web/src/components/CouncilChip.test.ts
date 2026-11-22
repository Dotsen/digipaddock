// The card's "Council" in its header — who criticises its brief and code: text for everyone, an editor in a
// sheet for whoever has `settings`.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { models } from "../lib/stores/models.svelte";
import { projects } from "../lib/stores/projects.svelte";
import type { Card } from "../lib/types";
import CouncilChip from "./CouncilChip.svelte";

const COUNCIL = { brief: { critics: ["claude:opus", "or-x"], rounds: 3 }, code: { critics: ["claude:opus"], rounds: 3, min_lines: 100 } };

beforeEach(() => {
  setLocale("ru");
  // jsdom has no native modal implementation
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
  models.list = [
    { id: "claude:opus", title: "Claude Opus", provider: "subscription", enabled: 1, key: true, experimental: false, builtin: true },
    { id: "or-x", title: "GPT-5 via OpenRouter", provider: "openrouter", enabled: 1, key: true, experimental: true },
  ];
  projects.list = [{ id: "shop", name: "Shop", route: "@dev", council: COUNCIL }];
});
afterEach(() => {
  cleanup();
  models.reset();
  projects.reset();
});

const card = (over: Partial<Card> = {}) => ({ id: "KD-1", project: "shop", council: {}, revision: 3, ...over }) as Card;

it("without settings the council in force is text: the project's by default", () => {
  render(CouncilChip, { card: card(), editable: false, onchange: vi.fn() });
  expect(screen.queryAllByRole("button")).toEqual([]);
  expect(document.body.textContent).toContain("ТЗ: 2 · код: 1");
});

it("the card's own kind is counted over the project's", () => {
  render(CouncilChip, { card: card({ council: { brief: { critics: [], rounds: 3 } } }), editable: false, onchange: vi.fn() });
  expect(document.body.textContent).toContain("код: 1");
  expect(document.body.textContent).not.toContain("ТЗ");
});

it("with settings the chip opens the editor; a save sends the card's council and closes it", async () => {
  const onchange = vi.fn();
  render(CouncilChip, { card: card(), editable: true, onchange });
  await fireEvent.click(screen.getByRole("button", { name: "Совет моделей" }));
  const sheet = within(screen.getByRole("dialog", { name: "Совет моделей" }));
  await fireEvent.change(within(sheet.getByRole("group", { name: "ТЗ" })).getByLabelText("Состав"), { target: { value: "off" } });
  await fireEvent.click(sheet.getByRole("button", { name: "Сохранить" }));
  expect(onchange).toHaveBeenCalledWith({ council: { brief: { critics: [], rounds: 3 } } });
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("a save that changes nothing closes the sheet and writes nothing: the card's revision stays", async () => {
  const onchange = vi.fn();
  render(CouncilChip, { card: card({ council: { brief: { critics: ["or-x"], rounds: 2 } } }), editable: true, onchange });
  await fireEvent.click(screen.getByRole("button", { name: "Совет моделей" }));
  await fireEvent.click(within(screen.getByRole("dialog", { name: "Совет моделей" })).getByRole("button", { name: "Сохранить" }));
  expect(onchange).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("the editor opens from outside too: the council's block in the thread asks for it (KD-9267)", () => {
  render(CouncilChip, { card: card(), editable: true, open: true, onchange: vi.fn() });
  expect(screen.getByRole("dialog", { name: "Совет моделей" })).toBeTruthy();
});
