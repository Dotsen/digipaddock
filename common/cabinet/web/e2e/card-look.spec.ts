// The card's page as the owner sees it, at a phone's and a desktop's width: the header, the next step, the
// discussion and the tabs of cards in different states. Nothing overflows the screen; the screenshots
// (tmp/cabinet-dev/card-look/, or KD_LOOK_DIR) compare the look before and after a change of the design tokens.
import { expect } from "@playwright/test";
import { cardByTitle, ownerTest as test } from "./helpers";

const CARDS = [
  ["thread", "Card page in the cabinet", ""],
  ["review", "Push notifications: quiet hours", "Результат"],
  ["brief", "Daily error summary", "ТЗ"],
  ["failed", "Build the spend report", ""],
  ["council", "Nightly backup check", "ТЗ"],
] as const;

test("the card's page fits a phone and a desktop in every state", async ({ page, isMobile }) => {
  test.skip(isMobile, "both widths are set here");
  const dir = process.env.KD_LOOK_DIR ?? "card-look";
  await page.goto("/board");
  for (const [name, title, tab] of CARDS) {
    const { id } = await cardByTitle(page, title);
    for (const width of [390, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`/cards/${id}`);
      await expect(page.getByRole("heading", { level: 1 })).toContainText(title);
      if (tab) await page.getByRole("tab", { name: tab, exact: true }).click();
      await page.waitForTimeout(600); // the thread and the tab's own reads settle
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name} at ${width}`).toBe(true);
      await page.screenshot({ path: `../../../tmp/cabinet-dev/${dir}/${name}-${width}.png`, fullPage: true });
    }
  }
});
