// Checklists of a card: an AC item kept after a reload with its counter, a DoD item that rewrites only its
// own line, and a stale tab refused with the «card changed» toast and a fresh read. The counters sit in the
// header's secondary facts ("More").
import { expect } from "@playwright/test";
import { cardByTitle, getJson, ownerTest as test } from "./helpers";

const TITLE = "Card page in the cabinet";

test("AC and DoD items tick, persist and refuse a stale revision", async ({ page, context }) => {
  await page.goto("/board");
  const { id } = await cardByTitle(page, TITLE);
  await page.goto(`/cards/${id}`);
  await page.getByRole("button", { name: "Ещё" }).click();
  await page.getByRole("tab", { name: "ТЗ" }).click();
  const ac = () => page.getByRole("group", { name: "Критерии приёмки" }).getByRole("checkbox");
  await expect(page.getByText("AC 1/3")).toBeVisible();

  await ac().nth(1).click();
  await expect(page.getByText("AC 2/3")).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Ещё" }).click();
  await page.getByRole("tab", { name: "ТЗ" }).click();
  await expect(ac().nth(1)).toBeChecked();
  await expect(page.getByText("AC 2/3")).toBeVisible();

  const before = (await getJson(page, `/api/cards/${id}`)).dod.split("\n");
  await page.getByRole("group", { name: /DoD/ }).getByRole("checkbox").first().click();
  await expect(page.getByText("DoD 2/3")).toBeVisible();
  const after = (await getJson(page, `/api/cards/${id}`)).dod.split("\n");
  expect(after).toHaveLength(before.length);
  const changed = after.flatMap((line: string, i: number) => (line === before[i] ? [] : [i]));
  expect(changed).toEqual([0]);
  expect(after[0]).toMatch(/^- \[x\] #1 /);

  // a second tab that hears nothing keeps an old revision
  const stale = await context.newPage();
  await stale.routeWebSocket(/\/ws$/, () => {});
  await stale.goto(`/cards/${id}`);
  await stale.getByRole("button", { name: "Ещё" }).click();
  await stale.getByRole("tab", { name: "ТЗ" }).click();
  await expect(stale.getByText("AC 2/3")).toBeVisible();
  await ac().nth(2).click();
  await expect(page.getByText("AC 3/3")).toBeVisible();
  await stale.getByRole("group", { name: "Критерии приёмки" }).getByRole("checkbox").nth(0).click();
  await expect(stale.getByText("Карточка изменилась — обновите")).toBeVisible();
  await expect(stale.getByText("AC 3/3")).toBeVisible();
  await expect(stale.getByRole("group", { name: "Критерии приёмки" }).getByRole("checkbox").nth(0)).toBeChecked();
});
