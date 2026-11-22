import { expect, test } from "@playwright/test";
import { api, codes, loginByCode, virtualAuthenticator } from "./helpers";
import { briefCouncil } from "./scenarios";

// One test on the test's own page: the owner's sign-in code is one-time and the key it makes lives in this page's
// virtual authenticator, so the scenarios that need the owner follow the sign-in as steps — and a failure keeps
// its trace and screenshot as any test's page does.
test("test cabinet login and narrow-screen form remain usable; the council of a brief ends clean", async ({ page }) => {
  // The namespace has no external interface. Defense in depth: abort other HTTP origins.
  await page.route("**/*", (route) => {
    const url = new URL(route.request().url());
    return url.origin === "http://localhost:8765" ? route.continue() : route.abort();
  });

  await test.step("login, a passkey, a card at 360/900/1440", async () => {
    await virtualAuthenticator(page);
    for (const width of [360, 900, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/login");
      await expect(page.getByRole("button", { name: "Войти", exact: true })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await loginByCode(page, "owner", codes().owner);
    await expect(page.getByRole("button", { name: "Создать ключ" })).toBeVisible();
    await page.getByRole("button", { name: "Создать ключ" }).click();
    await expect(page.getByText("Ключ добавлен")).toBeVisible();
    await page.getByRole("button", { name: "Готово" }).click();
    const created = await api(page, "POST", "/api/cards", { title: "Isolated smoke card", project: "digipaddock" });
    expect(created.status).toBe(201);
    await page.goto(`/cards/${created.body.id}`);
    await expect(page.getByRole("heading", { name: "Isolated smoke card" })).toBeVisible();
  });

  // The brief's council: the analyst hands a brief in, two critics read it, the owner starts the work.
  await test.step("the council of a brief: the hand-in, two critics, the owner's start", async () => {
    await briefCouncil(page, "Isolated council card");
  });
});
