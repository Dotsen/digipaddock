import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { session, type Me } from "../lib/session.svelte";
import { agentSession, command } from "../lib/stores/runs.fixtures";
import type { Card, CardAction, CardNext, CardPipeline, Decision, DecisionItem } from "../lib/types";
import NextStep from "./NextStep.svelte";

const decide = vi.hoisted(() => ({ approveDecisions: vi.fn(async () => ({})) }));
vi.mock("../lib/decide", async (original) => ({ ...(await original<object>()), approveDecisions: decide.approveDecisions }));

const me: Me = {
  id: "owner", name: "Владелец", language: "ru", timezone: "Etc/GMT-3", permissions: { "*": ["*"] },
  projects: ["digipaddock"], csrf: "c", push_public_key: "k", second_factor: { passkey: true, totp: false, password: false },
  must_setup: false,
};

const base: Card = {
  id: "KD-5", n: 5, status: "Inbox", assignee: "@dev", project: "digipaddock", title: "Отчёт", labels: [], servers: [],
  priority: "", due: "", parent: "", dependencies: [], revision: 3,
  created: "2026-10-17T10:00:00Z", updated: "2026-10-17T10:00:00Z",
  description: "", acceptance: [], dod: "", plan: "", notes: "", summary: "", created_by: "@owner", thread: 7,
};

function action(over: Partial<CardAction>): CardAction {
  return { id: "start", label: "action.start", primary: true, agent: "@dev", enabled: true, reason: "", params: {},
    message: "", comment: "none", brief_version: null, ...over };
}

function decision(type: string, over: Partial<Decision> = {}, view: Record<string, unknown> = {}): DecisionItem {
  return {
    decision: {
      id: `${type}1`, type, card: "KD-5", project: "digipaddock", state: "open", step_up: false, decide_permission: `decide.${type}`,
      payload: { text: "?" }, created: "2026-10-17T10:00:00Z", expires: "", requested_by: "agent:boss", decided_by: "",
      decided_at: "", signature: null, reply: "", question: "", ...over,
    },
    view: { assignee: "@dev", ...view },
  };
}

/** The backend's next step: the page words it and adds no rule of its own. */
function nxt(what: string, over: Partial<CardNext> = {}): CardNext {
  return { source: "derived", what, who: "-", since: "2026-10-17T10:00:00Z", text: "", params: {}, ...over };
}

function on(d: DecisionItem, what = "wait_decision", params: CardNext["params"] = {}): CardNext {
  return nxt(what, { who: "@owner", decision: { id: d.decision.id, type: d.decision.type }, params });
}

function show(props: Record<string, unknown> = {}) {
  const onact = vi.fn();
  const onchanged = vi.fn();
  render(NextStep, {
    card: base, actions: [], decisions: {}, commands: [], sessions: [], busy: false, now: Date.parse("2026-10-17T10:00:10Z"),
    canControl: true, onact, onchanged, ...props,
  });
  return { onact, onchanged };
}

beforeEach(() => {
  setLocale("ru");
  session.signedIn(me);
  decide.approveDecisions.mockClear();
});
afterEach(() => {
  cleanup();
  session.clear();
});

