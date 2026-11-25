// Scenario 2: a card from the board's form — Inbox, then To Do by "Start @dev"; a comment; a message that
// arrives over the live channel; the description edited, and an edit over an old revision refused.
import { expect } from "@playwright/test";
import { api, getJson, ownerTest as test } from "./helpers";

// One status leaves its cards in one column, the status in the address; "all" brings the
// rest back; Done keeps its 7-day switch; a desktop's cards carry no status pill (the column names it)
test("the status filter leaves one column", async ({ page }) => {
  await page.goto("/board");
  const all = (await getJson(page, "/api/cards")) as { id: string; status: string }[];
  await expect(page.locator("a.bc").first()).toBeVisible();
  await expect(page.locator("a.bc .pill").first()).toBeHidden();

  // the filters are one "Filters" menu at any width; four of them — milestones are gone
  for (const name of ["Статус", "Исполнитель", "Специализация", "Метка"]) await expect(page.getByLabel(name, { exact: true })).toBeHidden();
  await expect(page.getByLabel("Веха", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: /^Фильтры/ }).click();
  await page.getByLabel("Статус", { exact: true }).selectOption("Review");
  await page.waitForURL(/[?&]status=Review/);
  await expect(page.locator(".col")).toHaveCount(1);
  await expect(page.locator(".col h3")).toContainText("Review");
  await expect(page.locator("a.bc")).toHaveCount(all.filter((c) => c.status === "Review").length);

  await page.getByLabel("Статус", { exact: true }).selectOption("");
  await expect(page).not.toHaveURL(/status=/);
  await expect(page.locator(".col")).toHaveCount(6);

  // a filtered board is a link: it opens with its filters in view
  await page.goto("/board?status=Done");
  await expect(page.getByLabel("Статус", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Фильтры/ })).toHaveText(/Фильтры\s*1/);
  await expect(page.locator(".col")).toHaveCount(1);
  await expect(page.locator("a.bc").first()).toBeVisible(); // loaded: the switch shows only over the cards
  const done = all.filter((c) => c.status === "Done").length;
  const toggle = page.locator(".col h3 .more");
  if (await toggle.count()) {
    await toggle.click();
    await expect(page.locator("a.bc")).toHaveCount(done);
  } else {
    await expect(page.locator("a.bc")).toHaveCount(done); // all of them finished within the week
  }
});

test("an owner cancels a card, finds it through the status filter and restores it", async ({ page }) => {
  await page.goto("/board");
  const created = await api(page, "POST", "/api/cards", { title: "E2E: cancelled card", project: "digipaddock" });
  expect(created.status).toBe(201);
  const id = created.body.id;
  await page.goto(`/cards/${id}`);
  await page.getByRole("button", { name: "Сменить статус вручную" }).click();
  await page.getByRole("option", { name: "Cancelled", exact: true }).click();
  await page.getByRole("radio", { name: "Уже не актуально" }).check(); // a ready reason of the target status
  await page.getByRole("button", { name: "Отправить", exact: true }).first().click();
  await expect.poll(async () => (await getJson(page, `/api/cards/${id}`)).status).toBe("Cancelled");
  expect((await getJson(page, `/api/cards/${id}`)).outcome).toBe("cancelled");

  await page.goto("/board");
  await expect(page.locator("a.bc").first()).toBeVisible();
  await expect(page.locator(`a.bc[href='/cards/${id}']`)).toHaveCount(0);
  await page.getByRole("button", { name: /^Фильтры/ }).click();
  await page.getByLabel("Статус", { exact: true }).selectOption("Done");
  await expect(page.locator(`a.bc[href='/cards/${id}']`)).toHaveCount(0);
  await page.getByLabel("Статус", { exact: true }).selectOption("Cancelled");
  await page.waitForURL(/[?&]status=Cancelled/);
  await expect(page.locator(".col")).toHaveCount(1);
  await expect(page.locator(`a.bc[href='/cards/${id}']`)).toBeVisible();
  await page.reload();
  await expect(page.locator(`a.bc[href='/cards/${id}']`)).toBeVisible();

  await page.locator(`a.bc[href='/cards/${id}']`).click();
  await page.getByRole("button", { name: "Вернуть во Входящие", exact: true }).click();
  await page.getByRole("button", { name: "Отправить", exact: true }).first().click();
  await expect.poll(async () => (await getJson(page, `/api/cards/${id}`)).status).toBe("Inbox");
});

