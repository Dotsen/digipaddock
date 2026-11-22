// The new card's form on the board — the key fields, "More", one card after another.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { clearDrafts } from "../lib/drafts";
import { rememberProject, lastProject } from "../lib/newcard";
import { session, type Me } from "../lib/session.svelte";
import { models } from "../lib/stores/models.svelte";
import { projects } from "../lib/stores/projects.svelte";
import type { CardCompact } from "../lib/types";
import NewCard from "./NewCard.svelte";

const me = (permissions: Record<string, string[]>) => ({ id: "owner", name: "Владелец", permissions, projects: [] }) as unknown as Me;
const CARDS = [
  { id: "KD-3", n: 3, title: "Цель", project: "shop", status: "To Do", labels: ["ui", "spec:implementer"] },
  { id: "KD-4", n: 4, title: "Основа", project: "shop", status: "Done", labels: ["night"] },
  { id: "KD-5", n: 5, title: "Отменённая основа", project: "shop", status: "Cancelled", labels: ["old"] },
] as CardCompact[];

let sent: Record<string, unknown>[] = [];
let answer: () => Response;
const created = (id: string, title = "Форма") =>
  new Response(JSON.stringify({ id, title }), { status: 201, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  setLocale("ru");
  // jsdom has no native modal implementation
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
  localStorage.clear();
  session.me = me({ "*": ["*"] });
  models.list = [{ id: "claude:opus", title: "Claude Opus", provider: "subscription", enabled: 1, key: true, experimental: false, builtin: true }];
  projects.list = [
    { id: "shop", name: "Shop", route: "@dev" },
    { id: "web", name: "Web", route: "@owner" },
    { id: "ops", name: "Ops", route: "@sysadmin" },
  ];
  projects.assignees = ["@analyst", "@dev", "@owner"];
  sent = [];
  answer = () => created("KD-7");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit) => {
      sent.push(JSON.parse(String(init.body)));
      return answer();
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  session.me = null;
  models.reset();
  projects.reset();
  clearDrafts();
});

const open = (project = "") => render(NewCard, { project, cards: CARDS, onclose: vi.fn() });
const title = () => screen.getByLabelText("Заголовок") as HTMLInputElement;
const project = () => screen.getByLabelText("Проект") as HTMLSelectElement;
const description = () => screen.getByLabelText("Описание (Markdown)") as HTMLTextAreaElement;
const create = () => screen.getByRole("button", { name: "Создать" }) as HTMLButtonElement;
const type = (field: HTMLElement, value: string) => fireEvent.input(field, { target: { value } });
const more = async () => {
  const details = screen.getByText("Ещё").closest("details")!;
  details.open = true;
  await fireEvent(details, new Event("toggle"));
  return details;
};
// the lists with search — the field, what its open list offers, a pick from it
const picker = (name: string) => screen.getByRole("combobox", { name }) as HTMLInputElement;
const find = async (name: string, text = "") => {
  await fireEvent.focus(picker(name));
  await type(picker(name), text);
  return within(screen.getByRole("listbox", { name })).queryAllByRole("option").map((o) => o.textContent?.trim());
};
const choose = async (name: string, text: string, option: string) => {
  await find(name, text);
  await fireEvent.click(within(screen.getByRole("listbox", { name })).getByRole("option", { name: option }));
};

it("the key fields: the title, the project last used by this person, the description", () => {
  rememberProject("owner", "web");
  open("shop");
  expect(project().value).toBe("web");
  expect([...project().options].map((o) => o.text)).toEqual(["Shop", "Web", "Ops"]);
  expect([title().value, description().value]).toEqual(["", ""]);
  expect(create().disabled).toBe(true);
});

it("a remembered project the person cannot create in gives way to the board's, then to the first", () => {
  session.me = me({ shop: ["cards.create"], ops: ["cards.create"] });
  rememberProject("owner", "web");
  open("ops");
  expect([...project().options].map((o) => o.value)).toEqual(["shop", "ops"]);
  expect(project().value).toBe("ops");
  cleanup();
  open("web");
  expect(project().value).toBe("shop");
});

it("a created card leaves the form open for the next one", async () => {
  open("shop");
  await type(title(), "  Первая ");
  await type(description(), "Зачем **это**");
  await fireEvent.click(create());
  const notice = await screen.findByRole("status");
  expect(sent).toEqual([{ title: "Первая", project: "shop", description: "Зачем **это**" }]);
  expect(within(notice).getByRole("link", { name: /KD-7/ }).getAttribute("href")).toBe("/cards/KD-7");
  expect([title().value, description().value, project().value]).toEqual(["", "", "shop"]);
  expect(document.activeElement).toBe(title());
  expect(lastProject("owner")).toBe("shop");
  // the next card, and the notice names it
  answer = () => created("KD-8", "Вторая");
  await type(title(), "Вторая");
  await fireEvent.click(create());
  await vi.waitFor(() => expect(screen.getByRole("status").textContent).toContain("KD-8"));
  expect(sent[1]).toEqual({ title: "Вторая", project: "shop" });
});

