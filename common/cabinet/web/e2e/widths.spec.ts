// At 375, 768 and 1440 px the card — a long title, labels, an open
// question and the next step —, the overview and the Inbox never scroll sideways, and the thread's field stays
// in view.
import { expect } from "@playwright/test";
import { api, executor, ownerTest as test } from "./helpers";

// the longest title a card takes (120), with a word no line can break
const LONG = "Длинный заголовок карточки на всю строку и ещё немного: проверка_переноса_длинного_слова_без_пробелов_в_заголовке";

test("the card and the overview fit 375, 768 and 1440 px", async ({ page }) => {
  await page.goto("/");
  const created = await api(page, "POST", "/api/cards", { title: LONG, project: "digipaddock" });
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const card = created.body;
  const labelled = await api(page, "PATCH", `/api/cards/${card.id}`, {
    revision: card.revision,
    changes: { labels: ["очень-длинная-метка-без-пробелов-для-проверки-ширины", "spec:implementer"] },
  });
  expect(labelled.status, JSON.stringify(labelled.body)).toBe(200);
  await executor(page, { op: "decision.open", type: "question", agent: "dev", card: card.id, payload: { text: "Вопрос по ширине?" } });

  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`/cards/${card.id}`);
    await expect(page.getByText("Вопрос по ширине?")).toBeVisible();
    await page.locator("main").getByRole("button", { name: "Ещё" }).click(); // the labels are among the secondary facts
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `card at ${width}`).toBe(true);
    await page.locator(".thread").evaluate((el) => (el.scrollTop = el.scrollHeight));
    await page.evaluate(() => window.scrollTo(0, 1e6)); // below 1100 px the header scrolls away
    await expect(page.locator(".composer textarea")).toBeInViewport();

    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Дашборд", level: 1 })).toBeVisible();
    await expect(page.locator("#needs")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `overview at ${width}`).toBe(true);

    // the Inbox's header — the queue link, the heading, its count, "Select" — wraps on a phone
    await page.goto("/inbox");
    await expect(page.getByRole("button", { name: "Выбрать" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `inbox at ${width}`).toBe(true);
  }
});

// the board at 375 px — a title of one long word and many labels wrap, every card shows its
// status; from 900 px the pill is gone (the column names the status)
test("the board fits 375 px and shows the status on each card there only", async ({ page }) => {
  await page.goto("/board");
  const created = await api(page, "POST", "/api/cards", { title: "Ширина_доски_" + "очень_длинное_слово_".repeat(5), project: "digipaddock" });
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const labels = Array.from({ length: 8 }, (_, i) => `метка-доски-без-пробелов-${i}`);
  const labelled = await api(page, "PATCH", `/api/cards/${created.body.id}`, { revision: created.body.revision, changes: { labels } });
  expect(labelled.status, JSON.stringify(labelled.body)).toBe(200);

  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto("/board");
  await expect(page.locator(`a.bc[href="/cards/${created.body.id}"]`)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "board at 375").toBe(true);
  const cards = page.locator("a.bc");
  const total = await cards.count();
  await expect(page.locator("a.bc .pill")).toHaveCount(total);
  for (const pill of await page.locator("a.bc .pill").all()) await expect(pill).toBeVisible();

  await page.setViewportSize({ width: 1440, height: 800 });
  await expect(page.locator("a.bc .pill").first()).toBeHidden();
});

test("an agent's name is never covered by its state: the chip goes to the next line", async ({ page }) => {
  for (const width of [375, 900, 1280, 1440]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/agents");
    await expect(page.locator(".agent .chip.state").first()).toBeVisible();
    const covered = await page.locator(".agent .head").evaluateAll((heads) =>
      heads.flatMap((head) => {
        const who = head.querySelector(".who")!, chip = head.querySelector(".chip.state")!;
        const a = who.getBoundingClientRect(), b = chip.getBoundingClientRect();
        const overlap = a.right > b.left + 0.5 && b.right > a.left && a.bottom > b.top && b.bottom > a.top;
        return overlap || who.scrollWidth > who.clientWidth + 1 ? [who.textContent] : [];
      }),
    );
    expect(covered, `agents at ${width}`).toEqual([]);
  }
});
