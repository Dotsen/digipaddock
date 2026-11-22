// the council's editor — per kind the critics (the enabled models, one may read twice), the rounds and,
// for the code, the least diff. the critics are picked at once, from a list with search; the choice «as
// the project's / its own / none» is there only on a card whose project names critics for that kind.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { models } from "../lib/stores/models.svelte";
import CouncilEditor from "./CouncilEditor.svelte";

beforeEach(() => {
  setLocale("ru");
  models.list = [
    { id: "claude:opus", title: "Claude Opus", provider: "subscription", enabled: 1, key: true, experimental: false, builtin: true },
    { id: "or-x", title: "GPT-5 via OpenRouter", provider: "openrouter", enabled: 1, key: true, experimental: true },
    { id: "fw-k", title: "Kimi via Fireworks", provider: "fireworks", enabled: 0, key: false, experimental: true },
  ];
});
afterEach(() => {
  cleanup();
  models.reset();
});

const kind = (name: string) => within(screen.getByRole("group", { name }));
const save = () => screen.getByRole("button", { name: "Сохранить" }) as HTMLButtonElement;
const critics = (name: string) => kind(name).getByRole("combobox", { name: "Критики" }) as HTMLInputElement;
const offered = async (name: string) => {
  await fireEvent.focus(critics(name));
  return within(kind(name).getByRole("listbox", { name: "Критики" })).getAllByRole("option").map((o) => o.textContent?.trim());
};

async function add(name: string, model: string) {
  await fireEvent.focus(critics(name));
  await fireEvent.click(within(kind(name).getByRole("listbox", { name: "Критики" })).getByRole("option", { name: model }));
}

it("on a card whose project has no council the critics are picked at once: two critics and two rounds", async () => {
  const onsave = vi.fn();
  render(CouncilEditor, { value: {}, inherited: {}, onsave });
  expect(screen.queryByLabelText("Состав")).toBeNull(); // nothing to take from the project, nothing to switch off
  // the disabled model is no new critic; a subscription's model is named as one
  expect(await offered("ТЗ")).toEqual(["Claude Opus · подписка", "GPT-5 via OpenRouter"]);
  expect(kind("ТЗ").queryByLabelText("Кругов")).toBeNull(); // the numbers come with the first critic
  await add("ТЗ", "Claude Opus · подписка");
  await add("ТЗ", "GPT-5 via OpenRouter");
  await fireEvent.input(kind("ТЗ").getByLabelText("Кругов"), { target: { value: "2" } });
  await fireEvent.click(save());
  expect(onsave).toHaveBeenCalledWith({ brief: { critics: ["claude:opus", "or-x"], rounds: 2 } });
});

it("one model twice is two independent reads; an eleventh critic is not added", async () => {
  const onsave = vi.fn();
  render(CouncilEditor, { value: { brief: { critics: Array(9).fill("or-x"), rounds: 3 } }, inherited: {}, onsave });
  expect(critics("ТЗ").disabled).toBe(false);
  await add("ТЗ", "GPT-5 via OpenRouter");
  expect(critics("ТЗ").disabled).toBe(true);
  await fireEvent.click(save());
  expect(onsave).toHaveBeenCalledWith({ brief: { critics: Array(10).fill("or-x"), rounds: 3 } });
});

it("no critic picked — the kind is left out; a card's «none» the project has nothing against stays as it was", async () => {
  const onsave = vi.fn();
  render(CouncilEditor, { value: {}, inherited: {}, onsave });
  await fireEvent.click(save());
  expect(onsave).toHaveBeenLastCalledWith({});
  cleanup();
  const none = { brief: { critics: [], rounds: 3 } };
  render(CouncilEditor, { value: none, inherited: {}, onsave });
  await fireEvent.click(save());
  expect(onsave).toHaveBeenLastCalledWith(none);
});

it("«без совета» on a card is the kind without critics; «как у проекта» leaves the kind out", async () => {
  const onsave = vi.fn();
  const project = { brief: { critics: ["or-x"], rounds: 3 }, code: { critics: ["claude:opus"], rounds: 3, min_lines: 100 } };
  render(CouncilEditor, { value: { code: { critics: ["or-x"], rounds: 1, min_lines: 0 } }, inherited: project, onsave });
  const mode = (name: string) => kind(name).getByLabelText("Состав") as HTMLSelectElement;
  expect([mode("ТЗ").value, mode("код").value]).toEqual(["project", "own"]);
  expect(mode("ТЗ").selectedOptions[0].text).toBe("как у проекта: 1");
  await fireEvent.change(mode("ТЗ"), { target: { value: "off" } });
  await fireEvent.change(mode("код"), { target: { value: "project" } });
  await fireEvent.click(save());
  expect(onsave).toHaveBeenCalledWith({ brief: { critics: [], rounds: 3 } });
});

