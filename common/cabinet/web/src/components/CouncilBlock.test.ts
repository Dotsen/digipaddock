// a council as one block of the card's thread — its round and standing, each critic's card, the findings
// with the writer's answers; whoever manages councils cancels an open one (after a confirmation) or edits the
// composition. a finding's row says what goes wrong, with where under it; how grave it is and how its
// writer answered are icons, named for a reader and in the opened finding.
import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { council, finding, round, run } from "../lib/council.fixtures";
import { setLocale } from "../lib/i18n.svelte";
import { models } from "../lib/stores/models.svelte";
import CouncilBlock from "./CouncilBlock.svelte";

beforeEach(() => {
  setLocale("ru");
  models.list = [
    { id: "claude:opus", title: "Claude Opus", provider: "subscription", enabled: 1, key: true, experimental: false, builtin: true },
    { id: "or-x", title: "GPT-5 via OpenRouter", provider: "openrouter", enabled: 1, key: true, experimental: true },
  ];
});
afterEach(() => {
  cleanup();
  models.reset();
});

const text = () => (document.body.textContent ?? "").replace(/\s+/g, " ");
const flat = (el: Element | null | undefined) => (el?.textContent ?? "").replace(/\s+/g, " ").trim();
const label = (el: Element | null | undefined) => el?.getAttribute("aria-label");

