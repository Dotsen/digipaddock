import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import type { CardCompact, Decision } from "../types";
import { actMany, canSelect, groupByType, inbox, type InboxRow } from "./inbox.svelte";

function decision(id: string, type: string, over: Partial<Decision> = {}): Decision {
  return {
    id,
    type,
    card: "KD-1",
    project: "admin",
    state: "open",
    step_up: type !== "promote" && type !== "question",
    decide_permission: `decide.${type}`,
    payload: {},
    created: "2026-10-15T11:00:00Z",
    expires: "",
    requested_by: "agent:sysadmin",
    decided_by: "",
    decided_at: "",
    signature: null,
    reply: "",
    question: "",
    ...over,
  } as Decision;
}

const row = (d: Decision, stale = false): InboxRow => ({ decision: d, view: { stale }, card: undefined });

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("inbox", () => {
  it("groups decisions by type, critical ones first", () => {
    const rows = [
      row(decision("a", "admin_plan")),
      row(decision("b", "promote")),
      row(decision("c", "admin_plan")),
      row(decision("d", "admin_critical")),
    ];
    expect(groupByType(rows).map((g) => [g.type, g.rows.map((r) => r.decision.id)])).toEqual([
      ["admin_critical", ["d"]],
      ["admin_plan", ["a", "c"]],
      ["promote", ["b"]],
    ]);
  });

  it("selects several decisions of one type only, never a stale one", () => {
    const plan = row(decision("a", "admin_plan"));
    const other = row(decision("b", "admin_plan"));
    expect(canSelect([], plan)).toBe(true);
    expect(canSelect([plan], other)).toBe(true);
    expect(canSelect([plan], row(decision("c", "promote")))).toBe(false);
    expect(canSelect([], row(decision("d", "admin_plan"), true))).toBe(false);
  });

  it("canSelect skips pr_approve: a pull request is signed alone, with its mode", () => {
    expect(canSelect([], row(decision("e", "pr_approve")))).toBe(false);
  });

  it("loads the inbox with each decision's view and card", async () => {
    const plan = decision("a", "admin_plan");
    const question = decision("q", "question", { card: "KD-2", project: "digipaddock" });
    const review = { id: "KD-3", status: "Review", title: "R" };
    const fetch = vi.fn(async (url: string) => {
      if (url === "/api/inbox") return json({ decisions: [plan], questions: [question], review: [review] });
      if (url === "/api/decisions?state=open")
        return json([
          { decision: plan, view: { steps: [{}], stale: false } },
          { decision: question, view: { text: "Как?" } },
          { decision: decision("x", "promote"), view: {} },
        ]);
      if (url === "/api/cards") return json([{ id: "KD-1", title: "План" }, { id: "KD-2", title: "Вопрос" }]);
      throw new Error(url);
    });
    vi.stubGlobal("fetch", fetch);
    await inbox.load();
    expect(inbox.decisions).toHaveLength(1);
    expect(inbox.decisions[0]).toMatchObject({ view: { steps: [{}] }, card: { title: "План" } });
    expect(inbox.questions[0]).toMatchObject({ view: { text: "Как?" }, card: { title: "Вопрос" } });
    expect(inbox.review.map((c) => c.id)).toEqual(["KD-3"]);
  });

  it("moves review cards one by one, each with its revision, and reports every card", async () => {
    const cards = [
      { id: "KD-1", revision: 4 },
      { id: "KD-2", revision: 7 },
      { id: "KD-3", revision: 1 },
    ] as CardCompact[];
    const post = vi
      .fn()
      .mockResolvedValueOnce({ card: { id: "KD-1", revision: 5, status: "In Progress" }, commands: [] })
      .mockRejectedValueOnce(new ApiError(409, "conflict", "stale"))
      .mockRejectedValueOnce(new ApiError(403, "refused", "no"));
    const results = await actMany(cards, "return", "Нет теста", post);
    expect(post.mock.calls).toEqual([
      ["/api/cards/KD-1/act", { action: "return", revision: 4, comment: "Нет теста" }],
      ["/api/cards/KD-2/act", { action: "return", revision: 7, comment: "Нет теста" }],
      ["/api/cards/KD-3/act", { action: "return", revision: 1, comment: "Нет теста" }],
    ]);
    expect(results[0].updated?.status).toBe("In Progress");
    expect(results.map((r) => [r.card, r.outcome])).toEqual([
      ["KD-1", "ok"],
      ["KD-2", "conflict"],
      ["KD-3", "error"],
    ]);
  });
});