it("the choice project / own / none is there only for the kind the project names critics for", async () => {
  const onsave = vi.fn();
  render(CouncilEditor, { value: {}, inherited: { brief: { critics: ["or-x"], rounds: 3 } }, onsave });
  expect(kind("код").queryByLabelText("Состав")).toBeNull();
  expect(critics("код")).toBeTruthy();
  // the brief is the project's until the card takes its own: then its critics are picked
  expect(kind("ТЗ").queryByRole("combobox", { name: "Критики" })).toBeNull();
  await fireEvent.change(kind("ТЗ").getByLabelText("Состав"), { target: { value: "own" } });
  await add("ТЗ", "Claude Opus · подписка");
  await fireEvent.click(save());
  expect(onsave).toHaveBeenCalledWith({ brief: { critics: ["claude:opus"], rounds: 3 } });
});

it("the code's council has the least diff; a critic is removed; bad numbers do not save", async () => {
  const onsave = vi.fn();
  render(CouncilEditor, { value: { code: { critics: ["or-x", "claude:opus"], rounds: 3, min_lines: 100 } }, inherited: {}, onsave });
  expect(kind("ТЗ").queryByLabelText("Порог, строк")).toBeNull();
  await fireEvent.click(kind("код").getByRole("button", { name: "Убрать: GPT-5 via OpenRouter" }));
  await fireEvent.input(kind("код").getByLabelText("Порог, строк"), { target: { value: "0" } });
  await fireEvent.input(kind("код").getByLabelText("Кругов"), { target: { value: "6" } });
  expect(save().disabled).toBe(true);
  await fireEvent.input(kind("код").getByLabelText("Кругов"), { target: { value: "5" } });
  await fireEvent.click(save());
  expect(onsave).toHaveBeenCalledWith({ code: { critics: ["claude:opus"], rounds: 5, min_lines: 0 } });
});

it("of two reads of one model the one clicked is removed", async () => {
  const onsave = vi.fn();
  render(CouncilEditor, { value: { brief: { critics: ["or-x", "claude:opus", "or-x"], rounds: 3 } }, inherited: {}, onsave });
  await fireEvent.click(kind("ТЗ").getAllByRole("button", { name: "Убрать: GPT-5 via OpenRouter" })[1]);
  await fireEvent.click(save());
  expect(onsave).toHaveBeenCalledWith({ brief: { critics: ["or-x", "claude:opus"], rounds: 3 } });
});

it("a project's default has no «as the project's»: a kind without critics is simply not there", async () => {
  const onchange = vi.fn();
  render(CouncilEditor, { value: { brief: { critics: ["or-x"], rounds: 3 } }, onchange });
  expect(screen.queryByLabelText("Состав")).toBeNull();
  expect(screen.queryByRole("button", { name: "Сохранить" })).toBeNull(); // the form around it saves
  await add("код", "Claude Opus · подписка");
  expect(onchange).toHaveBeenLastCalledWith({
    brief: { critics: ["or-x"], rounds: 3 }, code: { critics: ["claude:opus"], rounds: 3, min_lines: 100 },
  }, true);
  await fireEvent.click(kind("ТЗ").getByRole("button", { name: "Убрать: GPT-5 via OpenRouter" }));
  expect(onchange).toHaveBeenLastCalledWith({ code: { critics: ["claude:opus"], rounds: 3, min_lines: 100 } }, true);
  await fireEvent.input(kind("код").getByLabelText("Кругов"), { target: { value: "" } });
  expect(onchange.mock.lastCall?.[1]).toBe(false); // the form around it does not save a number out of range
});

it("a critic whose model is disabled since stays named; one gone shows its id", () => {
  render(CouncilEditor, { value: { brief: { critics: ["fw-k", "gone"], rounds: 3 } }, inherited: {}, onsave: vi.fn() });
  expect(kind("ТЗ").getByRole("button", { name: "Убрать: Kimi via Fireworks" })).toBeTruthy();
  expect(kind("ТЗ").getByRole("button", { name: "Убрать: gone" })).toBeTruthy();
});