it("the notice's link closes the form", async () => {
  const onclose = vi.fn();
  render(NewCard, { project: "shop", cards: CARDS, onclose });
  await type(title(), "Первая");
  await fireEvent.click(create());
  await fireEvent.click(within(await screen.findByRole("status")).getByRole("link"));
  expect(onclose).toHaveBeenCalled();
});

it.each([{ ctrlKey: true }, { metaKey: true }])("Ctrl+Enter and Cmd+Enter create: %o", async (mod) => {
  open("shop");
  await type(title(), "Из описания");
  await type(description(), "Текст");
  await fireEvent.keyDown(description(), { key: "Enter", ...mod });
  await screen.findByRole("status");
  expect(sent).toEqual([{ title: "Из описания", project: "shop", description: "Текст" }]);
});

it("Enter alone in the description is a new line, and nothing is sent without a title", async () => {
  open("shop");
  await type(description(), "Текст");
  await fireEvent.keyDown(description(), { key: "Enter" });
  await fireEvent.keyDown(description(), { key: "Enter", ctrlKey: true });
  expect(sent).toEqual([]);
});

it("one card per submit", async () => {
  let release = (_: Response) => {};
  const held = new Promise<Response>((resolve) => (release = resolve));
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
    sent.push(JSON.parse(String(init.body)));
    return held;
  }));
  open("shop");
  await type(title(), "Одна");
  await fireEvent.keyDown(title(), { key: "Enter", ctrlKey: true });
  await fireEvent.keyDown(title(), { key: "Enter", ctrlKey: true });
  await fireEvent.submit(title().closest("form")!);
  expect(create().disabled).toBe(true);
  release(created("KD-7"));
  await screen.findByRole("status");
  expect(sent).toHaveLength(1);
});

it("«Ещё» sends the assignee, labels, criteria, parent and dependencies", async () => {
  open("shop");
  const extra = within(await more());
  await type(title(), "Полная");
  await fireEvent.click(extra.getByRole("button", { name: "Исполнитель" }));
  expect(within(screen.getByRole("listbox", { name: "Исполнитель" })).getAllByRole("option").map((o) => o.textContent?.trim())).toEqual([
    "по маршруту (@dev)", "никто", "@analyst", "@dev", "@owner",
  ]);
  await fireEvent.click(screen.getByRole("option", { name: "@analyst" }));
  // the labels of the board's cards (a cancelled card's are not offered), and a new one from the search
  expect(await find("Метки")).toEqual(["night", "spec:implementer", "ui"]);
  await choose("Метки", "", "ui");
  await choose("Метки", "impl", "spec:implementer");
  await choose("Метки", " срочно ", "Добавить «срочно»");
  const criterion = extra.getByLabelText("Добавить критерий");
  await type(criterion, "Открывается");
  await fireEvent.keyDown(criterion, { key: "Enter" });
  await type(criterion, "Не теряется"); // typed, not added: it goes with the card all the same
  // a card is found by its number or its title, the newest first; a cancelled one is not offered
  expect(await find("Родитель")).toEqual(["KD-4 · Основа", "KD-3 · Цель"]);
  expect(await find("Родитель", "осн")).toEqual(["KD-4 · Основа"]);
  await choose("Родитель", "осн", "KD-4 · Основа");
  await choose("Родитель", "kd-3", "KD-3 · Цель"); // one parent: the next pick replaces it
  await choose("Зависит от", "основа", "KD-4 · Основа");
  expect(extra.queryByLabelText("Веха")).toBeNull(); // milestones are gone
  expect(sent).toEqual([]);
  await fireEvent.click(create());
  await screen.findByRole("status");
  expect(sent).toEqual([{
    title: "Полная",
    project: "shop",
    assignee: "@analyst",
    labels: ["ui", "spec:implementer", "срочно"],
    acceptance: [{ text: "Открывается", checked: false }, { text: "Не теряется", checked: false }],
    parent: "KD-3",
    dependencies: ["KD-4"],
  }]);
  // the criteria are the card's own text and go with it; the rest stays for the next card
  await type(title(), "Следующая");
  await fireEvent.click(create());
  await vi.waitFor(() => expect(sent).toHaveLength(2));
  expect(sent[1]).toEqual({
    title: "Следующая", project: "shop", assignee: "@analyst", labels: ["ui", "spec:implementer", "срочно"],
    parent: "KD-3", dependencies: ["KD-4"],
  });
});

