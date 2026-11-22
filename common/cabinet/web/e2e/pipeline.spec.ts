// the pipeline's records on the seed's card — the problem, the outcome @dev recorded and what blocks it,
// with a link to the question — and the card's row among the overview's problems.
import { expect } from "@playwright/test";
import { cardByTitle, ownerTest as test } from "./helpers";

const TITLE = "Agents' progress report";

test("the card's next step shows the pipeline's problem, outcome and waits; the overview lists it", async ({ page }) => {
  await page.goto("/");
  const { id } = await cardByTitle(page, TITLE);
  await page.goto(`/cards/${id}`);
  const next = page.getByRole("region", { name: "Следующий шаг" });
  await expect(next.getByText(/Ход @dev закончился без следующего шага — BOSS разбирает/)).toBeVisible();
  await expect(next.getByText(/@dev ждёт ответа: waiting for an answer: count a retry as a separate row/)).toBeVisible();
  const blocking = next.getByRole("list", { name: "Блокирует" });
  await expect(blocking.getByRole("link", { name: "вопрос" })).toBeVisible();
  await blocking.getByRole("link", { name: "вопрос" }).click();
  await page.waitForURL((url) => url.pathname === `/cards/${id}` && url.searchParams.has("decision"));
  await expect(page.getByText("Count a retry as a separate row?").first()).toBeVisible();

  await page.goto("/");
  const problems = page.locator("#problems");
  const row = problems.locator(".entry", { hasText: TITLE });
  await expect(row.getByText("Ход @dev закончился без следующего шага")).toBeVisible();
  await expect(row.getByText("Застряла")).toBeVisible();
});
