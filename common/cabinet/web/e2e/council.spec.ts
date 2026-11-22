// the models of the Claude subscription in the cabinet's lists and the council's composition — the
// owner puts a card on a subscription model, sets its project's default council and the card's own; the card's
// kind wins over the project's. The gathering itself (rounds, critics' jobs) is not part of this stage.
import { expect } from "@playwright/test";
import path from "node:path";
import { api, cardByTitle, getJson, ownerTest as test } from "./helpers";
import { briefCouncil } from "./scenarios";

test("a subscription model on a card; the council of a project and of a card", async ({ page }, info) => {
  await page.goto("/board");
  const created = await api(page, "POST", "/api/cards", { title: "Совет моделей из e2e", project: "digipaddock" });
  expect(created.status).toBe(201);
  const id: string = created.body.id;

  // the subscription's models come first in the list: rows of the catalog, not the owner's to change
  await page.goto("/settings/models");
  const first = page.locator("ul.models > li").first();
  await expect(first.getByText("Claude Opus", { exact: true })).toBeVisible();
  await expect(first.getByText(/на подписке Claude/)).toBeVisible();
  await expect(first.getByText(/сейчас отвечает claude-opus-5-5/)).toBeVisible(); // what the alias served last
  await expect(first.getByRole("button", { name: "Удалить" })).toHaveCount(0);
  await expect(first.getByRole("button", { name: "Править" })).toHaveCount(0);
  await expect(first.getByLabel("Включена")).toHaveCount(0);
  expect((await api(page, "DELETE", "/api/models/claude%3Aopus")).status).toBe(403);

  // the project's default: one critic of the code
  await page.goto("/settings/projects");
  const project = page.locator("li").filter({ has: page.locator("code", { hasText: /^digipaddock$/ }) });
  await project.getByRole("button", { name: "Править" }).click();
  const code = project.getByRole("group", { name: "код" });
  await code.getByRole("combobox", { name: "Критики" }).click();
  await code.getByRole("option", { name: "Claude Opus 5.5 · подписка" }).click();
  await project.getByRole("button", { name: "Сохранить" }).click();
  await expect
    .poll(async () => (await getJson(page, "/api/projects")).find((p: { id: string }) => p.id === "digipaddock").council)
    .toEqual({ code: { critics: ["claude:opus"], rounds: 3, min_lines: 100 } });

  try {
    await page.goto(`/cards/${id}`);
    // the card on a model of the subscription, by its reference
    await page.getByRole("button", { name: "Модель", exact: true }).click();
    await page.getByRole("option", { name: "Claude Sonnet 5.5 · подписка" }).click();
    await expect.poll(async () => (await getJson(page, `/api/cards/${id}`)).model).toBe("claude:sonnet");
    await expect(page.getByText("нет ключа провайдера на хосте")).toHaveCount(0); // it needs no provider key

    // the council in force is the project's; the card's own brief council: one model reading twice, two rounds
    const chip = page.getByRole("button", { name: "Совет моделей", exact: true });
    await expect(chip).toContainText("код: 1");
    await chip.click();
    const sheet = page.getByRole("dialog", { name: "Совет моделей" });
    const brief = sheet.getByRole("group", { name: "ТЗ" });
    // the project names no critics of a brief — nothing to choose first, the models are picked at once;
    // the list stays open, and the same model picked again is one more read
    await expect(brief.getByLabel("Состав")).toHaveCount(0);
    await brief.getByRole("combobox", { name: "Критики" }).fill("opus");
    await brief.getByRole("option", { name: "Claude Opus 5.5 · подписка" }).click();
    await brief.getByRole("combobox", { name: "Критики" }).fill("opus");
    await brief.getByRole("option", { name: "Claude Opus 5.5 · подписка" }).click();
    await page.keyboard.press("Escape"); // the list's own: the sheet stays
    await expect(brief.getByRole("listbox", { name: "Критики" })).toBeHidden();
    await brief.getByLabel("Кругов").fill("2");
    await page.screenshot({ path: path.join(info.outputDir, "council-editor.png") });
    await sheet.getByRole("button", { name: "Сохранить" }).click();
    await expect(sheet).toHaveCount(0);
    await expect(chip).toContainText("ТЗ: 2 · код: 1");
    expect((await getJson(page, `/api/cards/${id}`)).council).toEqual({ brief: { critics: ["claude:opus", "claude:opus"], rounds: 2 } });

    // "no council" for the code on the card: the project's critic does not read this card
    await chip.click();
    await sheet.getByRole("group", { name: "код" }).getByLabel("Состав").selectOption("off");
    await sheet.getByRole("button", { name: "Сохранить" }).click();
    await expect(chip).toContainText("ТЗ: 2");
    await expect(chip).not.toContainText("код");
    await page.screenshot({ path: path.join(info.outputDir, "council-card.png") });
  } finally {
    // the seed's project as the other specs know it
    expect((await api(page, "PATCH", "/api/projects/digipaddock", { council: {} })).status).toBe(200);
  }
});

