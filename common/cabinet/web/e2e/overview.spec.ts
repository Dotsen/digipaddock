// the overview of the seed — its sections and counts, a question that opens its card at
// it, and an answer given in another tab that clears the row without a reload.
import { expect } from "@playwright/test";
import { api, executor, getJson, ownerTest as test } from "./helpers";

test("the overview's sections and counts, a question opens its card, an answer elsewhere clears it live", async ({ page, browser }, info) => {
  await page.goto("/");
  const created = await api(page, "POST", "/api/cards", { title: "Вопрос для обзора из e2e", project: "digipaddock" });
  expect(created.status).toBe(201);
  const id: string = created.body.id;
  await executor(page, { op: "decision.open", type: "question", agent: "dev", card: id, payload: { text: "Брать вариант Б?" } });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Дашборд", level: 1 })).toBeVisible();
  const found = await getJson(page, "/api/dashboard");
  const counts = page.getByRole("navigation", { name: "Сводка" });
  await expect(counts.getByRole("link", { name: `${found.counts.needs} Ждут вас` })).toBeVisible();
  await expect(counts.getByRole("link", { name: `${found.problems.filter((p: any) => p.kind !== "waiting").length} Ошибки выполнения` })).toBeVisible();
  for (const section of ["Ждут вас", "В работе", "Проблемы", "Изменения", "Команда"]) {
    await expect(page.getByRole("heading", { name: new RegExp(section), level: 2 })).toBeVisible();
  }
  // the seed's failed run is an error, its blocked card waits for a dependency: both in words
  const problems = page.locator("#problems");
  await expect(problems.getByText(/процесс сессии завершился \(код 1\)/)).toBeVisible();
  await expect(problems.getByText(/Ждёт зависимостей/)).toBeVisible();

  // the question opens its card at the question
  const needs = page.locator("#needs");
  await needs.getByRole("link", { name: /Вопрос для обзора из e2e/ }).click();
  await page.waitForURL((url) => url.pathname === `/cards/${id}` && url.searchParams.has("decision"));
  await expect(page.getByText("Брать вариант Б?")).toBeVisible();

  // another tab answers it: the overview drops the row without a reload
  await page.goto("/");
  await expect(needs.getByRole("link", { name: /Вопрос для обзора из e2e/ })).toBeVisible();
  const other = await browser.newContext({ storageState: `${info.project.metadata.state}/auth/owner.json`, baseURL: info.project.use.baseURL });
  const tab = await other.newPage();
  await tab.goto("/");
  const open = (await getJson(tab, `/api/decisions?state=open&card=${id}`)).map((item: any) => item.decision);
  const answered = await api(tab, "POST", `/api/decisions/${open[0].id}/reply`, { text: "да" });
  expect(answered.status).toBe(200);
  await expect(needs.getByRole("link", { name: /Вопрос для обзора из e2e/ })).toHaveCount(0);
  await other.close();
});


test.describe("operator viewport", () => {
  test.use({ serviceWorkers: "block" });

for (const [width, height] of [[1280, 720], [1440, 900], [1920, 1080], [375, 812]]) {
  test(`overview layout at ${width}x${height} with long lists`, async ({ page }, info) => {
    await page.setViewportSize({ width, height });
    await page.goto("/");
    const seed = await getJson(page, "/api/dashboard");
    const long = "Очень длинное название задачи оператора для проверки переноса и доступности содержимого ".repeat(3);
    const sample = {
      ...seed,
      needs: Array.from({ length: 24 }, (_, i) => ({ ...seed.needs[0], card: `KD-${8000 + i}`, decision: `d-${i}`, title: i === 0 ? long : `Нужен ответ по задаче ${i + 1}` })),
      work: Array.from({ length: 24 }, (_, i) => ({ ...seed.work[0], card: `KD-${8100 + i}`, title: `Работа команды ${i + 1}`, run: i % 2 ? "queued" : "working" })),
      problems: Array.from({ length: 12 }, (_, i) => ({ ...seed.problems[0], card: `KD-${8200 + i}`, title: `Сигнал по запуску ${i + 1}` })),
      changes: Array.from({ length: 50 }, (_, i) => ({ ...seed.changes[0], seq: 9000 - i, title: `Изменение ${i + 1}`, new: true })),
      counts: { needs: 24, work: 24, problems: 12, changes_new: 50 },
      seen: 0, watermark: 9000,
    };
    await page.route(/\/api\/dashboard(?:\?|$)/, (route) => route.fulfill({ json: sample }));
    await page.reload();
    await expect(page.getByRole("link", { name: "24 Ждут вас" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width >= 900) {
      expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1)).toBe(true);
      for (const id of ["needs-title", "work-title", "problems-title", "changes-title", "team-title"]) {
        await expect(page.locator(`#${id}`)).toBeInViewport();
      }
      const scroll = page.locator("#needs .panel-body");
      expect(await scroll.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
      await scroll.focus();
      await page.keyboard.press("End");
      await expect.poll(() => scroll.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
    }
    await expect(page.getByRole("link", { name: /^Серверы:/ })).toHaveAttribute("href", "/servers");
    await page.locator("#needs .panel-body").evaluate((el: HTMLElement) => { el.scrollTop = 0; el.blur(); });
    await page.screenshot({ path: info.outputPath(`overview-${width}.png`), fullPage: true });
  });
}

});
