// On a phone: the bottom tab bar, and a long thread that scrolls inside the card, not the page.
import { expect } from "@playwright/test";
import { api, cardByTitle, executor, ownerTest as test } from "./helpers";

// A long question of an agent is a short line in the Inbox — at most three lines on a 360 px phone,
// its text unfolds on "Show"
test("a long question takes at most three lines in the Inbox", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await page.goto("/inbox");
  const created = await api(page, "POST", "/api/cards", { title: "Длинный вопрос из e2e", project: "digipaddock" });
  expect(created.status).toBe(201);
  const text = `Какой вариант брать для KD-76?\n\n${"Подробное пояснение аналитика. ".repeat(70)}`.slice(0, 2000);
  await executor(page, { op: "decision.open", type: "question", agent: "dev", card: created.body.id, payload: { text } });

  await page.goto("/inbox");
  const row = page.locator(".item.question", { hasText: "Длинный вопрос из e2e" });
  await expect(row).toBeVisible();
  const lineHeight = await row.locator(".t").evaluate((el) => parseFloat(getComputedStyle(el).lineHeight));
  const box = (await row.locator(".q-body").boundingBox())!;
  expect(box.height).toBeLessThanOrEqual(lineHeight * 3 + 24); // title, who and card, "Show"
  await expect(row.locator(".full")).toHaveCount(0);

  await row.getByRole("button", { name: "Показать" }).click();
  await expect(row.locator(".full")).toContainText("Подробное пояснение аналитика.");
  await row.getByRole("button", { name: "Свернуть" }).click();
  await expect(row.locator(".full")).toHaveCount(0);
  await row.getByRole("button", { name: /Какой вариант брать для KD-76/ }).click();
  await page.waitForURL(/\/cards\/KD-\d+\?decision=/);
});