test("a new card goes Inbox → To Do, takes a comment, a live message and a description", async ({ page }) => {
  await page.goto("/board?project=digipaddock");
  // "Add" opens the form; a created card leaves it open for the next one. The button is
  // in the page's header, apart from the search and the filters
  await expect(page.locator(".tools").getByRole("button", { name: "Добавить", exact: true })).toHaveCount(0);
  await page.locator(".topbar").getByRole("button", { name: "Добавить", exact: true }).click();
  const form = page.getByRole("dialog", { name: "Новая карточка" });
  await expect(form.getByLabel("Заголовок")).toBeFocused();
  await expect(form.getByLabel("Проект")).toHaveValue("digipaddock");
  await form.getByLabel("Заголовок").fill("E2E: первая из формы");
  await form.getByLabel("Описание (Markdown)").fill("Одна за другой");
  await form.getByLabel("Описание (Markdown)").press("Control+Enter");
  await expect(form.getByRole("status")).toContainText("E2E: первая из формы");
  await expect(form.getByLabel("Заголовок")).toHaveValue("");
  await expect(form.getByLabel("Описание (Markdown)")).toHaveValue("");
  await form.getByText("Ещё", { exact: true }).click();
  // lists with search — a new label from what was typed, the parent found by its title
  await form.getByRole("combobox", { name: "Метки" }).fill("e2e-form");
  await form.getByRole("option", { name: "Добавить «e2e-form»" }).click();
  await form.getByRole("combobox", { name: "Родитель" }).fill("card page in");
  await form.getByRole("option", { name: /Card page in the cabinet/ }).click();
  await expect(form.getByRole("listbox", { name: "Родитель" })).toBeHidden();
  await form.getByLabel("Добавить критерий").fill("Открывается с доски");
  await form.getByLabel("Заголовок").fill("E2E: карточка с доски");
  await form.getByRole("button", { name: "Создать", exact: true }).click();
  await expect(form.getByRole("status")).toContainText("E2E: карточка с доски");
  await form.getByRole("status").getByRole("link").click();
  await page.waitForURL(/\/cards\/KD-\d+$/);
  await expect(form).toBeHidden();
  const id = page.url().split("/").pop()!;
  const made = await getJson(page, `/api/cards/${id}`);
  expect([made.labels, made.acceptance.map((a: { text: string }) => a.text)]).toEqual([["e2e-form"], ["Открывается с доски"]]);
  const cards = (await getJson(page, "/api/cards")) as { id: string; title: string }[];
  expect(made.parent).toBe(cards.find((c) => c.title === "Card page in the cabinet")!.id);
  await expect(page.getByRole("heading", { name: /E2E: карточка с доски/ })).toBeVisible();
  expect((await getJson(page, `/api/cards/${id}`)).status).toBe("Inbox");

  // the card's own action; the status chip is only a manual override with a reason
  await page.getByRole("button", { name: /^Запустить @/ }).click();
  await expect.poll(async () => (await getJson(page, `/api/cards/${id}`)).status).toBe("To Do");

  await page.getByLabel("Сообщение…").fill("Комментарий из e2e");
  await page.getByRole("button", { name: "Отправить" }).click();
  await expect(page.locator(".thread").getByText("Комментарий из e2e")).toBeVisible();

  // written elsewhere: it comes over the live channel, no reload
  const sent = await api(page, "POST", `/api/cards/${id}/messages`, { body: "Сообщение из другого окна" });
  expect(sent.status).toBe(200);
  await expect(page.locator(".thread").getByText("Сообщение из другого окна")).toBeVisible();

  await page.getByRole("tab", { name: "ТЗ" }).click();
  await page.getByRole("button", { name: "Править" }).click();
  await page.getByLabel("Описание и AC", { exact: true }).fill("Описание из **e2e**");
  await page.getByRole("button", { name: "Сохранить" }).click();
  await expect(page.locator(".view").getByText("Описание из")).toBeVisible();

  // an edit over an old revision is refused
  const card = await getJson(page, `/api/cards/${id}`);
  const stale = await api(page, "PATCH", `/api/cards/${id}`, { revision: card.revision - 1, changes: { description: "старое" } });
  expect(stale.status).toBe(409);
  expect(stale.body.error).toBe("conflict");

  // and the editor says so when the card changed under a draft
  await page.getByRole("button", { name: "Править" }).click();
  await page.getByLabel("Описание и AC", { exact: true }).fill("Мой черновик");
  const other = await api(page, "PATCH", `/api/cards/${id}`, { revision: card.revision, changes: { description: "Чужая правка" } });
  expect(other.status).toBe(200);
  await expect(page.getByText("Описание изменилось, пока вы его правили")).toBeVisible();
  await expect(page.getByLabel("Описание и AC", { exact: true })).toHaveValue("Мой черновик");
});

