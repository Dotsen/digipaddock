// The seed's Review card with a long thread — "Result" shows its summary and the
// link in it, "History" pages back, and "Show earlier" in the thread keeps the reader's place.
import { expect } from "@playwright/test";
import { cardByTitle, getJson, ownerTest as test } from "./helpers";

const TITLE = "Card history in pages";

test("a Review card's result and link, its history paged back, the long thread's earlier page in place", async ({ page }) => {
  await page.goto("/");
  const { id } = await cardByTitle(page, TITLE);
  await page.goto(`/cards/${id}?tab=result`);
  await expect(page.getByText(/the thread and the history load in pages/)).toBeVisible();
  await expect(page.getByRole("link", { name: "https://forge.example/pr/42" }).first()).toBeVisible();
  await expect(page.getByText("Ready for review")).toBeVisible(); // the comment it was sent to Review with

  await page.getByRole("tab", { name: "История" }).click();
  const rows = page.locator(".history li");
  await expect(rows.first()).toBeVisible();
  const before = await rows.count();
  await page.getByRole("button", { name: "Показать ранее" }).click();
  await expect.poll(() => rows.count()).toBeGreaterThan(before);

  await page.getByRole("tab", { name: "Обсуждение" }).click();
  const thread = page.locator(".thread");
  await expect(thread.getByText("Message #450", { exact: true })).toBeVisible();
  await expect(thread.getByText("Message #1", { exact: true })).toHaveCount(0);
  await thread.evaluate((el) => (el.scrollTop = 0));
  const anchor = thread.getByText("Message #300", { exact: true });
  const top = (await anchor.boundingBox())!.y;
  await thread.getByRole("button", { name: "Показать ранее" }).click();
  await expect(thread.getByText("Message #120", { exact: true })).toBeAttached();
  await expect.poll(async () => Math.abs((await anchor.boundingBox())!.y - top)).toBeLessThan(4);
});

test("a link to a decision far above the long thread's window pages back to it and shows it", async ({ page }) => {
  await page.goto("/");
  const { id } = await cardByTitle(page, TITLE);
  const [early]: { id: string }[] = await getJson(page, `/api/cards/${id}/decisions`);
  await page.goto(`/cards/${id}?decision=${early.id}`);
  const decision = page.locator(`#decision-${early.id}`);
  await expect(decision).toBeInViewport({ timeout: 10_000 });
  await expect(page.locator(".thread").getByText("Message #10", { exact: true })).toBeAttached();
});