it("the labels, the parent and the dependencies are lists with search, not fields to type into", async () => {
  open("shop");
  const extra = within(await more());
  const searches = extra.getAllByRole("combobox").filter((field) => field.tagName === "INPUT");
  expect(searches.map((field) => field.getAttribute("aria-label"))).toEqual(["Метки", "Родитель", "Зависит от"]);
  expect(extra.queryByLabelText("Метки через запятую")).toBeNull();
  expect(extra.queryByLabelText("Добавить зависимость")).toBeNull();
});

it("a criterion, a label, the parent and a dependency are taken back", async () => {
  open("shop");
  const extra = within(await more());
  await type(title(), "Правка");
  for (const text of ["Первый", "Второй"]) {
    await type(extra.getByLabelText("Добавить критерий"), text);
    await fireEvent.keyDown(extra.getByLabelText("Добавить критерий"), { key: "Enter" });
  }
  await fireEvent.click(extra.getAllByRole("button", { name: "Убрать критерий" })[0]);
  await choose("Метки", "", "ui");
  await fireEvent.click(extra.getByRole("button", { name: "Убрать: ui" }));
  await choose("Родитель", "", "KD-3 · Цель");
  await fireEvent.click(extra.getByRole("button", { name: "Убрать: KD-3 · Цель" }));
  await choose("Зависит от", "", "KD-3 · Цель");
  await choose("Зависит от", "", "KD-4 · Основа");
  await fireEvent.click(extra.getByRole("button", { name: "Убрать: KD-3 · Цель" }));
  await fireEvent.click(create());
  await screen.findByRole("status");
  expect(sent[0]).toEqual({
    title: "Правка", project: "shop", acceptance: [{ text: "Второй", checked: false }], dependencies: ["KD-4"],
  });
});

it("the model, the effort and the council are there only with settings", async () => {
  session.me = me({ shop: ["cards.create"], web: ["cards.create", "settings"] });
  open("shop");
  const extra = within(await more());
  expect(extra.queryByRole("button", { name: "Модель" })).toBeNull();
  expect(extra.queryByRole("button", { name: "Усилие" })).toBeNull();
  expect(extra.queryByRole("button", { name: "Совет моделей" })).toBeNull();
  await fireEvent.change(project(), { target: { value: "web" } });
  await fireEvent.click(extra.getByRole("button", { name: "Модель" }));
  await fireEvent.click(screen.getByRole("option", { name: /Claude Opus/ }));
  await fireEvent.click(extra.getByRole("button", { name: "Усилие" }));
  await fireEvent.click(screen.getByRole("option", { name: "high" }));
  await type(title(), "С моделью");
  await fireEvent.click(create());
  await screen.findByRole("status");
  expect(sent[0]).toEqual({ title: "С моделью", project: "web", model: "claude:opus", effort: "high" });
  // back on a project without the right: what was chosen is not sent
  await fireEvent.change(project(), { target: { value: "shop" } });
  await type(title(), "Без права");
  await fireEvent.click(create());
  await vi.waitFor(() => expect(sent).toHaveLength(2));
  expect(sent[1]).toEqual({ title: "Без права", project: "shop" });
});

it("the council chosen in its editor goes with the card", async () => {
  open("shop");
  const extra = within(await more());
  await fireEvent.click(extra.getByRole("button", { name: "Совет моделей" }));
  const editor = within(screen.getByRole("dialog", { name: "Совет моделей" }));
  const brief = within(editor.getByRole("group", { name: "ТЗ" }));
  // no mode to choose first — the models are picked at once, and one may be picked twice
  for (const _ of [1, 2]) {
    await fireEvent.focus(brief.getByRole("combobox", { name: "Критики" }));
    await fireEvent.click(brief.getByRole("option", { name: /Claude Opus/ }));
  }
  await fireEvent.click(editor.getByRole("button", { name: "Сохранить" }));
  expect(screen.queryByRole("dialog", { name: "Совет моделей" })).toBeNull();
  await type(title(), "С советом");
  await fireEvent.click(create());
  await screen.findByRole("status");
  expect(sent[0]).toEqual({
    title: "С советом", project: "shop", council: { brief: { critics: ["claude:opus", "claude:opus"], rounds: 3 } },
  });
});

it("a refusal is shown and nothing is lost", async () => {
  answer = () => new Response(JSON.stringify({ error: "refused", message: "no" }), { status: 403, headers: { "Content-Type": "application/json" } });
  open("shop");
  await type(title(), "Откажут");
  await type(description(), "Текст");
  await fireEvent.click(create());
  expect((await screen.findByRole("alert")).textContent).toBeTruthy();
  expect(screen.queryByRole("status")).toBeNull();
  expect([title().value, description().value]).toEqual(["Откажут", "Текст"]);
  expect(create().disabled).toBe(false);
  answer = () => created("KD-9");
  await fireEvent.click(create());
  await screen.findByRole("status");
  expect(screen.queryByRole("alert")).toBeNull();
});

