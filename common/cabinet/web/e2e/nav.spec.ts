// The coordinator's chat is one tap from anywhere; on its page the menu marks it, not "Agents".
// The menu's order, the folded side menu and "Back" on a desktop.
import { expect } from "@playwright/test";
import { api, cardByTitle, executor, getJson, ownerTest as test } from "./helpers";

test("the side menu: the inbox first, the dashboard at /", async ({ page, isMobile }) => {
  test.skip(isMobile, "the side menu is a desktop's");
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Дашборд", level: 1 })).toBeVisible();
  await expect(page.locator("nav.side").getByRole("link")).toHaveText(
    [/Входящие/, /Дашборд/, /BOSS/, /Доска/, /Агенты/, /Серверы/, /Инциденты/, /Лента/, /Настройки/],
  );
});

test("the side menu folds to icons, the page widens, the fold survives a reload", async ({ page, isMobile }) => {
  test.skip(isMobile, "the side menu is a desktop's");
  await page.goto("/board");
  const side = page.locator("nav.side");
  await expect(side.getByRole("button", { name: "Свернуть меню" })).toBeVisible(); // unfolded at first
  const wide = (await side.boundingBox())!.width;
  const before = (await page.locator("main").boundingBox())!.width;
  const badges = await side.locator(".badge").count();
  await side.getByRole("button", { name: "Свернуть меню" }).click();
  await expect(side.getByRole("button", { name: "Развернуть меню" })).toBeVisible();
  expect((await side.boundingBox())!.width).toBeLessThanOrEqual(72);
  expect((await side.boundingBox())!.width).toBeLessThan(wide);
  expect((await page.locator("main").boundingBox())!.width).toBeGreaterThan(before);
  for (const name of ["Входящие", "Дашборд", "BOSS", "Доска", "Агенты", "Серверы", "Инциденты", "Лента", "Настройки"]) {
    await expect(side.getByRole("link", { name })).toBeVisible();
  }
  await expect(side.locator(".badge")).toHaveCount(badges); // the inbox's and BOSS's badges stay on the icons
  await expect(side.getByRole("button", { name: "Выйти" })).toBeVisible();

  await page.reload();
  await expect(side.getByRole("button", { name: "Развернуть меню" })).toBeVisible();
  await side.getByRole("button", { name: "Развернуть меню" }).click();
  await expect(side.getByRole("button", { name: "Свернуть меню" })).toBeVisible();
});

test("«Назад» returns to the filtered board, or to the page's parent in a new tab", async ({ page, context, isMobile }) => {
  test.skip(isMobile, "a phone keeps its own back");
  const back = (p = page) => p.locator("main").getByRole("button", { name: "Назад" });
  // the coordinator's page is its chat, the side menu's BOSS: a top level too
  for (const path of ["/", "/inbox", "/board", "/agents", "/agents/boss", "/feed", "/settings", "/servers", "/incidents"]) {
    await page.goto(path);
    await expect(page.locator("main h1").first()).toBeVisible();
    await expect(back(), path).toHaveCount(0);
  }

  await page.goto("/board?project=digipaddock&status=To%20Do");
  const first = page.locator("a.bc").first();
  await first.click();
  await page.waitForURL(/\/cards\/KD-\d+$/);
  await back().click();
  await page.waitForURL(/\/board\?project=digipaddock&status=To(%20|\+)Do$/);

  const { id } = await cardByTitle(page, "Card page in the cabinet");
  const created = await api(page, "POST", "/api/cards", { title: "Назад с решения из e2e", project: "digipaddock" });
  expect(created.status).toBe(201);
  await executor(page, { op: "decision.open", type: "question", agent: "dev", card: created.body.id, payload: { text: "Назад?" } });
  const decision = (await getJson(page, `/api/decisions?state=open&card=${created.body.id}`))[0].decision;
  for (const [path, parent] of [
    [`/cards/${id}`, "/board"],
    [`/decisions/${decision.id}`, "/inbox"],
    ["/agents/dev", "/agents"],
    ["/incidents/P-333", "/incidents"],
    ["/servers/web-1", "/servers"],
    ["/settings/users", "/settings"],
    ["/settings/projects", "/settings"],
  ]) {
    const tab = await context.newPage();
    await tab.goto(path);
    await back(tab).click();
    await expect(tab).toHaveURL(new RegExp(`${parent.replace("/", "\\/")}$`));
    await tab.close();
  }
});

test("the BOSS item opens the coordinator's page and is the one marked", async ({ page, isMobile }) => {
  await page.goto("/board");
  const nav = page.locator(isMobile ? "nav.tabbar" : "nav.side");
  const boss = nav.getByRole("link", { name: "BOSS" });
  const agents = nav.getByRole("link", { name: "Агенты" });
  await boss.click();
  await page.waitForURL(/\/agents\/boss$/);
  await expect(boss).toHaveAttribute("aria-current", "page");
  await expect(agents).not.toHaveAttribute("aria-current", "page");
  await page.goto("/agents/dev");
  await expect(agents).toHaveAttribute("aria-current", "page");
  await expect(boss).not.toHaveAttribute("aria-current", "page");
});

