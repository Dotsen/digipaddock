// an agent's question form from the seed — the recommendation is marked, never preset; own words
// span lines; Ctrl+Enter sends; the thread shows the chosen labels. An old text question: its options are
// recognized, the original text is there on request, and a free-text answer goes instead of the form.
import { expect, type Page } from "@playwright/test";
import { cardByTitle, getJson, ownerTest as test } from "./helpers";

async function decisionOf(page: Page, card: string) {
  return (await getJson(page, `/api/decisions?state=all&card=${card}`))[0].decision;
}

test("a question form is answered with options, own words and Ctrl+Enter", async ({ page }) => {
  await page.goto("/inbox");
  const card = await cardByTitle(page, "Brief: memory service probe");
  await page.goto(`/cards/${card.id}`);
  const recommended = page.getByRole("radio", { name: /A direct HTTP request/ });
  await expect(recommended).not.toBeChecked();
  await expect(page.locator("label", { has: recommended })).toContainText("Рекомендуется");
  await page.getByRole("radio", { name: /Through the gateway/ }).check();
  await page.getByRole("checkbox", { name: /The queue/ }).check();
  await page.getByRole("textbox", { name: "What else should be checked? — Свой ответ" }).fill("and the delay");
  const when = page.getByRole("textbox", { name: "When can a stop be tried?" });
  await when.click();
  await page.keyboard.type("ночью");
  await page.keyboard.press("Enter");
  await page.keyboard.type("после 02:00");
  await expect(when).toHaveValue("ночью\nпосле 02:00");
  await page.keyboard.press("Control+Enter");

  // the answer in the thread reads question by question, as does the decision's result
  const answered = page.locator(".msg.sys .answer li", { hasText: "How should the service be probed?" });
  await expect(answered.locator(".a")).toHaveText("Through the gateway");
  await expect(page.locator("ol.chosen")).toContainText("The queue — and the delay");
  const decision = await decisionOf(page, card.id);
  expect(decision.state).toBe("approved");
  expect(decision.answer.answers.probe.selected_option_ids).toEqual(["gate"]);
  expect(decision.answer.answers.window.custom_text).toBe("ночью\nпосле 02:00");
});

test("an old question shows its recognized options, the original and takes free text", async ({ page }) => {
  await page.goto("/inbox");
  const card = await cardByTitle(page, "Brief: weekly spend summary");
  await page.goto(`/cards/${card.id}`);
  const question = page.locator("article.dec");
  await expect(question.getByText("Варианты распознаны из текста")).toBeVisible();
  await expect(question.getByRole("radio", { name: /from the token ledger/ })).not.toBeChecked();
  const original = question.locator("details.original");
  await original.locator("summary").click();
  await expect(original).toContainText("What is the spend alert threshold");
  await question.getByRole("button", { name: "Ответить свободным текстом" }).click();
  await question.getByRole("textbox", { name: "Ваш ответ…" }).fill("А, только владельцу, по понедельникам");
  await question.getByRole("button", { name: "Ответить", exact: true }).click();
  await expect.poll(async () => (await decisionOf(page, card.id)).state).toBe("approved");
  const decision = await decisionOf(page, card.id);
  expect(decision.reply).toBe("А, только владельцу, по понедельникам");
  expect(decision.answer).toBeNull();
});