describe("CouncilBlock", () => {
  it("names the round, the standing and each critic's card with its numbers", () => {
    render(CouncilBlock, { council: council() });
    expect(text()).toContain("Совет · ТЗ ред. 1 · круг 1 из 3");
    expect(text()).toContain("ждёт ответов @analyst: 1");
    const [opus, gpt] = [...document.querySelectorAll(".run")].map((el) => (el.textContent ?? "").replace(/\s+/g, " "));
    expect(opus).toContain("Claude Opus · подписка");
    expect(opus).toContain("требует правок");
    expect(opus).toContain("важн. 2");
    expect(opus).toContain("мелк. 1");
    expect(opus).not.toContain("крит."); // a weight the critic found nothing of is not counted
    expect(opus).toContain("$0.12");
    expect(opus).toContain("та же модель");
    expect(gpt).toContain("GPT-5 via OpenRouter");
    expect(gpt).toContain("готово");
    expect(gpt).not.toContain("та же модель");
    expect(gpt).not.toContain("$"); // nothing spent is not «$0.00»
  });

  // the model that really read, as the usage ledger names it
  it("a critic's line names the model that served it", () => {
    const runs = [
      run({ served: ["claude-opus-5-5", "claude-haiku-4-5"] }),
      run({ n: 2, model: "or-x", served: ["or-x"] }),
      run({ n: 3, state: "queued", verdict: "" }),
    ];
    render(CouncilBlock, { council: council({ rounds_list: [round({ runs })] }) });
    const lines = [...document.querySelectorAll(".run")];
    expect((lines[0].textContent ?? "").replace(/\s+/g, " ")).toContain("Claude Opus · подписка claude-opus-5-5 +1");
    expect(lines[0].querySelector(".served")?.getAttribute("title")).toBe("Отработало на: claude-opus-5-5, claude-haiku-4-5");
    expect(lines[1].querySelector(".served")).toBeNull(); // a cabinet model's own name is not said twice
    expect(lines[2].querySelector(".served")).toBeNull(); // nothing in the ledger yet
  });

  it("the current round is open, an earlier one folded", () => {
    const two = council({
      state: "running",
      round: 2,
      rounds_list: [round({ runs: [run({ verdict: "needs_changes" })] }), round({ round: 2, version: 2, state: "running", ended: "", runs: [run({ state: "queued", verdict: "" }), run({ n: 2, model: "or-x", state: "done" })] })],
    });
    render(CouncilBlock, { council: two });
    expect(text()).toContain("Совет · ТЗ ред. 2 · круг 2 из 3");
    expect(text()).toContain("читают критики: 1 из 2");
    const rounds = [...document.querySelectorAll<HTMLDetailsElement>("details.round")];
    expect(rounds.map((d) => d.open)).toEqual([false, true]);
    expect(rounds[1].textContent).toContain("в очереди");
  });

  // a folded round still says what it found
  it("a round's line counts what its critics found, by weight", () => {
    render(CouncilBlock, { council: council() });
    const counts = [...document.querySelectorAll("details.round > summary .count")];
    expect(counts.map(label)).toEqual(["важная: 2", "мелкая: 1"]);
    expect(counts.map(flat)).toEqual(["2", "1"]);
  });

  // and what became of it — the writer's answers stand on the findings of the round they answer
  it("a round's line counts how its findings stand with the writer", () => {
    render(CouncilBlock, { council: council() });
    const tallies = [...document.querySelectorAll("details.round > summary .tally")];
    expect(tallies.map(label)).toEqual(["отклонено: 1", "без ответа: 1"]); // the minor one holds nothing
    expect(tallies.map((el) => el.getAttribute("title"))).toEqual(["отклонено: 1", "без ответа: 1"]);
    expect(tallies.map(flat)).toEqual(["1", "1"]);
    cleanup();
    const answered = run({ findings: [
      finding({ answer: "fixed" }), finding({ id: "r1.f2", answer: "fixed" }),
      finding({ id: "r1.f3", answer: "later", card: "KD-77", note: "next stage" }),
    ] });
    render(CouncilBlock, { council: council({ rounds_list: [round({ runs: [answered] })] }) });
    expect([...document.querySelectorAll("details.round > summary .tally")].map(label)).toEqual(["исправлено: 2", "позже: 1"]);
    cleanup();
    render(CouncilBlock, { council: council({ rounds_list: [round({ runs: [run()] })] }) });
    expect(document.querySelector("details.round > summary .tallies")).toBeNull(); // nothing found: nothing to count
  });

  it("a finding's row says what goes wrong and where; its weight and the writer's answer are icons", () => {
    render(CouncilBlock, { council: council() });
    const [rejected, open, minor] = [...document.querySelectorAll<HTMLDetailsElement>("details.finding")];
    const row = rejected.querySelector("summary");
    expect(flat(row)).toBe("a request hangs and the owner waits forever Где: AC 1"); // what, then where — no words more
    expect(label(row?.querySelector(".sev"))).toBe("важная");
    expect(row?.querySelector(".sev")?.getAttribute("title")).toBe("важная");
    expect(label(row?.querySelector(".ans"))).toBe("Ответ автора: отклонено");
    expect(row?.querySelector(".ans")?.getAttribute("title")).toBe("Ответ автора: отклонено");
    expect(label(open.querySelector("summary .ans"))).toBe("Ответ автора: без ответа");
    // a minor finding without an answer carries no answer mark (a dash read as «rejected»)
    expect(label(minor.querySelector("summary .sev"))).toBe("мелкая");
    expect(minor.querySelector("summary .ans")).toBeNull();
  });

  it("an opened finding names its weight and id, the evidence, the recommendation and the answer in words", () => {
    render(CouncilBlock, { council: council() });
    const [rejected, open, minor] = [...document.querySelectorAll<HTMLDetailsElement>("details.finding")];
    const said = flat(rejected.querySelector(".body"));
    expect(said).toContain("важная · r1.f1");
    expect(said).toContain("the brief names no timeout");
    expect(said).toContain("name the timeout");
    expect(said).toContain("Ответ автора отклонено: by design");
    expect(said).not.toContain("a request hangs"); // the row above already says it
    expect(flat(open.querySelector(".body"))).toContain("Ответ автора без ответа");
    // the writer answers a minor finding too; one left without an answer holds nothing, and says so
    expect(flat(minor.querySelector(".body"))).toContain("Ответ автора нет — мелкая находка сдачу не задерживает");
  });

  it("a critic's findings come the gravest first", () => {
    const mixed = council({
      rounds_list: [round({ runs: [run({ verdict: "needs_changes", findings: [
        finding({ id: "r1.f1", severity: "minor", scenario: "low" }),
        finding({ id: "r1.f2", severity: "critical", scenario: "high" }),
        finding({ id: "r1.f3", scenario: "middle" }),
      ] })] })],
    });
    render(CouncilBlock, { council: mixed });
    expect([...document.querySelectorAll("details.finding .what")].map(flat)).toEqual(["high", "middle", "low"]);
    expect([...document.querySelectorAll("details.finding summary .sev")].map(label)).toEqual(["критичная", "важная", "мелкая"]);
  });

  it("the other answers: fixed, later with its card", () => {
    const answered = council({
      rounds_list: [round({ runs: [run({ verdict: "needs_changes", findings: [
        finding({ answer: "fixed" }),
        finding({ id: "r1.f2", answer: "later", card: "KD-77", note: "next stage" }),
      ] })] })],
    });
    render(CouncilBlock, { council: answered });
    const [fixed, later] = [...document.querySelectorAll<HTMLDetailsElement>("details.finding")];
    expect(label(fixed.querySelector("summary .ans"))).toBe("Ответ автора: исправлено");
    expect(flat(fixed.querySelector("summary"))).not.toContain("исправлено"); // the check mark says it
    expect(flat(fixed.querySelector(".body"))).toContain("Ответ автора исправлено");
    expect(label(later.querySelector("summary .ans"))).toBe("Ответ автора: позже");
    expect(flat(later.querySelector(".body"))).toContain("Ответ автора позже KD-77: next stage");
    expect(later.querySelector(".body a")?.getAttribute("href")).toBe("/cards/KD-77");
    expect(text()).toContain("ждёт новую редакцию от @analyst"); // nothing left to answer
  });

  it("a failed critic says why; a critic's questions stand apart from what it had no access to", () => {
    const asked = council({
      rounds_list: [round({ runs: [
        run({ state: "failed", verdict: "", fail_reason: "no final block" }),
        run({ n: 2, model: "or-x", verdict: "needs_changes", questions: [
          { text: "Which timezone?", options: ["UTC", "the owner's"], recommendation: "the owner's" },
          { text: "the log format", missing: true },
        ] }),
      ] })],
    });
    render(CouncilBlock, { council: asked });
    const [failed, asking] = [...document.querySelectorAll(".run")];
    expect(flat(failed.querySelector(".verdict"))).toBe("не ответил");
    expect(flat(failed)).toContain("no final block");
    const list = asking.querySelector(".questions");
    expect(flat(asking)).toContain("Вопросы критика");
    expect(flat(list)).toContain("Which timezone?");
    expect(flat(list)).toContain("UTC");
    expect(flat(list)).toContain("рекомендует: the owner's");
    expect(flat(list)).not.toContain("the log format");
    expect(flat(asking)).toContain("Не хватило для проверки");
    expect(flat(asking.querySelector(".missing"))).toBe("the log format");
    expect(flat(failed)).not.toContain("Вопросы критика"); // no heading over nothing
    expect(flat(failed)).not.toContain("Не хватило для проверки");
  });

  // icons have no hover on a phone — the block explains them once, with whom the writer owes an answer
  it("the block explains its icons and which findings the writer answers", () => {
    render(CouncilBlock, { council: council() });
    const legend = document.querySelector("details.legend");
    expect(flat(legend?.querySelector("summary"))).toBe("Обозначения");
    const said = flat(legend);
    for (const word of ["критичная", "важная", "мелкая", "исправлено", "отклонено", "позже", "без ответа"]) {
      expect(said).toContain(word);
    }
    expect(legend?.querySelectorAll(".ans")).toHaveLength(4); // «no answer required» is no mark of its own
    expect(said).toContain("Автор отвечает на каждую находку круга.");
    expect(said).toContain("мелкая сдачу не задерживает — без ответа у неё отметки нет.");
    expect(said).toContain("Круг только с мелкими находками завершает совет, и они остаются без ответа.");
    expect(said).toContain("В строке круга — сколько находок каждой важности и что с ними сделал автор.");
    cleanup();
    render(CouncilBlock, { council: council({ rounds_list: [round({ runs: [run()] })] }) });
    expect(document.querySelector("details.legend")).toBeNull(); // nothing found: nothing to explain
  });

  it("without the right there are no buttons", () => {
    render(CouncilBlock, { council: council() });
    expect(screen.queryAllByRole("button")).toEqual([]);
  });

  it("«Отменить совет» asks first and calls back only after the confirmation", async () => {
    const oncancel = vi.fn();
    render(CouncilBlock, { council: council(), canManage: true, oncancel, onedit: vi.fn() });
    await fireEvent.click(screen.getByRole("button", { name: "Отменить совет" }));
    expect(oncancel).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Не отменять" }));
    expect(oncancel).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Да, отменить" })).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Отменить совет" }));
    await fireEvent.click(screen.getByRole("button", { name: "Да, отменить" }));
    expect(oncancel).toHaveBeenCalledTimes(1);
  });

  it("«Изменить состав» opens the composition, for the next round", async () => {
    const onedit = vi.fn();
    render(CouncilBlock, { council: council(), canManage: true, oncancel: vi.fn(), onedit });
    expect(text()).toContain("со следующего круга");
    await fireEvent.click(screen.getByRole("button", { name: "Изменить состав" }));
    expect(onedit).toHaveBeenCalledTimes(1);
  });

  it("an ended council says how it ended and offers nothing", () => {
    render(CouncilBlock, { council: council({ state: "cancelled", end_reason: "owner", ended: "2026-11-10T11:00:00Z" }), canManage: true, oncancel: vi.fn(), onedit: vi.fn() });
    expect(text()).toContain("отменён · владельцем");
    expect(screen.queryAllByRole("button")).toEqual([]);
    cleanup();
    render(CouncilBlock, { council: council({ state: "failed", end_reason: "no_critic" }) });
    expect(text()).toContain("не состоялся · ни один критик не ответил");
    cleanup();
    for (const [state, said] of [["clean", "чисто"], ["settled", "автор отклонил находки"], ["limited", "предел кругов"]]) {
      render(CouncilBlock, { council: council({ state: state as "clean" }) });
      expect(text()).toContain(said);
      cleanup();
    }
    // not the rounds' limit: the writer handed the same brief in past the findings (where the pipeline observes)
    render(CouncilBlock, { council: council({ state: "limited", end_reason: "unanswered" }) });
    expect(text()).toContain("автор сдал ТЗ, не ответив на находки");
    expect(text()).not.toContain("предел кругов");
    cleanup();
  });
});