it("another project resets the assignee to its route", async () => {
  open("shop");
  const extra = within(await more());
  await fireEvent.click(extra.getByRole("button", { name: "Исполнитель" }));
  await fireEvent.click(screen.getByRole("option", { name: "@analyst" }));
  await fireEvent.change(project(), { target: { value: "ops" } });
  expect(extra.getByRole("button", { name: "Исполнитель" }).textContent).toContain("по маршруту (@sysadmin)");
  await type(title(), "Другая");
  await fireEvent.click(create());
  await screen.findByRole("status");
  expect(sent[0]).toEqual({ title: "Другая", project: "ops" });
});

// the final review: a sheet opened over the form (the council's editor) is inside the form's element, so the
// browser counts its fields as the form's — Enter in them must not create the card with the council as it was
it("Enter and Ctrl+Enter in the council's editor create nothing", async () => {
  open("shop");
  const extra = within(await more());
  await type(title(), "С советом");
  await fireEvent.click(extra.getByRole("button", { name: "Совет моделей" }));
  const brief = within(within(screen.getByRole("dialog", { name: "Совет моделей" })).getByRole("group", { name: "ТЗ" }));
  await fireEvent.focus(brief.getByRole("combobox", { name: "Критики" }));
  await fireEvent.click(brief.getByRole("option", { name: /Claude Opus/ }));
  const rounds = brief.getByLabelText("Кругов");
  rounds.focus();
  await fireEvent.keyDown(rounds, { key: "Enter", ctrlKey: true });
  await fireEvent.submit(title().closest("form")!); // the browser's implicit submission from the sheet's field
  expect(sent).toEqual([]);
  expect(screen.getByRole("dialog", { name: "Совет моделей" })).toBeTruthy();
});

it("Enter in a list's search picks, it does not create the card; Ctrl+Enter creates", async () => {
  open("shop");
  await more();
  await type(title(), "Метки");
  for (const name of ["Метки", "Родитель", "Зависит от"]) {
    expect(await fireEvent.keyDown(picker(name), { key: "Enter" })).toBe(false); // the default is off
  }
  await find("Метки", "ui");
  await fireEvent.keyDown(picker("Метки"), { key: "Enter" }); // the first match
  expect(sent).toEqual([]);
  await fireEvent.keyDown(picker("Метки"), { key: "Enter", ctrlKey: true });
  await screen.findByRole("status");
  expect(sent).toEqual([{ title: "Метки", project: "shop", labels: ["ui"] }]);
});

it("the notice of the last card goes when the next one is refused", async () => {
  open("shop");
  await type(title(), "Первая");
  await fireEvent.click(create());
  await screen.findByRole("status");
  answer = () => new Response(JSON.stringify({ error: "refused", message: "no" }), { status: 403, headers: { "Content-Type": "application/json" } });
  await type(title(), "Вторая");
  await fireEvent.click(create());
  await screen.findByRole("alert");
  expect(screen.queryByRole("status")).toBeNull();
});

it("unsent text survives closing the form", async () => {
  open("shop");
  await more();
  await type(title(), "Не дописал");
  await type(description(), "Длинный текст");
  await choose("Метки", "", "ui");
  await fireEvent.change(project(), { target: { value: "web" } });
  cleanup(); // Escape, the backdrop, the cross
  open("shop");
  expect([title().value, description().value, project().value]).toEqual(["Не дописал", "Длинный текст", "web"]);
  const details = screen.getByText("Ещё").closest("details")!;
  expect(details.open).toBe(true); // what "More" holds is in view, not sent unseen
  expect(within(details).getByRole("button", { name: "Убрать: ui" })).toBeTruthy();
});

it("a form closed after its card was created opens clean", async () => {
  open("shop");
  await more();
  await choose("Метки", "", "ui");
  await type(title(), "Готово");
  await fireEvent.click(create());
  await screen.findByRole("status");
  cleanup();
  open("shop");
  const details = screen.getByText("Ещё").closest("details")!;
  expect([title().value, details.open]).toEqual(["", false]);
  expect(within(details).queryByRole("button", { name: "Убрать: ui" })).toBeNull();
});

it("a card just created is offered as the parent and a dependency of the next one", async () => {
  open("shop");
  answer = () => created("KD-7", "Основа плана");
  await type(title(), "Основа плана");
  await fireEvent.click(create());
  await screen.findByRole("status");
  await more();
  expect(await find("Родитель", "основа плана")).toEqual(["KD-7 · Основа плана"]);
  await choose("Зависит от", "kd-7", "KD-7 · Основа плана");
  answer = () => created("KD-8", "Вторая");
  await type(title(), "Вторая");
  await fireEvent.click(create());
  await vi.waitFor(() => expect(sent).toHaveLength(2));
  expect(sent[1]).toMatchObject({ title: "Вторая", dependencies: ["KD-7"] });
});
