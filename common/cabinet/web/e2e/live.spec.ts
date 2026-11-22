// Two pages of the owner on one card. An agent's comment and question reach both without
// a reload and once; the question is answered from the thread's field (multi-line, Ctrl+Enter); a draft
// survives a tab switch; an edit under the description editor asks the person; a page that was offline
// catches up without a reload.
import { expect, type Locator, type Page } from "@playwright/test";
import { api, cardByTitle, executor, getJson, ownerTest as test } from "./helpers";

const TITLE = "Cache the board's responses";
const thread = (page: Page) => page.locator(".thread");
const field = (page: Page): Locator => page.locator(".composer textarea");

test("a live thread in two tabs, a question answered from the field, drafts and an editor conflict", async ({ page, browser }) => {
  await page.goto("/board");
  const { id } = await cardByTitle(page, TITLE);
  const second = await page.context().newPage();
  for (const p of [page, second]) {
    await p.goto(`/cards/${id}`);
    await expect(p.getByRole("heading", { name: new RegExp(TITLE) })).toBeVisible();
  }

  // AC01: an agent's comment shows on both pages, once, without a reload
  await executor(page, { op: "worker.edit", agent: "dev", card: id, comment: "шаг 1 из e2e" });
  for (const p of [page, second]) {
    await expect(thread(p).getByText(/шаг 1 из e2e/)).toHaveCount(1);
  }

  // AC02: an agent's question shows on both pages
  await executor(page, {
    op: "decision.open",
    type: "question",
    agent: "dev",
    card: id,
    payload: { text: "Какой вариант кеша?" },
  });
  for (const p of [page, second]) {
    await expect(thread(p).getByText("Какой вариант кеша?")).toBeVisible();
    await expect(p.getByRole("button", { name: /Ответ на вопрос @dev/, pressed: true })).toBeVisible();
  }

  // AC14: a draft typed on page 1 survives switching the tab away and back
  await field(page).fill("черновик первой вкладки");
  await page.getByRole("tab", { name: "ТЗ" }).click();
  await page.getByRole("tab", { name: "Обсуждение" }).click();
  await expect(field(page)).toHaveValue("черновик первой вкладки");

  // AC15: page 2 answers from the field, on two lines; Enter breaks the line, Ctrl+Enter sends
  await field(second).fill("вариант А");
  await field(second).press("Enter");
  await field(second).pressSequentially("и пояснение");
  await field(second).press("Control+Enter");
  await expect(field(second)).toHaveValue("");
  await expect(page.getByText("Отвечено", { exact: false }).first()).toBeVisible();
  const decisions = (await getJson(page, `/api/decisions?state=all&card=${id}`)).map((item: any) => item.decision);
  const question = decisions.find((d: any) => d.type === "question" && d.payload.text === "Какой вариант кеша?");
  expect(question.state).toBe("approved");
  expect(question.reply).toBe("вариант А\nи пояснение");

  // AC12: page 1 edits the description, page 2 saves another one; page 1 is asked, nothing is overwritten
  await page.getByRole("tab", { name: "ТЗ" }).click();
  await page.getByRole("button", { name: "Править" }).click();
  await page.getByLabel("Описание и AC", { exact: true }).fill("Текст первой вкладки");
  const card = await getJson(second, `/api/cards/${id}`);
  const saved = await api(second, "PATCH", `/api/cards/${id}`, {
    revision: card.revision,
    changes: { description: "Описание со второй вкладки" },
  });
  expect(saved.status).toBe(200);
  const editor = page.locator(".editor");
  await expect(editor.getByText("Описание изменилось, пока вы его правили")).toBeVisible();
  await expect(editor.getByRole("button", { name: "Сохранить", exact: true })).toHaveCount(0);
  await editor.getByRole("button", { name: "Взять новый текст" }).click();
  await expect(page.getByLabel("Описание и AC", { exact: true })).toHaveValue("Описание со второй вкладки");
  await editor.getByRole("button", { name: "Отмена" }).click();
  await page.getByRole("tab", { name: "Обсуждение" }).click();

  // AC04: offline, a comment is written elsewhere; back online, it shows without a reload
  const elsewhere = await browser.newContext({
    storageState: await page.context().storageState(),
    baseURL: String(test.info().project.use.baseURL),
  });
  const other = await elsewhere.newPage();
  await other.goto("/inbox");
  await page.context().setOffline(true);
  await expect(page.getByText("Нет связи — показаны сохранённые данные")).toBeVisible();
  await executor(other, { op: "worker.edit", agent: "dev", card: id, comment: "написано, пока не было связи" });
  await page.context().setOffline(false);
  await expect(thread(page).getByText(/написано, пока не было связи/)).toHaveCount(1);
  await expect(page.getByText("Восстанавливаем связь…")).toHaveCount(0);
  await expect(page.getByText("Нет связи — показаны сохранённые данные")).toHaveCount(0);
  await elsewhere.close();
  await second.close();
});