// the council of the code stands on a pull request's head
describe("CouncilBlock: the code", () => {
  const HEAD = "a".repeat(40);
  const basis = { repo: "acme/web", project: "web", number: 7, head: HEAD, target: "main", url: "https://forge/pulls/7" };
  const found = run({ verdict: "with_fixes", counts: { critical: 0, major: 1, minor: 0 }, findings: [finding()] });
  const code = (over: Parameters<typeof council>[0] = {}) =>
    council({ kind: "code", basis, rounds_list: [round({ version: null, head: HEAD, runs: [found] })], ...over });

  it("names the pull request, links to it and says which head each round read", () => {
    render(CouncilBlock, { council: code(), assignee: "@dev" });
    expect(text()).toContain("Совет · код · PR #7 · круг 1 из 3");
    expect(screen.getByRole("link", { name: "открыть в Forgejo ↗" }).getAttribute("href")).toBe("https://forge/pulls/7");
    expect(text()).toContain("Круг 1 · head aaaaaaa");
    expect(text()).toContain("ждёт ответов @dev: 1");
    expect(document.getElementById("council-c1")).toBeTruthy(); // the pull request's decision links here
  });

  it("answered findings wait for a commit; a round a new head cut short says so", () => {
    const answered = run({ ...found, findings: [finding({ answer: "fixed" })] });
    const two = code({
      round: 2,
      rounds_list: [
        round({ version: null, head: "b".repeat(40), state: "superseded", runs: [run({ state: "cancelled", verdict: "" })] }),
        round({ round: 2, version: null, head: HEAD, runs: [answered] }),
      ],
    });
    render(CouncilBlock, { council: two, assignee: "@dev" });
    expect(text()).toContain("ждёт новый коммит от @dev");
    expect(text()).toContain("прерван новым коммитом");
    expect(text()).not.toContain("редакци");
  });

  it("an open council shows the pull request it holds back, with nothing to decide", async () => {
    const pull = {
      repo: "acme/web", number: 7, title: "KD-1: a change", url: "https://forge/pulls/7", head: HEAD, release: false,
      target: "main", effect: "", checks: ["static"], state: "open", stale: false, truncated: false, cage: [],
      files: [{ path: "src/app.py", status: "modified", additions: 150, deletions: 0 }],
    };
    const body = JSON.stringify({ diff: "", truncated: false, head: HEAD });
    const fetch = vi.fn(async () => new Response(body, { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);
    render(CouncilBlock, { council: code({ state: "running", pull }), assignee: "@dev" });
    expect(text()).toContain("Решение по PR откроется, когда совет закончится");
    expect(text()).toContain("src/app.py");
    expect(text()).toContain("Проверки: static");
    expect(screen.queryByRole("button", { name: /Одобрить|Отклонить/ })).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Показать дифф" }));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    expect((fetch.mock.calls[0] as unknown as [string])[0]).toBe("/api/cards/KD-5/councils/c1/diff");
    cleanup();
    render(CouncilBlock, { council: code({ state: "clean" }) }); // ended: the decision shows it
    expect(text()).not.toContain("Решение по PR откроется");
    vi.unstubAllGlobals();
  });

  it("cancelling says what it does to the pull request; an end by the merge is named", async () => {
    render(CouncilBlock, { council: code({ state: "running" }), canManage: true });
    await fireEvent.click(screen.getByRole("button", { name: "Отменить совет" }));
    expect(text()).toContain("решение по PR откроется без их ревью");
    expect(text()).not.toContain("ТЗ останется");
    cleanup();
    render(CouncilBlock, { council: code({ state: "cancelled", end_reason: "pr_closed" }) });
    expect(text()).toContain("отменён · PR слит или закрыт");
  });
});