// a gathering in the card's thread. The seed's «Nightly backup check» is under its council: the first
// round was read by two critics, the analyst rejected one finding and asked the owner about another. The council
// is one block — no line per review — with each critic's verdict and numbers; a finding's row says what goes
// wrong (its weight and the writer's answer are icons) and opens with what the critic said and the
// writer's answer in words; the owner sees the controls; the brief tab names the round per version.
test("a council is one block of the card's thread, with its critics, findings and answers", async ({ page }, info) => {
  await page.goto("/inbox");
  const card = await cardByTitle(page, "Nightly backup check");
  await page.goto(`/cards/${card.id}`);

  const block = page.getByRole("region", { name: "Совет · ТЗ ред. 1 · круг 1 из 3" });
  await expect(block).toBeVisible();
  await expect(block).toContainText("ждёт ответов @analyst: 1");
  const runs = block.locator(".run");
  await expect(runs).toHaveCount(2);
  await expect(runs.nth(0)).toContainText("Claude Opus 5.5 · подписка");
  await expect(runs.nth(0)).toContainText("claude-opus-5-5"); // the model that served the read
  await expect(runs.nth(0)).toContainText("требует правок");
  await expect(runs.nth(0)).toContainText("важн. 2");
  await expect(runs.nth(0)).toContainText("мелк. 1");
  await expect(runs.nth(0)).toContainText("$0.12");
  await expect(runs.nth(1)).toContainText("готово");
  await expect(page.getByText(/brief council round 1/)).toHaveCount(0); // the reviews are no lines of their own

  // a finding's row says what goes wrong, with where under it; its weight and the answer are icons with names
  const finding = block.locator("details.finding").first();
  const row = finding.locator("summary");
  await expect(row).toContainText("the check itself stops running and nobody learns of it for weeks");
  await expect(row).toContainText("Где:");
  await expect(row.getByRole("img", { name: "важная", exact: true })).toBeVisible();
  await expect(row.getByRole("img", { name: "Ответ автора: отклонено" })).toBeVisible();
  await expect(row).not.toContainText("отклонено");
  // opened, it says why, what to do and what the writer answered — in words
  await expect(finding.getByText("Рекомендация")).toBeHidden();
  await row.click();
  await expect(finding.getByText("Рекомендация")).toBeVisible();
  await expect(finding).toContainText("отклонено: the owner asked for a notice on a failure only");
  await expect(block.locator("details.finding").nth(1).getByRole("img", { name: "Ответ автора: без ответа" })).toBeVisible();
  // a minor finding without an answer carries no answer mark; the round's line counts what the
  // writer did with its findings
  await expect(block.locator("details.finding").nth(2).getByRole("img", { name: "мелкая", exact: true })).toBeVisible();
  await expect(block.locator("details.finding").nth(2).getByRole("img", { name: /Ответ автора/ })).toHaveCount(0);
  const line = block.locator("details.round > summary").last();
  await expect(line.getByRole("img", { name: "отклонено: 1" })).toBeVisible();
  await expect(line.getByRole("img", { name: "без ответа: 1" })).toBeVisible();
  await expect(block).toContainText("Where does a restore run?");
  // the icons are explained in the block: a phone has no hover
  await block.getByText("Обозначения").click();
  await expect(block).toContainText("Автор отвечает на каждую находку круга.");
  await expect(block).toContainText("мелкая сдачу не задерживает — без ответа у неё отметки нет.");
  await expect(block).toContainText("В строке круга — сколько находок каждой важности и что с ними сделал автор.");

  // the owner manages it: a cancel is asked again in the block (and not given here), the composition opens
  await block.getByRole("button", { name: "Отменить совет" }).click();
  await expect(block.getByRole("button", { name: "Да, отменить" })).toBeVisible();
  await block.getByRole("button", { name: "Не отменять" }).click();
  await block.getByRole("button", { name: "Изменить состав" }).click();
  const sheet = page.getByRole("dialog", { name: "Совет моделей" });
  await expect(sheet.getByRole("group", { name: "ТЗ" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await page.screenshot({ path: path.join(info.outputDir, "council-block.png"), fullPage: true });

  // the brief tab: the council's standing and the round that read the version
  await page.getByRole("tab", { name: "ТЗ" }).click();
  await expect(page.locator(".state")).toContainText("Совет: круг 1 из 3 · ждёт ответов @analyst: 1");
  await expect(page.locator(".versions > li").first()).toContainText("круг 1: Claude Opus 5.5 · подписка — требует правок, Claude Sonnet 5.5 · подписка — готово");
  expect((await getJson(page, `/api/cards/${card.id}/councils`)).councils[0].state).toBe("needs_writer");
});

// the whole road of a brief under its council — the scenario `kd-e2e run cabinet-smoke` plays too.
test("the council of a brief: from the hand-in to the owner's start", async ({ page }) => {
  await page.goto("/inbox");
  await briefCouncil(page, "Совет по ТЗ из e2e");
});