// A task is added from any page — the menu's "Add task"; the form reads the cards for its lists
// itself, the board need not be open
test("the menu's «Добавить задачу» opens the new card's form away from the board", async ({ page }) => {
  await page.goto("/inbox");
  await page.locator("nav.side").getByRole("button", { name: "Добавить задачу" }).click();
  const form = page.getByRole("dialog", { name: "Новая карточка" });
  await expect(form.getByLabel("Заголовок")).toBeFocused();
  await form.getByText("Ещё", { exact: true }).click();
  await form.getByRole("combobox", { name: "Зависит от" }).fill("nightly");
  await expect(form.getByRole("option", { name: /Nightly backup check/ })).toBeVisible();
  await page.keyboard.press("Escape"); // the list's own
  await expect(form.getByRole("listbox", { name: "Зависит от" })).toBeHidden();
  await expect(form).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(form).toBeHidden();
  await expect(page).toHaveURL(/\/inbox$/);
});

// The menu's search: a card's number opens that card, words open the board searched; the board searches as one
// types. A card put off until a time is shaded on the board, the project chips wear their colours.
test("the menu's search opens a card by its number and the board by words; the board searches as one types", async ({ page }) => {
  await page.goto("/inbox");
  const made = await api(page, "POST", "/api/cards", { title: "E2E: put off till tomorrow", project: "digipaddock" });
  expect(made.status).toBe(201);
  const id: string = made.body.id;
  const until = new Date(Date.now() + 2 * 86_400_000).toISOString().slice(0, 16);
  expect((await api(page, "POST", `/api/cards/${id}/defer`, { until, reason: "e2e" })).status).toBe(200);

  const find = page.locator("nav.side").getByRole("searchbox", { name: "Найти задачу" });
  await find.fill(id.split("-")[1]); // the bare number
  await find.press("Enter");
  await page.waitForURL(new RegExp(`/cards/${id}$`));
  await expect(find).toHaveValue("");

  await find.fill("Nightly backup");
  await find.press("Enter");
  await page.waitForURL(/\/board\?q=Nightly/);
  const search = page.getByRole("searchbox", { name: "Поиск по карточкам" });
  await expect(search).toHaveValue("Nightly backup");
  await expect(page.locator("a.bc", { hasText: "Nightly backup check" }).first()).toBeVisible();

  // no Enter: the board follows the field
  await search.fill("put off till");
  await page.waitForURL(/q=put\+off\+till|q=put%20off%20till/);
  const tile = page.locator(`a.bc[href='/cards/${id}']`);
  await expect(tile).toHaveClass(/deferred/);
  await expect(search).toHaveValue("put off till");
  const chip = page.locator(".filters .chip", { hasText: /^DigiPaddock$|^digipaddock$/i }).first();
  expect(await chip.evaluate((el) => getComputedStyle(el).borderTopColor)).not.toBe(
    await page.locator(".filters .chip").first().evaluate((el) => getComputedStyle(el).borderTopColor));
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/board");
    await expect(page.locator("a.bc").first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `width ${width}`).toBe(true);
    await page.screenshot({ path: `../../../tmp/cabinet-dev/board-${width}.png` });
    await page.goto(`/board?q=${encodeURIComponent("E2E")}&project=digipaddock`);
    await expect(tile).toBeVisible();
    await page.screenshot({ path: `../../../tmp/cabinet-dev/board-${width}-search.png` });
  }
});
