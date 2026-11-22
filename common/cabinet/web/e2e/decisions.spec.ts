// Scenario 3: decisions from the inbox — a sysadmin plan signed alone for 72 hours, two plans in one
// signature, a critical command signed on its own, an agent's question answered. Runs on a phone too.
import { expect, type Page } from "@playwright/test";
import { getJson, ownerTest as test } from "./helpers";

async function decisionsOf(page: Page, card: string) {
  return (await getJson(page, `/api/decisions?state=all&card=${card}`)).map((item: any) => item.decision);
}

test("plans, a batch, a critical command and a question", async ({ page, owner }) => {
  expect(owner.id).toBeTruthy(); // the owner's passkey is on this page

  // a plan alone: the command opened, a term of 72 hours, a signature
  await page.goto("/inbox");
  await page.getByRole("button", { name: /Refresh the package indexes, restart nginx/ }).click();
  await page.waitForURL(/\/cards\/KD-\d+\?decision=/);
  const plan = new URL(page.url()).pathname.split("/").pop()!;
  const command = page.locator("details.cmd").first();
  await command.locator("summary").click();
  await expect(command).toHaveAttribute("open", "");
  await page.getByRole("button", { name: "Подписать", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Срок одобрения").selectOption("72");
  await dialog.getByRole("button", { name: "Подписать" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText(/Одобрено/).first()).toBeVisible();
  const [signed] = await decisionsOf(page, plan);
  expect(signed.state).toBe("approved");
  expect(signed.signature).toBeTruthy();
  expect((await getJson(page, `/api/cards/${plan}`)).status).toBe("To Do");

  // two plans, one signature
  await page.goto("/inbox");
  await page.getByRole("button", { name: "Выбрать" }).click();
  await page.getByRole("button", { name: /Start the queue worker/ }).click();
  await page.getByRole("button", { name: /Refresh the package lists/ }).click();
  await page.getByRole("button", { name: "Подписать: 2" }).click();
  await expect(dialog.getByText("Пакет: 2")).toBeVisible();
  await dialog.getByRole("button", { name: "Подписать" }).click();
  await expect(dialog).toBeHidden();
  const approved = (await getJson(page, "/api/decisions?state=approved")).map((item: any) => item.decision);
  const batch = approved.filter((d: any) => d.type === "admin_plan" && d.card !== plan);
  expect(batch).toHaveLength(2);
  expect(batch[0].batch_id).toBeTruthy();
  expect(batch[1].batch_id).toBe(batch[0].batch_id);

  // a critical command: its own signature, once
  await page.goto("/inbox");
  await page.getByRole("button", { name: /Remove a peer that no longer answers/ }).click();
  await page.waitForURL(/\/cards\/KD-\d+\?decision=/);
  const critical = new URL(page.url()).pathname.split("/").pop()!;
  await expect(page.getByText("одноразово, 30 мин").first()).toBeVisible();
  await page.getByRole("button", { name: "Подписать", exact: true }).click();
  await expect(dialog.getByLabel("Срок одобрения")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Подписать" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText(/Одобрено/).first()).toBeVisible();
  expect((await decisionsOf(page, critical))[0].state).toBe("approved");

  // an agent's question: answered in words
  await page.goto("/inbox");
  await page.getByRole("button", { name: /Show the card's draft/ }).click();
  await page.waitForURL(/\/cards\/KD-\d+\?decision=/);
  // the question's «reply» hands the answer to the thread's field
  await page.getByRole("button", { name: "Ответить" }).click();
  await page.getByPlaceholder("Ваш ответ").fill("Только владельцу");
  await page.getByRole("button", { name: "Отправить" }).click();
  await expect(page.getByText("Ответ: Только владельцу")).toBeVisible();
});
