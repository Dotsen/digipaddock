// The brief as a versioned document. The seed's handed brief reads "Done · v1" and its indicator opens
// the tab; an edit of the description makes it stale (the start is off and says why); a person's revision is v2
// with its changes; the start approves v2 and the card goes To Do.
import { expect } from "@playwright/test";
import { cardByTitle, getJson, ownerTest as test } from "./helpers";

test("a brief goes ready → stale → a person's v2 → approved by the start", async ({ page }) => {
  await page.goto("/inbox");
  const card = await cardByTitle(page, "Daily error summary");
  await page.goto(`/cards/${card.id}`);
  const indicator = page.locator(".props button.chip.brief");
  await expect(indicator).toHaveText("Готово · v1");
  await indicator.click();
  await expect(page.getByRole("tab", { name: "ТЗ" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".view")).toContainText("Source: the session log.");
  await expect(page.getByText("подготовил агент")).toBeVisible();

  await page.getByRole("button", { name: "Править" }).click();
  await page.getByLabel("Описание и AC", { exact: true }).fill(
    "Goal: a daily summary of the agents' errors.\n\nSource: the session log and the monitor.\nFor: the owner, at 09:00.",
  );
  await page.getByRole("button", { name: "Сохранить", exact: true }).click();
  await expect(indicator).toHaveText("Изменено после готовности · v1");
  const start = page.getByRole("button", { name: "Одобрить ТЗ и запустить" });
  await expect(start).toBeDisabled();
  await expect(page.getByText("ТЗ изменилось после готовности — сохраните редакцию или верните аналитику").first()).toBeVisible();
  await expect(page.getByText("Изменения после v1")).toBeVisible();
  await expect(page.locator(".diff .d-add").first()).toContainText("Source: the session log and the monitor.");

  await page.getByRole("button", { name: "Сохранить как новую редакцию" }).click();
  await expect(indicator).toHaveText("Готово · v2");
  const versions = page.locator(".versions > li");
  await expect(versions).toHaveCount(2);
  await expect(versions.nth(0).locator("summary b")).toContainText("v2 · @owner");
  await expect(versions.nth(0)).toContainText("редакция человека");
  await versions.nth(0).locator("summary").click();
  await expect(versions.nth(0).locator(".diff .d-del").first()).toContainText("Source: the session log.");

  await expect(start).toBeEnabled();
  await start.click();
  await expect(indicator).toHaveText("Согласовано · v2");
  const after = await getJson(page, `/api/cards/${card.id}`);
  expect(after.status).toBe("To Do");
  const brief = await getJson(page, `/api/cards/${card.id}/brief`);
  expect(brief.versions.map((v: { approved_by: string }) => v.approved_by)).toEqual(["", "user:owner"]);
});
