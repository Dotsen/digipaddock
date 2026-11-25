// The menu's order, the phone's tab bar with "More", the folded side menu.
import { cleanup, fireEvent, render, within } from "@testing-library/svelte";
import { tick } from "svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { live } from "../lib/live.svelte";
import { newCardForm } from "../lib/newcardform.svelte";
import { navigate, startRouter } from "../lib/router.svelte";
import { session } from "../lib/session.svelte";
import { SIDE_MENU_KEY, sideMenu } from "../lib/sidemenu.svelte";
import Nav from "./Nav.svelte";

function signIn(permissions: Record<string, string[]>) {
  session.signedIn({ id: "owner", name: "Owner", language: "ru", timezone: "Etc/GMT-3", permissions, projects: [],
    csrf: "x", push_public_key: "", second_factor: { passkey: false, totp: false, password: true }, must_setup: false });
}

const names = (nav: HTMLElement) => within(nav).getAllByRole("link").map((a) => a.textContent?.trim());

beforeEach(() => {
  history.replaceState(null, "", "/inbox");
  startRouter();
  localStorage.clear();
  if (sideMenu.collapsed) sideMenu.toggle();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  session.clear();
  newCardForm.reset();
  vi.unstubAllGlobals();
});

it("lists the inbox first, the dashboard, BOSS, then the rest; the tab bar keeps five and «Ещё»", () => {
  signIn({ "*": ["*"] });
  const { container } = render(Nav);
  const side = container.querySelector<HTMLElement>("nav.side")!;
  const tabbar = container.querySelector<HTMLElement>("nav.tabbar")!;
  expect(names(side)).toEqual(["Входящие", "Дашборд", "BOSS", "Доска", "Деплой", "Серверы", "Инциденты", "Агенты", "Лента", "Настройки"]);
  expect(names(tabbar)).toEqual(["Входящие", "Дашборд", "BOSS", "Деплой", "Инциденты"]);
  expect(within(tabbar).getByRole("button", { name: "Ещё" })).toBeTruthy();
});

it("shows the count of incidents not resolved on their item, in the tab bar and the side menu", async () => {
  signIn({ "*": ["*"] });
  live.incidents = 3;
  try {
    const { container } = render(Nav);
    for (const nav of ["nav.tabbar", "nav.side"]) {
      const item = within(container.querySelector<HTMLElement>(nav)!).getByRole("link", { name: /^Инциденты/ });
      expect(within(item).getByLabelText("Инциденты, открытых: 3").textContent).toBe("3");
    }
    live.incidents = 0;
    await tick();
    expect(container.querySelector('[aria-label^="Инциденты, открытых"]')).toBeNull();
  } finally {
    live.incidents = 0;
  }
});

it("puts «Серверы» under «Ещё» on a phone and in the side menu, for who reads the servers only", async () => {
  signIn({ "*": ["cards.read", "servers.read"] });
  const { container } = render(Nav);
  expect(names(container.querySelector<HTMLElement>("nav.side")!)).toContain("Серверы");
  expect(names(container.querySelector<HTMLElement>("nav.tabbar")!)).not.toContain("Серверы");
  await fireEvent.click(within(container.querySelector<HTMLElement>("nav.tabbar")!).getByRole("button", { name: "Ещё" }));
  expect(names(container.ownerDocument.querySelector<HTMLElement>(".more-sheet")!)).toContain("Серверы");
  cleanup();
  signIn({ "*": ["cards.read", "agents.chat"] }); // a partner
  const partner = render(Nav).container;
  expect(names(partner.querySelector<HTMLElement>("nav.side")!)).not.toContain("Серверы");
});

it("puts «Деплой» in the tab bar in place of «Агенты» for who holds deploys.read only", async () => {
  signIn({ "*": ["cards.read", "deploys.read"] });
  const { container } = render(Nav);
  const tabbar = container.querySelector<HTMLElement>("nav.tabbar")!;
  expect(names(container.querySelector<HTMLElement>("nav.side")!)).toContain("Деплой");
  expect(names(tabbar)).toEqual(["Входящие", "Дашборд", "Деплой"]);
  await fireEvent.click(within(tabbar).getByRole("button", { name: "Ещё" }));
  expect(names(container.ownerDocument.querySelector<HTMLElement>(".more-sheet")!)).toContain("Агенты");
  cleanup();
  signIn({ "*": ["cards.read", "servers.read"] }); // no deploys: «Агенты» keep their tab
  const other = render(Nav).container;
  expect(names(other.querySelector<HTMLElement>("nav.side")!)).not.toContain("Деплой");
  expect(names(other.querySelector<HTMLElement>("nav.tabbar")!)).toContain("Агенты");
});

