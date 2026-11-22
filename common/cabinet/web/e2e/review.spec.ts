// Cards in Review, from the inbox: several picked, accepted at once, each with its own result.
import { expect } from "@playwright/test";
import { getJson, ownerTest as test } from "./helpers";

test("two Review cards accepted together end in Done", async ({ page }) => {
  await page.goto("/inbox");
  await expect(page.getByText("На проверке")).toBeVisible();
  // the seed's two plain Review cards (its long-thread one belongs to result.spec)
  const SEED = ["Push notifications: quiet hours", "Update the price list"];
  const review: { id: string; title: string }[] = (await getJson(page, "/api/inbox")).review.filter((card: { title: string }) =>
    SEED.includes(card.title),
  );
  expect(review).toHaveLength(2);

  await page.getByRole("button", { name: "Выбрать" }).click();
  for (const card of review) await page.getByRole("button", { name: new RegExp(card.title) }).click();
  await page.getByRole("button", { name: "Принять" }).click();
  for (const card of review) await expect(page.getByText(`${card.id}: готово`)).toBeVisible();
  for (const card of review) expect((await getJson(page, `/api/cards/${card.id}`)).status).toBe("Done");
});
