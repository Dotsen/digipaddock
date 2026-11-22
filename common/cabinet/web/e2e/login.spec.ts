// Scenario 1: a one-time code, the setup page, a passkey; out and back in with the key alone. It also keeps
// the owner's session and passkey for the specs that follow (projects depend on this one).
import { expect, test } from "@playwright/test";
import { codes, loginByCode, saveOwner, signOut, virtualAuthenticator } from "./helpers";

test("a code, a passkey on the setup page, then signing in with the key alone", async ({ page, isMobile }) => {
  const auth = await virtualAuthenticator(page);
  await loginByCode(page, "owner", codes().owner);
  await page.waitForURL(/\/setup$/);
  await page.getByRole("button", { name: "Создать ключ" }).click();
  await expect(page.getByText("Ключ добавлен")).toBeVisible();
  await page.getByRole("button", { name: "Готово" }).click();
  await page.waitForURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Дашборд" })).toBeVisible();

  await signOut(page);
  await page.getByRole("button", { name: "Войти с ключом" }).click();
  await page.waitForURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Дашборд" })).toBeVisible();
  if (isMobile) await expect(page.locator("nav.tabbar")).toBeVisible();
  else await expect(page.locator("nav.side")).toBeVisible();

  await saveOwner(page, auth);
});
