// scenarios the browser tests and the sealed `kd-e2e` suite (`isolated-smoke.spec.ts`) both play, each a
// function over a signed-in owner's page. They wait for states, never for time.
import { expect, type Page } from "@playwright/test";
import { api, executor, getJson } from "./helpers";

const BRIEF = "Goal: a weekly digest of the cards closed.\n\nSource: the board.\nFor: the owner, on Monday morning.";
const COUNCIL = { brief: { critics: ["claude:opus", "claude:sonnet"], rounds: 2 } };

/** The council of a brief from the analyst's hand-in to the owner's start: the owner
 * puts a council of two on a card of the analyst, the analyst writes the brief and hands it in, both critics read
 * it (the dev cabinet's host plays them: nothing found), the council's block says "clean", the brief tab names the
 * round, and "Approve the brief and start" starts the work. The card's id. */
export async function briefCouncil(page: Page, title: string): Promise<string> {
  const created = await api(page, "POST", "/api/cards", { title, project: "digipaddock" });
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const id: string = created.body.id;
  const owned = await api(page, "PATCH", `/api/cards/${id}`, {
    revision: created.body.revision,
    changes: { assignee: "@analyst", labels: ["spec:task-brief"], council: COUNCIL },
  });
  expect(owned.status, JSON.stringify(owned.body)).toBe(200);

  // the analyst's turn, as its kd-task writes it: the brief, then the hand-in to the developer
  const draft = await getJson(page, `/api/cards/${id}`);
  await executor(page, {
    op: "worker.edit", agent: "analyst", card: id, revision: draft.revision,
    description: BRIEF, ac: ["The digest lists every card closed in the week", "It arrives on Monday before 09:00"],
  });
  await executor(page, {
    op: "worker.handoff", agent: "analyst", card: id, to: "@dev", spec: "implementer",
    add_label: ["brief-ready"], comment: "Brief ready: v1",
  });

  // both critics read the version; the council ends with nothing found
  await expect
    .poll(async () => (await getJson(page, `/api/cards/${id}/councils`)).councils[0]?.state, { timeout: 30_000 })
    .toBe("clean");
  await page.goto(`/cards/${id}`);
  const block = page.getByRole("region", { name: "Совет · ТЗ ред. 1 · круг 1 из 2" });
  await expect(block).toBeVisible();
  await expect(block).toContainText("чисто");
  const runs = block.locator(".run");
  await expect(runs).toHaveCount(2);
  // a critic is named with the version its alias served, and its line names the model that read
  await expect(runs.nth(0)).toContainText("Claude Opus 5.5 · подписка");
  await expect(runs.nth(0)).toContainText("claude-opus-5-5");
  await expect(runs.nth(0)).toContainText("готово");
  await expect(runs.nth(1)).toContainText("Claude Sonnet 5.5 · подписка");
  await expect(runs.nth(1)).toContainText("claude-sonnet-5-5");
  await expect(runs.nth(1)).toContainText("готово");
  await expect(block.getByRole("button", { name: "Отменить совет" })).toHaveCount(0); // it is over

  // the brief tab names the round that read the version; the owner starts the work
  await page.getByRole("tab", { name: "ТЗ" }).click();
  await expect(page.locator(".versions > li").first()).toContainText("круг 1");
  const start = page.getByRole("button", { name: "Одобрить ТЗ и запустить" });
  await expect(start).toBeEnabled();
  await start.click();
  await expect.poll(async () => (await getJson(page, `/api/cards/${id}`)).status).toBe("To Do");
  const brief = await getJson(page, `/api/cards/${id}/brief`);
  expect(brief.versions.at(-1).approved_by).toBe("user:owner");
  return id;
}