describe("NextStep", () => {
  it("an open BOSS proposal is the one way to start: approve it, no second start button", async () => {
    const promote = decision("promote");
    const { onchanged } = show({
      card: { ...base, next: on(promote) },
      actions: [action({ label: "action.start_brief", brief_version: 2 }), action({ id: "brief", label: "action.brief_rework", primary: false })],
      decisions: { [promote.decision.id]: promote },
    });
    expect(document.body.textContent).toContain("@boss предлагает взять карточку в работу");
    expect(screen.getAllByRole("button", { name: "Одобрить ТЗ и запустить" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Доработать ТЗ" })).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" }));
    await vi.waitFor(() => expect(decide.approveDecisions).toHaveBeenCalledWith([promote], expect.any(Function)));
    await vi.waitFor(() => expect(onchanged).toHaveBeenCalled());
  });

  it("a proposal while BOSS is off offers no approval and is read again until it is back (KD-9221)", () => {
    vi.useFakeTimers();
    try {
      const promote = decision("promote", {}, { blocked: "boss_off" });
      const { onchanged } = show({ card: { ...base, next: on(promote) }, actions: [action({})], decisions: { [promote.decision.id]: promote } });
      expect(screen.queryByRole("button", { name: "Одобрить ТЗ и запустить" })).toBeNull();
      expect(screen.getByRole("status").textContent).toContain("BOSS сейчас выключен");
      vi.advanceTimersByTime(30_000);
      expect(onchanged).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a stale proposal is not approved from here", () => {
    const promote = decision("promote", {}, { stale: true });
    show({ card: { ...base, next: on(promote) }, actions: [action({})], decisions: { [promote.decision.id]: promote } });
    expect(screen.queryByRole("button", { name: "Одобрить ТЗ и запустить" })).toBeNull();
    expect(screen.getByRole("link", { name: "Открыть предложение" }).getAttribute("href")).toBe("/cards/KD-5?decision=promote1");
  });

  it("a stale proposal never hides what the card waits for (KD-9203)", () => {
    // "The proposal is outdated" took the lead while @analyst waited for the owner's answers
    const promote = decision("promote", {}, { stale: true });
    const q = decision("question", { requested_by: "agent:analyst" });
    show({
      card: { ...base, next: on(q, "wait_decision", { agent: "@analyst" }) },
      actions: [action({})],
      decisions: { [promote.decision.id]: promote, [q.decision.id]: q },
    });
    const leads = [...document.querySelectorAll(".lead")].map((p) => p.textContent);
    expect(leads[0]).toBe("@analyst ждёт вашего ответа");
    expect(screen.getByRole("link", { name: "Открыть предложение" })).toBeTruthy();
  });

  it("while the brief is written: who writes it, where the questions come, what follows (KD-9203)", () => {
    show({
      card: { ...base, assignee: "@analyst", labels: ["spec:task-brief"], next: nxt("brief_writing", { who: "@analyst" }) },
      actions: [action({ agent: "@sysadmin", enabled: false, reason: "brief_writing" })],
    });
    expect(document.querySelector(".lead")?.textContent).toBe("@analyst готовит ТЗ");
    expect(document.body.textContent).toContain(
      "Вопросы придут сюда, в обсуждение. Когда ТЗ будет готово, @boss предложит запуск",
    );
  });

  it("a question on an agent's decision: the agent owes the answer (KD-9203)", () => {
    const promote = decision("promote", { question: "Почему сейчас?" }, { asked_open: true });
    const next = { ...on(promote, "asked", { agent: "@boss" }), who: "@boss" };
    show({ card: { ...base, next }, actions: [action({})], decisions: { [promote.decision.id]: promote } });
    expect(document.querySelector(".lead")?.textContent).toBe("@boss отвечает на ваш вопрос");
    expect(screen.getByRole("button", { name: "Одобрить ТЗ и запустить" })).toBeTruthy(); // still yours to approve
  });

  it("an agent's open question: waiting for the person, with a link to it", () => {
    const q = decision("question", { requested_by: "agent:dev" });
    show({ card: { ...base, status: "In Progress", next: on(q, "wait_decision", { agent: "@dev" }) }, decisions: { [q.decision.id]: q } });
    expect(document.body.textContent).toContain("@dev ждёт вашего ответа");
    expect(screen.getByRole("link", { name: "Перейти к вопросу" }).getAttribute("href")).toBe("/cards/KD-5?decision=question1");
  });

  it("next step points to the PR decision; «Принять» stays off with its reason", () => {
    const pr = decision("pr_approve", { requested_by: "agent:dev", payload: { number: 12 } }, { number: 12 });
    show({
      card: { ...base, status: "Review", next: on(pr, "wait_decision", { number: 12 }) },
      decisions: { [pr.decision.id]: pr },
      actions: [action({ id: "accept", label: "action.accept", enabled: false, reason: "pr_open", params: { pr: 12 } })],
      commands: [command({ state: "done", agent: "dev", session: "s1" })],
    });
    expect(document.body.textContent).toContain("PR #12 ждёт вашего решения");
    expect(screen.getByRole("link", { name: "Перейти к PR" }).getAttribute("href")).toBe("/cards/KD-5?decision=pr_approve1");
    expect((screen.getByRole("button", { name: "Принять" }) as HTMLButtonElement).disabled).toBe(true);
    expect(document.body.textContent).toContain("PR #12 не слит — сначала решение по PR");
    expect(document.body.textContent).toContain("Ждёт вашего ответа");
  });

  it("the lead is the backend's step, never the page's own rule (KD-9204)", () => {
    const q = decision("question", { requested_by: "agent:dev" });
    show({ card: { ...base, status: "Review", next: nxt("review", { who: "@owner" }) }, decisions: { [q.decision.id]: q } });
    expect(document.querySelector(".lead")?.textContent).toBe("На проверке: примите работу или верните её");
    expect(document.body.textContent).not.toContain("ждёт вашего ответа");
  });

  it("any other decision is named by its type and links to itself (KD-9204)", () => {
    const plan = decision("admin_plan", { requested_by: "agent:sysadmin" });
    show({ card: { ...base, status: "In Progress", next: on(plan) }, decisions: { [plan.decision.id]: plan } });
    expect(document.body.textContent).toContain("Ждёт вашего решения: план сисадмина");
    expect(screen.getByRole("link", { name: "Перейти к решению" }).getAttribute("href")).toBe("/cards/KD-5?decision=admin_plan1");
  });

  it("the step's own words: a ready brief, an ended turn (KD-9204)", () => {
    show({ card: { ...base, next: nxt("brief_ready", { who: "@owner", params: { version: 2 } }) } });
    expect(document.body.textContent).toContain("ТЗ ред. 2 готово — одобрите его, чтобы запустить");
    cleanup();
    show({ card: { ...base, status: "In Progress", next: nxt("turn_ended", { params: { agent: "@dev" } }) } });
    expect(document.body.textContent).toContain("Ход @dev закончился, продолжения нет");
  });

  it("a council's step: the critics read, then the writer answers (KD-9267)", () => {
    show({ card: { ...base, next: nxt("council_running", { params: { round: 1, rounds: 3, done: 1, total: 2 } }) } });
    expect(document.body.textContent).toContain("Совет: круг 1 из 3 — ждёт критиков (1 из 2 ответил)");
    cleanup();
    // the writer is the step's «who», not the card's assignee (@dev here)
    show({ card: { ...base, next: nxt("council_writer", { who: "@analyst", params: { round: 2, rounds: 3, findings: 4 } }) } });
    expect(document.body.textContent).toContain("Совет: круг 2 из 3 — ждёт @analyst: ответить на находки (4)");
  });

  it("a run under way says how far it got", () => {
    show({
      card: { ...base, status: "In Progress" },
      commands: [command({ state: "started", agent: "dev", session: "s1" })],
      sessions: [agentSession({ id: "s1", agent: "dev" })],
    });
    expect(document.body.textContent).toContain("@dev работает");
  });

  it("each action says what it does and who does it; an off one says why and stays", async () => {
    const { onact } = show({
      actions: [
        action({}),
        action({ id: "brief", label: "action.brief", agent: "@analyst", primary: false, enabled: false, reason: "no_agent" }),
      ],
    });
    expect(screen.getByRole("button", { name: "Запустить @dev" })).toBeTruthy();
    expect(document.body.textContent).toContain("@dev получит задачу и начнёт работу");
    expect((screen.getByRole("button", { name: "Разработать ТЗ" }) as HTMLButtonElement).disabled).toBe(true);
    expect(document.body.textContent).toContain("Карточку некому взять");
    await fireEvent.click(screen.getByRole("button", { name: "Запустить @dev" }));
    expect(onact).toHaveBeenCalledWith("start", undefined, null);
  });

  it("the pipeline's problem, the recorded outcome and the waits in two lists", () => {
    const q = decision("question", { id: "d1", requested_by: "agent:dev" });
    const pipeline: CardPipeline = {
      mode: "enforce",
      outcome: { outcome: "wait_user", note: "нужен ответ по схеме", agent: "@dev", state: "final", reason: "", created: "2026-10-17T09:00:00Z" },
      waits: [
        { id: "w1", kind: "decision", ref: "d1", state: "open", blocking: true, reason: "" },
        { id: "w2", kind: "card", ref: "KD-9", state: "open", blocking: false, reason: "" },
      ],
      waits_total: 2,
      problems: [{ kind: "missing_next_step", agent: "@dev", created: "2026-10-17T09:50:10Z" }],
    };
    show({ card: { ...base, status: "In Progress", pipeline }, decisions: { d1: q } });
    const text = document.body.textContent ?? "";
    expect(text).toContain("Ход @dev закончился без следующего шага — BOSS разбирает · 10 мин");
    expect(text).toContain("@dev ждёт ответа: нужен ответ по схеме");
    const blocking = screen.getByRole("list", { name: "Блокирует" });
    expect(blocking.querySelector("a")?.getAttribute("href")).toBe("/cards/KD-5?decision=d1");
    expect(blocking.textContent).toContain("вопрос");
    const free = screen.getByRole("list", { name: "Не блокирует" });
    expect(free.querySelector("a")?.getAttribute("href")).toBe("/cards/KD-9");
    expect(document.querySelector(".problem.warn")).toBeTruthy();
  });

  it("two waits on the same decision are two rows, keyed by the wait", () => {
    const q = decision("question", { id: "d1", requested_by: "agent:dev" });
    const wait = { kind: "decision" as const, ref: "d1", blocking: true, reason: "" };
    const pipeline: CardPipeline = {
      mode: "enforce", outcome: null, waits_total: 2, problems: [],
      waits: [{ id: "w1", state: "open", ...wait }, { id: "w2", state: "pending", ...wait }],
    };
    show({ card: { ...base, status: "In Progress", pipeline }, decisions: { d1: q } });
    expect(screen.getByRole("list", { name: "Блокирует" }).querySelectorAll("li")).toHaveLength(2);
  });

  it("on the coordinator's own card the problem is the owner's", () => {
    const pipeline: CardPipeline = {
      mode: "enforce", outcome: null, waits: [], waits_total: 0,
      problems: [{ kind: "continuation_limit", agent: "@boss", created: "2026-10-17T10:00:00Z" }],
    };
    show({ card: { ...base, status: "In Progress", assignee: "@boss", pipeline } });
    expect(document.body.textContent).toContain("Автопродолжения @boss остановлены пределом — решение за вами");
  });

  it("without pipeline facts the next step is the backend's", () => {
    show({ card: { ...base, status: "In Progress", pipeline: null, next: nxt("not_started", { who: "@dev" }) } });
    expect(document.querySelector(".problem")).toBeNull();
    expect(screen.queryByRole("list", { name: "Блокирует" })).toBeNull();
    expect(document.body.textContent).toContain("Ждём @dev");
  });

  it("nothing to do: by the card's status", () => {
    show({ card: { ...base, status: "Done", next: nxt("done") } });
    expect(document.body.textContent).toContain("Готово");
    cleanup();
    show({ card: { ...base, status: "Blocked", dependencies: ["KD-3"], next: nxt("wait_deps") } });
    expect(document.body.textContent).toContain("Ждёт зависимостей");
    expect(screen.getByRole("link", { name: "KD-3" })).toBeTruthy();
    cleanup();
    show({ card: { ...base, status: "In Progress", next: nxt("not_started", { who: "@dev" }) } });
    expect(document.body.textContent).toContain("Ждём @dev");
    cleanup();
    show({ card: { ...base, status: "To Do", assignee: "", next: nxt("not_started") } });
    expect(document.body.textContent).toContain("У карточки нет исполнителя");
  });

  it("an operation wait names its roles and state, never a card link (KD-9088)", () => {
    const op = "a".repeat(32);
    const pipeline: CardPipeline = {
      mode: "enforce", outcome: null, waits_total: 2, problems: [],
      waits: [
        { id: "w1", kind: "operation", ref: op, state: "open", blocking: true, reason: "",
          operation: { id: op, agents: ["analyst", "dev"], state: "running", phase: "draining", done: ["analyst"] } },
        { id: "w2", kind: "operation", ref: "release:abc1234", state: "open", blocking: true, reason: "",
          operation: { id: "", agents: [], state: "awaited", phase: "", done: [] } },
      ],
    };
    show({ card: { ...base, status: "In Progress", pipeline } });
    const list = screen.getByRole("list", { name: "Блокирует" });
    expect(list.textContent).toContain("установка analyst, dev · идёт · готово: analyst");
    expect(list.textContent).toContain("установка · ждёт запроса релиза");
    expect(list.querySelector(`a[href*="${op}"]`)).toBeNull();
    expect(list.querySelector('a[href*="release"]')).toBeNull();
  });

  it("the card shows its help with the step and the last ended one (KD-9071)", () => {
    const one = { id: "c".repeat(32), kind: "platform_read", lead: "@dev", role: "@sysadmin", helper: "",
                  step: "сверить номера инцидентов", state: "requested" as const, reason: "", evidence: "", limit: "",
                  updated: "" };
    const ended = { ...one, id: "d".repeat(32), helper: "@sysadmin", step: "прочитать журнал",
                    state: "failed" as const, reason: "capability_missing" };
    const older = { ...ended, id: "e".repeat(32), step: "старый шаг" };
    const pipeline: CardPipeline = { mode: "enforce", outcome: null, waits_total: 0, problems: [], waits: [],
                                     assists: [one, ended, older] };
    show({ card: { ...base, status: "In Progress", pipeline } });
    const list = screen.getByRole("list", { name: "Помощь" });
    expect(list.textContent).toContain("@sysadmin · ждёт назначения");
    expect(list.textContent).toContain("сверить номера инцидентов");
    expect(list.textContent).toContain("@sysadmin · не выполнена (нет нужного инструмента)");
    expect(list.textContent).not.toContain("старый шаг");
  });

  it("an assist wait names the helping role and its state (KD-9071)", () => {
    const id = "c".repeat(32);
    const assist = { id, kind: "platform_read", lead: "@dev", role: "@sysadmin", helper: "@sysadmin",
                     step: "сверить номера", state: "accepted" as const, reason: "", evidence: "", limit: "", updated: "" };
    const pipeline: CardPipeline = {
      mode: "enforce", outcome: null, waits_total: 1, problems: [],
      waits: [{ id: "w1", kind: "assist", ref: id, state: "open", blocking: true, reason: "", assist }],
    };
    show({ card: { ...base, status: "In Progress", pipeline } });
    const list = screen.getByRole("list", { name: "Блокирует" });
    expect(list.textContent).toContain("Помощь @sysadmin · в работе");
    expect(list.querySelector(`a[href*="${id}"]`)).toBeNull();
  });

  it("a continue the owner's stop beat says so (KD-9184)", () => {
    const pipeline: CardPipeline = {
      mode: "enforce", waits: [], waits_total: 0, problems: [],
      outcome: { outcome: "continue", note: "дальше тесты", agent: "@dev", state: "void", reason: "stopped", created: "2026-10-17T09:00:00Z" },
    };
    show({ card: { ...base, status: "In Progress", pipeline } });
    expect(screen.getByText(/дальше тесты/).textContent).toContain("остановлено владельцем, итог не применён");
  });

  it("a wait for the council of the code says so (KD-9268)", () => {
    const pipeline: CardPipeline = {
      mode: "enforce", outcome: null, waits_total: 1, problems: [],
      waits: [{ id: "w1", kind: "council", ref: "c".repeat(32), state: "open", blocking: true, reason: "" }],
    };
    const next = nxt("council_running", { source: "recorded", params: { round: 1, rounds: 3, done: 0, total: 2 } });
    show({ card: { ...base, status: "In Progress", pipeline, next } });
    const list = screen.getByRole("list", { name: "Блокирует" });
    expect(list.textContent).toContain("совет");
    expect(list.textContent).not.toContain("решение");
    expect(document.body.textContent).toContain("Совет: круг 1 из 3 — ждёт критиков (0 из 2 ответил)");
  });

  it("a contour wait names the contour in maintenance (KD-9184)", () => {
    const pipeline: CardPipeline = {
      mode: "enforce", outcome: null, waits_total: 1, problems: [],
      waits: [{ id: "w1", kind: "contour", ref: "app-ci", state: "open", blocking: true, reason: "" }],
    };
    show({ card: { ...base, status: "In Progress", pipeline } });
    const list = screen.getByRole("list", { name: "Блокирует" });
    expect(list.textContent).toContain("контур app-ci на обслуживании");
    expect(list.querySelector("a")).toBeNull();
  });
});

describe("NextStep — the aggregate of stages (KD-9121)", () => {
  it("says how many stages are done and lists the open ones", () => {
    const pipeline: CardPipeline = {
      mode: "enforce", outcome: null, waits: [], waits_total: 0, problems: [],
      stages: { total: 2, done: 1, items: [
        { key: "a", child: "KD-11", status: "Done", role: "@dev", title: "Модель" },
        { key: "b", child: "KD-12", status: "To Do", role: "@dev", title: "Веб" },
      ] },
    };
    show({ card: { ...base, status: "In Progress", pipeline } });
    expect(document.body.textContent).toContain("Ждёт этапов 1 из 2");
    expect((screen.getByRole("link", { name: /KD-12/ }) as HTMLAnchorElement).getAttribute("href")).toBe("/cards/KD-12");
    expect(document.body.textContent).not.toContain("Ждём @dev");
  });

  it("a stage problem names itself", () => {
    const pipeline: CardPipeline = {
      mode: "enforce", outcome: null, waits: [], waits_total: 0,
      problems: [{ kind: "stage_cancelled", agent: "@dev", created: "2026-10-17T10:00:00Z" }],
    };
    show({ card: { ...base, status: "In Progress", pipeline } });
    expect(document.body.textContent).toContain("Этап закрыт без работы — в плане дыра — BOSS разбирает");
  });
});

describe("NextStep — a chain plan and its stages", () => {
  const empty: CardPipeline = { mode: "enforce", outcome: null, waits: [], waits_total: 0, problems: [] };

  it("the plan names the chain and each open stage's gate", () => {
    const pipeline: CardPipeline = {
      ...empty,
      stages: { total: 2, done: 1, chain: true, items: [
        { key: "a", child: "KD-11", status: "Done", role: "@dev", title: "Модель", gate: "passed", reason: "" },
        { key: "b", child: "KD-12", status: "Review", role: "@dev", title: "Веб", gate: "reviewing", reason: "" },
      ] },
    };
    show({ card: { ...base, status: "In Progress", pipeline } });
    expect(document.body.textContent).toContain("Сквозной план: ветка и PR на каждый репозиторий, одна приёмка");
    expect(document.body.textContent).toContain("Пропущен BOSS, ждёт общей приёмки");
    expect(document.body.textContent).toContain("Сдан, идёт проверка кода");
  });

  it("a handed-in stage says whose move it is", () => {
    const stage = { parent: "KD-1", gate: "reviewing", verdict: "", reason: "" };
    const card = { ...base, status: "Review" as const, parent: "KD-1" };
    show({ card: { ...card, pipeline: { ...empty, stage }, next: nxt("stage_review") } });
    expect(document.body.textContent).toContain("Этап сдан: идёт проверка кода, затем решает BOSS");
    expect((screen.getByRole("link", { name: /KD-1/ }) as HTMLAnchorElement).getAttribute("href")).toBe("/cards/KD-1");
    cleanup();
    const ready = { ...stage, gate: "ready", verdict: "ready" };
    show({ card: { ...card, pipeline: { ...empty, stage: ready },
      next: nxt("stage_gate", { who: "@boss", params: { verdict: "ready" } }) } });
    expect(document.body.textContent).toContain("Этап сдан, проверка кода: ready — BOSS пропускает, возвращает или отдаёт вам");
  });

  it("a held stage says why it waits for the owner", () => {
    const stage = { parent: "KD-1", gate: "held", verdict: "ready", reason: "boss_silent" };
    show({ card: { ...base, status: "Review", parent: "KD-1", pipeline: { ...empty, stage },
      next: nxt("review", { who: "@owner" }) } });
    expect(document.body.textContent).toContain("На проверке: примите работу или верните её");
    expect(document.body.textContent).toContain("Ждёт вас: BOSS не ответил за 30 минут");
  });

  it("a reopened stage names itself", () => {
    const pipeline: CardPipeline = {
      ...empty, problems: [{ kind: "stage_reopened", agent: "@dev", created: "2026-10-17T10:00:00Z" }],
    };
    show({ card: { ...base, status: "In Progress", pipeline } });
    expect(document.body.textContent).toContain("Закрытый этап вернули в работу — BOSS разбирает");
  });
});
