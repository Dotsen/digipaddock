import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { models } from "../lib/stores/models.svelte";
import type { BriefState, BriefVersion, BriefView, Card, CouncilFacts } from "../lib/types";
import BriefTab from "./BriefTab.svelte";

const card: Card = {
  id: "KD-5", n: 5, status: "Inbox", assignee: "@dev", project: "digipaddock", title: "Отчёт", labels: [], servers: [],
  priority: "", due: "", parent: "", dependencies: [], revision: 3,
  created: "2026-10-17T10:00:00Z", updated: "2026-10-17T10:00:00Z",
  description: "Исходная постановка", acceptance: [{ text: "Есть отчёт", checked: false }], dod: "",
  plan: "", notes: "", summary: "", created_by: "@owner", thread: 1,
};

function view(brief: Partial<BriefState>, extra: Partial<BriefView> = {}): BriefView {
  return {
    brief: { state: "ready", version: 1, approved: false, legacy: false, questions: 0, error: "", ...brief },
    versions: [], current: { description: card.description, acceptance: ["Есть отчёт"] }, changes: [], ...extra,
  };
}

function version(over: Partial<BriefVersion>): BriefVersion {
  return { version: 1, origin: "agent", author: "agent:analyst", created: "2026-10-17T10:00:00Z", approved_by: "",
    approved_at: "", description: "Цель", acceptance: ["Есть отчёт"], changes: [], ...over };
}