// "More" — a sheet with the board, the servers, the feed and the settings; lit on their pages;
// no "Back" on a phone
test("«Ещё» opens the board, the servers, the feed and the settings and is lit there", async ({ page }) => {
  await page.goto("/inbox");
  const tabbar = page.locator("nav.tabbar");
  const more = tabbar.getByRole("button", { name: "Ещё" });
  const sheet = page.getByRole("dialog", { name: "Ещё" });
  await expect(tabbar.getByRole("link", { name: "Входящие" })).toHaveAttribute("aria-current", "page");
  await expect(more).not.toHaveAttribute("aria-current", "page");

  await more.click();
  await expect(sheet.getByRole("link")).toHaveText(["Доска", "Серверы", "Агенты", "Лента", "Настройки"]);
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);

  // a task is added from any page — "+" beside the search, the first row of "More"
  await more.click();
  await sheet.getByRole("button", { name: "Добавить задачу" }).click();
  await expect(sheet).toHaveCount(0);
  const form = page.getByRole("dialog", { name: "Новая карточка" });
  await expect(form.getByLabel("Заголовок", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await form.getByRole("button", { name: "Закрыть" }).click();
  await expect(form).toBeHidden();

  await more.click();
  await page.mouse.click(10, 10); // outside the sheet
  await expect(sheet).toHaveCount(0);

  await more.click();
  await sheet.getByRole("link", { name: "Доска" }).click();
  await page.waitForURL(/\/board$/);
  await expect(sheet).toHaveCount(0);
  await expect(more).toHaveAttribute("aria-current", "page");

  const { id } = await cardByTitle(page, "Card page in the cabinet");
  for (const path of [`/cards/${id}`, "/servers", "/feed", "/settings"]) {
    await page.goto(path);
    await expect(more).toHaveAttribute("aria-current", "page");
  }
  await page.goto(`/cards/${id}`);
  await expect(page.getByRole("button", { name: "Назад" })).toBeHidden();
});

// On a phone the projects and the search are in view; the filters under "Filters", the
// new card's form under "Add"
test("the board on a phone folds its filters and the new card's form", async ({ page }) => {
  await page.goto("/board");
  await expect(page.locator(".filters .chip").first()).toBeVisible();
  await expect(page.getByLabel("Поиск по карточкам")).toBeVisible();
  for (const name of ["Статус", "Исполнитель", "Специализация", "Метка"]) await expect(page.getByLabel(name, { exact: true })).toBeHidden();
  await expect(page.getByLabel("Веха", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Заголовок", { exact: true })).toBeHidden();

  const filters = page.getByRole("button", { name: /^Фильтры/ });
  await expect(filters).toHaveText("Фильтры");
  await filters.click();
  await page.getByLabel("Статус", { exact: true }).selectOption("Review");
  await page.waitForURL(/status=Review/);
  await page.getByLabel("Исполнитель", { exact: true }).selectOption({ index: 1 });
  await expect(filters).toHaveText(/Фильтры\s*2/);
  for (const card of await page.locator("a.bc").all()) await expect(card.locator(".pill")).toHaveText(/Review/);

  await page.reload();
  await expect(page).toHaveURL(/status=Review/);
  await expect(page.getByRole("button", { name: /^Фильтры/ })).toHaveText(/Фильтры\s*2/);

  const long = "A long title of a card that is picked as a dependency and has to wrap inside its chip on a phone, not widen it";
  expect((await api(page, "POST", "/api/cards", { title: long, project: "digipaddock" })).status).toBe(201);
  await page.getByRole("button", { name: "Добавить", exact: true }).click();
  const form = page.getByRole("dialog", { name: "Новая карточка" });
  await expect(form.getByLabel("Заголовок", { exact: true })).toBeVisible();
  await form.getByText("Ещё", { exact: true }).click();
  await expect(form.getByRole("combobox", { name: "Зависит от" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await form.evaluate((dialog) => dialog.scrollWidth <= dialog.clientWidth)).toBe(true);
  // a menu of the form fits the phone, and its Escape closes the menu, not the form
  for (const name of ["Исполнитель", "Модель"]) {
    await form.getByRole("button", { name, exact: true }).click();
    await expect(form.getByRole("listbox", { name })).toBeVisible();
    expect(await form.evaluate((dialog) => dialog.scrollWidth <= dialog.clientWidth)).toBe(true);
    await page.keyboard.press("Escape");
    await expect(form.getByRole("listbox", { name })).toBeHidden();
    await expect(form).toBeVisible();
  }
  // an open list with search fits the phone too, and its Escape closes the list, not the form
  for (const name of ["Метки", "Зависит от"]) {
    await form.getByRole("combobox", { name }).click();
    await expect(form.getByRole("listbox", { name })).toBeVisible();
    expect(await form.evaluate((dialog) => dialog.scrollWidth <= dialog.clientWidth)).toBe(true);
    await page.keyboard.press("Escape");
    await expect(form.getByRole("listbox", { name })).toBeHidden();
    await expect(form).toBeVisible();
  }
  // a chosen card with a long title wraps inside its chip, and its «×» stays in reach
  const deps = form.getByRole("group", { name: "Зависит от" });
  await deps.getByRole("combobox").fill("long title of a card");
  await deps.getByRole("option", { name: new RegExp(long) }).click();
  await page.keyboard.press("Escape");
  const edge = await form.evaluate((dialog) => dialog.getBoundingClientRect().right);
  for (const part of [deps.locator(".chip"), deps.getByRole("button", { name: /^Убрать: / })]) {
    const box = (await part.boundingBox())!;
    expect(box.x + box.width).toBeLessThanOrEqual(edge);
  }
  await deps.getByRole("button", { name: /^Убрать: / }).click();
  // a search that finds nothing says so in plain text, not in a framed note
  await deps.getByRole("combobox").fill("no such card anywhere");
  const none = deps.getByRole("listbox").getByText("Ничего не найдено");
  expect(await none.evaluate((el) => getComputedStyle(el).borderTopStyle)).toBe("none");
  await deps.getByRole("combobox").fill("");
  await page.keyboard.press("Escape");
  // unsent text survives closing the form
  await form.getByLabel("Заголовок", { exact: true }).fill("Черновик с телефона");
  await form.getByLabel("Заголовок", { exact: true }).press("Escape");
  await expect(form).toBeHidden();
  await page.getByRole("button", { name: "Добавить", exact: true }).click();
  await expect(form.getByLabel("Заголовок", { exact: true })).toHaveValue("Черновик с телефона");
});

test("the tab bar is there and the thread scrolls inside the card", async ({ page }) => {
  await page.goto("/inbox");
  const tabbar = page.locator("nav.tabbar");
  await expect(tabbar).toBeVisible();
  // the sections and "More", the deploys and the incidents among them; the board, the servers, the agents, the feed
  // and the settings are under "More"
  await expect(tabbar.getByRole("link")).toHaveText([/Входящие/, /Дашборд/, /BOSS/, /Деплой/, /Инциденты/]);
  await expect(tabbar.getByRole("button", { name: "Ещё" })).toBeVisible();

  const { id } = await cardByTitle(page, "Card page in the cabinet");
  await page.goto(`/cards/${id}`);
  await expect(page.getByText("On it. Starting with the markup and the tests.")).toBeVisible();
  const [scroll, client] = await page.locator(".thread").evaluate((el) => [el.scrollHeight, el.clientHeight]);
  expect(scroll).toBeGreaterThan(client);
  // its blocks keep their height while it scrolls (a decision card once shrank to a line)
  const decision = page.locator(".thread article.dec").first();
  await decision.scrollIntoViewIfNeeded();
  expect((await decision.boundingBox())!.height).toBeGreaterThan(60);
  // the header and the next step scroll away with the page; the tabs, the thread and its field keep a whole
  // screen above the tab bar (fixed above it they left the thread a line)
  await page.evaluate(() => window.scrollTo(0, 1e6));
  const tabs = (await page.locator(".cardview .tabs").boundingBox())!;
  const list = (await page.locator(".thread").boundingBox())!;
  const field = (await page.locator(".composer").boundingBox())!;
  const bar = (await tabbar.boundingBox())!;
  expect(tabs.y).toBeLessThan(40);
  expect(list.height).toBeGreaterThan(200);
  expect(field.y + field.height).toBeLessThanOrEqual(bar.y);
  // the attach (and voice) buttons stand in a column above send, the text field takes the rest of the width
  const send = (await page.locator(".composer .send").boundingBox())!;
  for (const name of ["Прикрепить", "Записать голосовое"]) {
    const button = page.locator(".composer .actions").getByRole("button", { name });
    if (!(await button.count())) continue;
    const box = (await button.boundingBox())!;
    expect(Math.abs(box.x - send.x)).toBeLessThan(2);
    expect(box.y + box.height).toBeLessThanOrEqual(send.y);
  }
  const text = (await page.locator(".composer textarea").boundingBox())!;
  expect(text.width).toBeGreaterThan(field.width - send.width - 40);
});