test("the status reason opens over the card without moving its header or discussion", async ({ page }) => {
  await page.goto("/board");
  const created = await api(page, "POST", "/api/cards", { title: "Модальная смена статуса", project: "digipaddock" });
  expect(created.status).toBe(201);
  for (const width of [360, 1280]) {
    await page.setViewportSize({ width, height: 812 });
    await page.goto(`/cards/${created.body.id}`);
    const trigger = page.getByRole("button", { name: "Сменить статус вручную" });
    await expect(trigger).toBeVisible();
    const before = await page.locator(".card-h").boundingBox();
    const tabs = await page.locator(".cardview .tabs").boundingBox();
    await trigger.click();
    await page.getByRole("option", { name: "Done", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Сменить статус вручную" });
    await expect(dialog).toBeVisible();
    await dialog.evaluate(async el => { await Promise.all(el.getAnimations().map(a => a.finished)); });
    await expect(dialog).toContainText("Inbox");
    await expect(dialog).toContainText("Done");
    await expect(dialog.getByRole("button", { name: "Отправить" })).toBeDisabled();
    expect(await page.locator(".card-h").boundingBox()).toEqual(before);
    expect(await page.locator(".cardview .tabs").boundingBox()).toEqual(tabs);
    const box = (await dialog.boundingBox())!;
    expect(box.width).toBeLessThanOrEqual(Math.min(width, 480));
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height).toBeLessThanOrEqual(812);
    await expect(dialog.getByRole("radio", { name: "Сделано вне системы" })).toBeFocused();
    await page.screenshot({ path: `../../../tmp/cabinet-dev/status-dialog-${width}.png` });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  }
  await page.getByRole("button", { name: "Сменить статус вручную" }).click();
  await page.getByRole("option", { name: "Done", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Сменить статус вручную" });
  await dialog.getByText("Сделано вне системы").click();
  await dialog.getByLabel("Пояснение (необязательно)").fill("Проверено владельцем");
  await dialog.getByRole("button", { name: "Отправить", exact: true }).click();
  await expect(page.getByRole("button", { name: "Сменить статус вручную" })).toContainText("Done");
});

test("the mobile More sheet fits its entries above the tab bar", async ({ page }) => {
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 812 });
    await page.goto("/board");
    await page.locator("nav.tabbar").getByRole("button", { name: "Ещё" }).click();
    const sheet = page.getByRole("dialog", { name: "Ещё" });
    await expect(sheet).toBeVisible();
    // Wait for the sheet's entrance animation before measuring its edges.
    await sheet.evaluate(async el => { await Promise.all(el.getAnimations().map(a => a.finished)); });
    const box = (await sheet.boundingBox())!;
    const links = await sheet.getByRole("link").all();
    // "Add task" is the sheet's first entry, the links follow it
    const first = (await sheet.getByRole("button", { name: "Добавить задачу" }).boundingBox())!;
    const last = (await links[links.length - 1].boundingBox())!;
    const bar = (await page.locator("nav.tabbar").boundingBox())!;
    expect(first.y - box.y).toBeLessThanOrEqual(10);
    expect(box.y + box.height - last.y - last.height).toBeLessThanOrEqual(10);
    expect(Math.abs(box.y + box.height - bar.y)).toBeLessThan(1);
    expect(box.width).toBeLessThanOrEqual(width);
    await page.screenshot({ path: `../../../tmp/cabinet-dev/more-sheet-${width}.png` });
    await page.keyboard.press("Escape");
  }
});

test("Agree posts a question reply and records it in the discussion", async ({ page }) => {
  await page.goto("/board");
  const created = await api(page, "POST", "/api/cards", { title: "Быстрое согласие", project: "digipaddock" });
  expect(created.status).toBe(201);
  await executor(page, { op: "decision.open", type: "question", agent: "dev", card: created.body.id, payload: { text: "Продолжаем работу?" } });
  await page.goto(`/cards/${created.body.id}`);
  const decision = page.locator("article.dec").first();
  await expect(decision.getByRole("button", { name: "Ответить", exact: true })).toBeVisible();
  await decision.getByRole("button", { name: "Согласен", exact: true }).click();
  await expect.poll(async () => (await getJson(page, `/api/decisions?state=all&card=${created.body.id}`))[0].decision.reply).toBe("Согласен");
  await expect(decision).toContainText("Отвечено");
  await expect(page.locator(".thread .msg").filter({ hasText: "Согласен" }).first()).toBeVisible();
});
