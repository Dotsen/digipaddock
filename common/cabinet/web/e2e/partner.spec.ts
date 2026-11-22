// Scenario 4: a partner (anna, project alert) sees her project only — no sysadmin plans in her inbox, no
// other project on the board, and a card of another project is not found (its existence is not shown).
import { expect, test } from "@playwright/test";
import { codes, getJson, loginByCode } from "./helpers";

test("anna sees alert only", async ({ page, browser }, info) => {
  // a digipaddock card's id, read as the owner
  const owner = await browser.newContext({ storageState: `${info.project.metadata.state}/auth/owner.json`, baseURL: info.project.use.baseURL });
  const digipaddock = await (await owner.request.get("/api/cards?project=digipaddock")).json();
  await owner.close();
  expect(digipaddock.length).toBeGreaterThan(0);

  await loginByCode(page, "anna", codes().anna);
  await page.waitForURL(/\/setup$/);
  await page.getByRole("button", { name: "Позже" }).click();
  await page.waitForURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Дашборд" })).toBeVisible();
  await expect(page.getByText(/web-1|nginx|воркер/)).toHaveCount(0);
  // the overview and the agents of a partner hold nothing of other projects, not even numbers
  await expect(page.getByText(/Cache the board's responses|Card history in pages|Build the spend report/)).toHaveCount(0);
  const overview = await getJson(page, "/api/dashboard");
  const rows = [...overview.needs, ...overview.work, ...overview.problems, ...overview.changes];
  expect(rows.length).toBeGreaterThan(0);
  const mine = new Set((await getJson(page, "/api/cards")).map((card: { id: string }) => card.id));
  for (const row of rows) expect(mine.has(row.card), row.card).toBe(true);
  for (const row of [...overview.needs, ...overview.work, ...overview.problems]) expect(row.project).toBe("alert");
  const dev = overview.team.find((member: { name: string }) => member.name === "dev");
  expect(dev.failed).toBe(0); // the seed's failed run is on a digipaddock card
  await page.goto("/agents");
  await expect(page.getByRole("link", { name: "@dev" })).toBeVisible();
  await expect(page.getByText(/сбоев|Cache the board's responses|Card history in pages/)).toHaveCount(0);

  await page.goto("/board");
  // the board's chips carry the projects' names (the cabinet's table)
  await expect(page.getByRole("button", { name: "Alert.Example.Com", exact: true })).toBeVisible();
  for (const other of ["DIGIPADDOCK", "Servers"]) await expect(page.getByRole("button", { name: other, exact: true })).toHaveCount(0);
  await expect(page.locator("a.bc").first()).toBeVisible();
  const seen: { project: string }[] = await getJson(page, "/api/cards");
  expect(new Set(seen.map((card) => card.project))).toEqual(new Set(["alert"]));

  await page.goto(`/cards/${digipaddock[0].id}`);
  await expect(page.getByRole("heading", { name: "Не найдено" })).toBeVisible();
});