it("has no BOSS item without agents.chat", () => {
  signIn({ "*": ["cards.read"] });
  const { container } = render(Nav);
  expect(names(container.querySelector<HTMLElement>("nav.side")!)).toEqual(["Входящие", "Дашборд", "Доска", "Агенты", "Лента", "Настройки"]);
  expect(names(container.querySelector<HTMLElement>("nav.tabbar")!)).toEqual(["Входящие", "Дашборд", "Агенты"]);
});

it("opens «Ещё» as a sheet that a choice, a tap outside and Esc close", async () => {
  signIn({ "*": ["*"] });
  const { container } = render(Nav);
  const more = within(container.querySelector<HTMLElement>("nav.tabbar")!).getByRole("button", { name: "Ещё" });
  const sheet = () => container.ownerDocument.querySelector<HTMLElement>(".more-sheet");

  await fireEvent.click(more);
  expect(names(sheet()!)).toEqual(["Доска", "Серверы", "Агенты", "Лента", "Настройки"]);
  await fireEvent.click(within(sheet()!).getByRole("link", { name: "Лента" }));
  expect(location.pathname).toBe("/feed");
  expect(sheet()).toBeNull();
  expect(more.getAttribute("aria-current")).toBe("page"); // the feed is under "More"

  await fireEvent.click(more);
  await fireEvent.click(container.ownerDocument.querySelector(".more-veil")!);
  expect(sheet()).toBeNull();

  await fireEvent.click(more);
  await fireEvent.keyDown(window, { key: "Escape" });
  expect(sheet()).toBeNull();

  navigate("/inbox");
  await tick();
  expect(more.getAttribute("aria-current")).toBeNull();
});

it("folds the side menu to icons with names in aria-label, remembered in this browser", async () => {
  signIn({ "*": ["*"] });
  const { container } = render(Nav);
  const side = container.querySelector<HTMLElement>("nav.side")!;
  await fireEvent.click(within(side).getByRole("button", { name: "Свернуть меню" }));
  expect(side.classList.contains("folded")).toBe(true);
  expect(localStorage.getItem(SIDE_MENU_KEY)).toBe("1");
  expect(within(side).getByRole("link", { name: "Входящие" }).textContent?.trim()).toBe("");
  expect(within(side).getByRole("button", { name: "Выйти" })).toBeTruthy();
  await fireEvent.click(within(side).getByRole("button", { name: "Развернуть меню" }));
  expect(side.classList.contains("folded")).toBe(false);
  expect(localStorage.getItem(SIDE_MENU_KEY)).toBeNull();
});

// a task is added from any page — the menu opens the new card's form
const noCards = () => vi.stubGlobal("fetch", vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })));

it("«Добавить задачу» in the side menu opens the new card's form; folded, it is an icon with its name", async () => {
  noCards();
  signIn({ "*": ["*"] });
  const { container } = render(Nav);
  const side = within(container.querySelector<HTMLElement>("nav.side")!);
  expect(side.getByRole("button", { name: "Добавить задачу" }).textContent).toContain("Добавить задачу");
  await fireEvent.click(side.getByRole("button", { name: "Добавить задачу" }));
  expect([newCardForm.open, newCardForm.project]).toEqual([true, ""]);
  newCardForm.close();
  sideMenu.toggle();
  await tick();
  const folded = side.getByRole("button", { name: "Добавить задачу" });
  expect([folded.textContent?.trim(), folded.getAttribute("title")]).toEqual(["", "Добавить задачу"]);
  await fireEvent.click(folded);
  expect(newCardForm.open).toBe(true);
});

it("on the board the form opens on the board's project", async () => {
  noCards();
  signIn({ "*": ["*"] });
  navigate("/board?project=shop");
  const { container } = render(Nav);
  await fireEvent.click(within(container.querySelector<HTMLElement>("nav.side")!).getByRole("button", { name: "Добавить задачу" }));
  expect([newCardForm.open, newCardForm.project]).toEqual([true, "shop"]);
});

it("on a phone «Добавить задачу» is the first entry of «Ещё», which it closes", async () => {
  noCards();
  signIn({ "*": ["*"] });
  const { container } = render(Nav);
  await fireEvent.click(within(container.querySelector<HTMLElement>("nav.tabbar")!).getByRole("button", { name: "Ещё" }));
  const sheet = container.ownerDocument.querySelector<HTMLElement>(".more-sheet")!;
  expect(sheet.firstElementChild?.textContent?.trim()).toBe("Добавить задачу");
  await fireEvent.click(within(sheet).getByRole("button", { name: "Добавить задачу" }));
  expect(container.ownerDocument.querySelector(".more-sheet")).toBeNull();
  expect(newCardForm.open).toBe(true);
});

it("who cannot create a card has no «Добавить задачу»", async () => {
  signIn({ shop: ["cards.read"] });
  const { container } = render(Nav);
  await fireEvent.click(within(container.querySelector<HTMLElement>("nav.tabbar")!).getByRole("button", { name: "Ещё" }));
  expect(within(container.ownerDocument.body).queryByRole("button", { name: "Добавить задачу" })).toBeNull();
});