function show(v: BriefView | null) {
  return render(BriefTab, {
    view: v, card, canEdit: true, busy: false, draftKey: "test:brief", onsave: vi.fn(async () => true),
    ontoggleac: vi.fn(), onaddac: vi.fn(), ontoggledod: vi.fn(), onadddod: vi.fn(),
  });
}

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("BriefTab", () => {
  it("not requested shows the original statement with the editors", () => {
    show(view({ state: "not_requested", version: 0 }));
    expect(screen.getByText("Не запрошено")).toBeTruthy();
    expect(screen.getByText("ТЗ ещё не готовилось — ниже исходная постановка")).toBeTruthy();
    expect(screen.getByText("Исходная постановка")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Править" })).toBeTruthy();
    expect(screen.getByText("Есть отчёт")).toBeTruthy();
  });

  it("stale shows the changes since vN with the unchanged runs folded", () => {
    const { container } = show(view({ state: "stale" }, {
      changes: [{ op: "…", text: "3" }, { op: "-", text: "старая цель" }, { op: "+", text: "новая цель" }],
    }));
    expect(screen.getByText("Изменено после готовности · v1")).toBeTruthy();
    expect(screen.getByText("Изменения после v1")).toBeTruthy();
    expect(screen.getByText("3 строк без изменений")).toBeTruthy();
    const lines = [...container.querySelectorAll(".diff li")].map((li) => [li.className.match(/d-\w+/)?.[0], li.textContent?.trim()]);
    expect(lines).toEqual([["d-skip", "3 строк без изменений"], ["d-del", "старая цель"], ["d-add", "новая цель"]]);
  });

  it("versions list newest first with author, date, approval and origin", () => {
    const { container } = show(view({ version: 2, approved: true }, {
      versions: [
        version({}),
        version({ version: 2, origin: "person", author: "user:owner", approved_by: "user:owner",
          approved_at: "2026-10-17T11:00:00Z", changes: [{ op: "+", text: "- Есть график" }] }),
      ],
    }));
    const heads = [...container.querySelectorAll(".versions > li summary b")].map((b) => b.textContent ?? "");
    expect(heads).toHaveLength(2);
    expect(heads[0]).toMatch(/^v2 · @owner · /);
    expect(heads[1]).toMatch(/^v1 · @analyst · /);
    expect(screen.getByText("редакция человека")).toBeTruthy();
    expect(screen.getByText("подготовил агент")).toBeTruthy();
    expect(screen.getByText(/^согласовано: @owner, /)).toBeTruthy();
    expect(screen.getByText("Редакции")).toBeTruthy();
  });

  it("legacy shows the confirmation label", () => {
    show(view({ version: 0, legacy: true }));
    expect(screen.getByText("Существующее описание · требуется подтверждение")).toBeTruthy();
  });

  it("a failed preparation says why", () => {
    show(view({ state: "failed", version: 0, error: "spawn_failed" }));
    expect(screen.getByText("Ошибка подготовки")).toBeTruthy();
    expect(screen.getByText("сессию не удалось запустить").getAttribute("title")).toBe("spawn_failed");
    cleanup();
    show(view({ state: "failed", version: 0, error: "brief_ended" }));
    expect(screen.getByText("аналитик закончил ход без ТЗ и без вопросов")).toBeTruthy();
    cleanup();
    show(view({ state: "failed", version: 0, error: "brief_not_started" }));
    expect(screen.getByText("аналитика не разбудили")).toBeTruthy();
  });

  it("shows the operations its approval grants, though the description names none (KD-9088)", () => {
    const step = { verb: "agent.reinstall", target: "factory", agents: ["dev", "boss"], what: "Переустановить роли",
      effect: "Новые инструкции", risk: { level: "medium", why: "Роли недоступны на время установки" },
      rollback: "Предыдущая выкатка" };
    const { container } = show(view({ version: 1 }, {
      current: { description: "Цель без слов об установке", acceptance: [], operations: [step] },
      versions: [version({ operations: [step] })],
    }));
    const now = container.querySelector(".ops-now")!;
    expect(now.textContent).toContain("Операции — их разрешает одобрение ТЗ");
    for (const part of ["Переустановить роли", "agent.reinstall", "factory", "dev, boss", "Новые инструкции",
      "риск средний", "Роли недоступны на время установки", "Предыдущая выкатка"]) {
      expect(now.textContent).toContain(part);
    }
    const past = container.querySelector(".versions .ops")!;  // the version's own steps
    expect(past.textContent).toContain("Переустановить роли");
  });

  it("a brief without operations shows no operations block", () => {
    const { container } = show(view({ version: 1 }));
    expect(container.querySelector(".ops-now")).toBeNull();
  });
});

describe("BriefTab — the council (KD-9267)", () => {
  const facts = (over: Partial<CouncilFacts> = {}): CouncilFacts => ({
    id: "c1", kind: "brief", state: "running", round: 2, rounds: 3, done: 0, total: 2, open_findings: 0, end_reason: "",
    version: 2, ...over,
  });
  const read = (round: number, state: string, runs: [string, string, string][]) => ({
    council: "c1", round, state, runs: runs.map(([model, state, verdict]) => ({ model, state, verdict })),
  });
  const text = (el: Element | null) => (el?.textContent ?? "").replace(/\s+/g, " ");

  beforeEach(() => {
    models.list = [
      { id: "claude:opus", title: "Claude Opus", provider: "subscription", enabled: 1, key: true, experimental: false, builtin: true },
      { id: "or-x", title: "GPT-5 via OpenRouter", provider: "openrouter", enabled: 1, key: true, experimental: true },
    ];
  });
  afterEach(() => models.reset());

  it("a version names the round that read it; the brief says the council reads it", () => {
    const { container } = show(view({ state: "in_council", version: 2, council: facts() }, {
      versions: [
        version({ council: read(1, "ended", [["claude:opus", "done", "needs_changes"], ["or-x", "failed", ""]]) }),
        version({ version: 2, council: read(2, "running", [["claude:opus", "queued", ""], ["or-x", "done", "ready"]]) }),
      ],
    }));
    expect(screen.getByText("ТЗ ред. 2 читает совет")).toBeTruthy();
    expect(text(container.querySelector(".state"))).toContain("Совет: круг 2 из 3 · читают критики: 0 из 2");
    const [second, first] = [...container.querySelectorAll(".versions > li summary")].map(text);
    expect(first).toContain("круг 1: Claude Opus · подписка — требует правок, GPT-5 via OpenRouter — не ответил");
    expect(second).toContain("круг 2: Claude Opus · подписка — в очереди, GPT-5 via OpenRouter — готово");
  });

  it("an ended council leaves its outcome at the brief; a version no council read says nothing", () => {
    const { container } = show(view({ version: 1, council: facts({ state: "clean", round: 1, done: 2 }) }, {
      versions: [version({ council: null })],
    }));
    expect(text(container.querySelector(".state"))).toContain("Совет: круг 1 из 3 · чисто");
    expect(text(container.querySelector(".versions"))).not.toContain("круг");
  });

  it("without a council the brief says nothing of one", () => {
    const { container } = show(view({ version: 1 }, { versions: [version({})] }));
    expect(text(container)).not.toContain("Совет");
  });
});

describe("BriefTab — stages (KD-9121)", () => {
  it("shows the stages of the current brief and of a version", () => {
    const stage = { key: "a", title: "Модель", role: "@dev", spec: "implementer", depends: [], acceptance: ["Тест проходит"] };
    const { container } = show(view({ version: 1 }, {
      current: { description: "Цель", acceptance: [], stages: [stage] },
      versions: [version({ stages: [stage] })],
    }));
    const now = container.querySelector(".stages-now")!;
    expect(now.textContent).toContain("Этапы — их заведёт одобрение ТЗ");
    expect(now.textContent).toContain("Модель");
    expect(container.querySelector(".versions .stages")!.textContent).toContain("Модель");
  });
});
