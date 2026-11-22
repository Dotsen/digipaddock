// a list with search — what a form picks labels, cards and a council's models from.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import Picker from "./Picker.svelte";

const options = [
  { value: "a", text: "Альфа первая" },
  { value: "b", text: "Бета" },
  { value: "g", text: "Гамма первая" },
];

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

const field = () => screen.getByRole("combobox", { name: "Буквы" }) as HTMLInputElement;
const rows = () => screen.queryAllByRole("option").map((o) => o.textContent?.trim());
const mount = (props: Record<string, unknown> = {}) => {
  const onchange = vi.fn();
  render(Picker, { label: "Буквы", options, value: [], onchange, ...props });
  return onchange;
};

it("the list opens on the field and typing filters it by every word", async () => {
  mount();
  expect(screen.queryByRole("listbox")).toBeNull();
  await fireEvent.focus(field());
  expect(rows()).toEqual(["Альфа первая", "Бета", "Гамма первая"]);
  await fireEvent.input(field(), { target: { value: "перв АЛ" } });
  expect(rows()).toEqual(["Альфа первая"]);
  await fireEvent.input(field(), { target: { value: "zzz" } });
  expect(rows()).toEqual([]);
  expect(screen.getByRole("listbox", { name: "Буквы" }).textContent).toContain("Ничего не найдено");
});

it("a single pick replaces the choice and closes the list", async () => {
  const onchange = mount({ value: ["a"] });
  await fireEvent.focus(field());
  await fireEvent.click(screen.getByRole("option", { name: "Бета" }));
  expect(onchange).toHaveBeenCalledWith(["b"]);
  expect(screen.queryByRole("listbox")).toBeNull();
});

it("multiple: a pick adds, a pick of a chosen one removes, the list stays open", async () => {
  const onchange = mount({ multiple: true, value: ["a"] });
  await fireEvent.focus(field());
  expect(screen.getByRole("option", { name: "Альфа первая" }).getAttribute("aria-selected")).toBe("true");
  await fireEvent.click(screen.getByRole("option", { name: "Бета" }));
  expect(onchange).toHaveBeenLastCalledWith(["a", "b"]);
  await fireEvent.click(screen.getByRole("option", { name: "Альфа первая" }));
  expect(onchange).toHaveBeenLastCalledWith([]);
  expect(screen.getByRole("listbox")).toBeTruthy();
});

it("repeat: the same option is added again, up to the most allowed", async () => {
  const onchange = mount({ repeat: true, max: 2, value: ["a"] });
  await fireEvent.focus(field());
  await fireEvent.click(screen.getByRole("option", { name: "Альфа первая" }));
  expect(onchange).toHaveBeenLastCalledWith(["a", "a"]);
  cleanup();
  mount({ repeat: true, max: 2, value: ["a", "a"] });
  expect(field().disabled).toBe(true);
});

it("create: a typed text that is no option is offered and added trimmed", async () => {
  const onchange = mount({ multiple: true, create: true, value: ["a"] });
  await fireEvent.focus(field());
  await fireEvent.input(field(), { target: { value: "  новое " } });
  await fireEvent.click(screen.getByRole("option", { name: "Добавить «новое»" }));
  expect(onchange).toHaveBeenLastCalledWith(["a", "новое"]);
  expect(field().value).toBe("");
  await fireEvent.input(field(), { target: { value: "b" } }); // an option's own value: it is picked, not made
  expect(screen.queryByRole("option", { name: "Добавить «b»" })).toBeNull();
});

it("the chosen are chips, and a chip's × removes exactly that one", async () => {
  const onchange = mount({ repeat: true, value: ["a", "b", "a", "gone"] });
  const chips = screen.getAllByRole("listitem");
  expect(chips.map((c) => c.textContent?.replace("×", "").trim())).toEqual(["Альфа первая", "Бета", "Альфа первая", "gone"]);
  await fireEvent.click(within(chips[2]).getByRole("button", { name: "Убрать: Альфа первая" }));
  expect(onchange).toHaveBeenCalledWith(["a", "b", "gone"]);
});

it("a chosen value no option names is shown as the form says (a model disabled since)", () => {
  mount({ value: ["old"], chosenText: (value: string) => `Модель ${value}` });
  expect(screen.getByRole("button", { name: "Убрать: Модель old" })).toBeTruthy();
});

it("Enter picks the first match and is not the form's submit; with Ctrl it is the form's", async () => {
  const onchange = mount({ multiple: true });
  await fireEvent.focus(field());
  await fireEvent.input(field(), { target: { value: "бе" } });
  expect(await fireEvent.keyDown(field(), { key: "Enter" })).toBe(false);
  expect(onchange).toHaveBeenCalledWith(["b"]);
  onchange.mockClear();
  await fireEvent.input(field(), { target: { value: "га" } });
  expect(await fireEvent.keyDown(field(), { key: "Enter", ctrlKey: true })).toBe(true);
  expect(onchange).not.toHaveBeenCalled();
  await fireEvent.input(field(), { target: { value: "" } }); // nothing typed: Enter picks nothing, and submits nothing
  expect(await fireEvent.keyDown(field(), { key: "Enter" })).toBe(false);
  expect(onchange).not.toHaveBeenCalled();
});

it("the arrows move over the list and Enter picks the row they stand on", async () => {
  const onchange = mount();
  await fireEvent.focus(field());
  await fireEvent.keyDown(field(), { key: "ArrowDown" });
  await fireEvent.keyDown(field(), { key: "ArrowDown" });
  await fireEvent.keyDown(field(), { key: "ArrowUp" });
  const active = field().getAttribute("aria-activedescendant");
  expect(active && document.getElementById(active)?.textContent?.trim()).toBe("Альфа первая");
  await fireEvent.keyDown(field(), { key: "Enter" });
  expect(onchange).toHaveBeenCalledWith(["a"]);
});

it("Escape closes the open list and goes no further; closed, it is not taken", async () => {
  mount();
  const outer = vi.fn();
  document.body.addEventListener("keydown", outer);
  await fireEvent.focus(field());
  expect(await fireEvent.keyDown(field(), { key: "Escape" })).toBe(false);
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(outer).not.toHaveBeenCalled();
  expect(await fireEvent.keyDown(field(), { key: "Escape" })).toBe(true);
  expect(outer).toHaveBeenCalledTimes(1);
  document.body.removeEventListener("keydown", outer);
});

it("a click outside closes the list", async () => {
  mount();
  await fireEvent.focus(field());
  await fireEvent.mouseDown(document.body);
  expect(screen.queryByRole("listbox")).toBeNull();
});

it("more matches than the list shows are cut with a hint", async () => {
  const many = Array.from({ length: 40 }, (_, i) => ({ value: `v${i}`, text: `Карточка ${i}` }));
  mount({ options: many });
  await fireEvent.focus(field());
  expect(rows()).toHaveLength(30);
  expect(screen.getByRole("listbox").textContent).toContain("Показаны первые 30");
});

it("the pick that fills the most allowed closes the list: nothing is added past it", async () => {
  const onchange = mount({ repeat: true, max: 2, value: ["a"] });
  await fireEvent.focus(field());
  await fireEvent.click(screen.getByRole("option", { name: "Бета" }));
  expect(onchange).toHaveBeenLastCalledWith(["a", "b"]);
  expect(screen.queryByRole("listbox")).toBeNull();
});
