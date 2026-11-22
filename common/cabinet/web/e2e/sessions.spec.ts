// a card starts a session on the (scripted) session host: the run state follows it and the agent's
// message arrives in the thread without a reload; a failed run says so and continues from the Sessions tab;
// the agent's chat answers; a Review card sent back for rework continues on its agent's host. The
// dev cabinet hosts @analyst and @dev on kd.cabinet.devhost.
import { expect, type Page } from "@playwright/test";
import { api, ownerTest as test } from "./helpers";

const thread = (page: Page) => page.locator(".thread");
const field = (page: Page) => page.locator(".composer textarea");
const run = (page: Page) => page.locator(".run");

test("a card starts a session, its messages arrive live, a failed run continues, the agent chat", async ({ page }) => {
  await page.goto("/board");
  // the card names a model of the subscription, so its turns say which model served them
  const created = await api(page, "POST", "/api/cards", { title: "Сессия аналитика из e2e", project: "digipaddock", model: "claude:sonnet" });
  expect(created.status).toBe(201);
  const id: string = created.body.id;
  await page.goto(`/cards/${id}`);

  // "Write the brief": the card goes to @analyst, the cabinet queues its command, the dev host runs the turn
  await page.getByRole("button", { name: "Разработать ТЗ" }).click();
  await expect(run(page).getByText("@analyst работает")).toBeVisible();
  await expect(thread(page).getByText(`Got it: [board] ${id} assigned to you`)).toBeVisible();
  await expect(run(page)).toHaveCount(0); // done, nobody is asked anything

  // a message the dev host fails: the run state says so; the Sessions tab continues the session
  await field(page).fill("@analyst [fail] проверь");
  await field(page).press("Control+Enter");
  await expect(run(page).getByText("Не удалось: ошибка во время хода")).toBeVisible();
  await expect(run(page).getByRole("button", { name: "Повторить" })).toBeVisible();
  await page.getByRole("tab", { name: "Сессии" }).click();
  await expect(page.getByText("сбой")).toBeVisible();
  await page.getByRole("button", { name: "Продолжить" }).click();
  await expect(page.getByText("ждёт задачу")).toBeVisible();

  // the session's turns, newest first; the first one's steps and its tool's masked output
  await page.getByRole("button", { name: "Ход" }).click();
  const first = page.locator("details.turn").last();
  await expect(first.locator("summary")).toContainText("Назначение");
  await expect(first.locator("summary")).toContainText("claude-sonnet-5-5");
  await first.locator("summary").click();
  await expect(first.getByText("Bash: ls work")).toBeVisible();
  await first.getByRole("button", { name: "Вывод" }).click();
  await expect(first.locator("pre.output")).toHaveText("brief.md\nnotes.md");
  await page.getByRole("tab", { name: "Обсуждение" }).click();
  await expect(thread(page).getByText(/Got it: \[session\] .*your previous run ended \(failed\)/)).toBeVisible();

  // the agent's chat: the owner writes, the agent answers in place
  await page.goto("/agents/analyst");
  await expect(page.getByText(/Работает \d из 2 · в очереди \d/)).toBeVisible();
  await field(page).fill("Привет из e2e");
  await field(page).press("Control+Enter");
  await expect(thread(page).getByText("Привет из e2e")).toBeVisible();
  await expect(thread(page).getByText(/Got it: \[chat\] a message for you/)).toBeVisible();
});

test("a Review card sent back with a comment continues its agent's work by it", async ({ page }) => {
  await page.goto("/board");
  const created = await api(page, "POST", "/api/cards", { title: "Вернуть на доработку из e2e", project: "digipaddock" });
  const id: string = created.body.id;
  const review = await api(page, "PATCH", `/api/cards/${id}`, {
    revision: created.body.revision,
    changes: { status: "Review" },
    override_reason: "подготовка e2e",
  });
  expect(review.status).toBe(200);
  await page.goto(`/cards/${id}`);
  await expect(page.getByRole("button", { name: "Принять" })).toBeVisible();
  await page.getByRole("button", { name: "Вернуть на доработку" }).click();
  await page.getByLabel("Что доработать").fill("Добавь тест на пустой список");
  await page.locator(".ask").getByRole("button", { name: "Отправить" }).click();
  await expect(thread(page).getByText("Добавь тест на пустой список")).toBeVisible();
  await expect(thread(page).getByText(`Got it: [board] ${id}: back in work — read the thread for what to do next`)).toBeVisible();
  await expect(page.getByRole("button", { name: "Принять" })).toHaveCount(0); // In Progress: no big buttons
});
