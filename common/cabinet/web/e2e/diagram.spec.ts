// a ```mermaid block of a card's description is a drawing in the diagram frame — a sandboxed document
// that has no part of the cabinet's session; a block mermaid cannot read stays as its text with the reason; the
// worker precaches neither the frame nor mermaid.
import { expect } from "@playwright/test";
import { api, ownerTest as test } from "./helpers";

const BRIEF = [
  "Подход",
  "---",
  "Запрос идёт по типу учётки, KD-1.",
  "",
  "```mermaid",
  "flowchart TD",
  "  A[Запрос партнёра] --> B{Тип учётки?}",
  "  B -- api --> C[Рестораны]",
  "  B -- sales --> D[Клиенты]",
  "```",
  "",
  "Схема с ошибкой:",
  "",
  "```mermaid",
  "flowchart TD",
  "  A -->",
  "  B{{{",
  "```",
  "",
  "После схем.",
].join("\n");

test("a diagram of a description is drawn in its sandboxed frame, a broken one stays as text", async ({ page }) => {
  await page.goto("/");
  const created = await api(page, "POST", "/api/cards", { title: "Схема в ТЗ из e2e", project: "digipaddock" });
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const card = created.body;
  const edited = await api(page, "PATCH", `/api/cards/${card.id}`, { revision: card.revision, changes: { description: BRIEF } });
  expect(edited.status, JSON.stringify(edited.body)).toBe(200);

  await page.goto(`/cards/${card.id}?tab=brief`);
  const text = page.locator(".md").filter({ hasText: "Запрос идёт по типу учётки" });
  await expect(text.locator("h2")).toHaveText("Подход");
  const frames = text.locator("iframe");
  await expect(frames).toHaveCount(2);
  await expect(frames.first()).toHaveAttribute("sandbox", "allow-scripts");

  // the first block is a drawing: an <svg> inside the frame, the frame as tall as the drawing
  const drawing = text.frameLocator("iframe").first().locator("#out svg");
  await expect(drawing).toBeVisible();
  await expect(drawing.getByText("Запрос партнёра")).toBeVisible();
  expect((await frames.first().boundingBox())?.height).toBeGreaterThan(100);
  await expect(text.getByText("Рисуем схему…")).toHaveCount(0);
  // the frame is another origin: the cabinet's session and storage are not its own
  const frame = page.frames().find((one) => one.url().endsWith("/diagram.html"));
  expect(await frame?.evaluate(() => self.origin)).toBe("null");
  expect(await frame?.evaluate(() => {
    try {
      return document.cookie;
    } catch (error) {
      return (error as Error).name;
    }
  })).toBe("SecurityError");

  // the second block is its own text and the reason
  await expect(text.getByText(/^Схема не нарисована, ниже — её текст · Parse error on line \d+$/)).toBeVisible();
  await expect(text.locator("pre code")).toHaveText("flowchart TD\n  A -->\n  B{{{");
  await expect(frames.nth(1)).toBeHidden();
  await expect(text.getByText("После схем.")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  // the app's worker leaves the frame to the server and does not carry mermaid in its precache
  const worker = await (await page.request.get("/sw.js")).text();
  expect(worker).not.toMatch(/assets\/(mermaid|diagram-frame)-/);
  expect(worker).not.toContain('"diagram.html"');
});
