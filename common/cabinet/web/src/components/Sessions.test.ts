import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { agentSession, workerJob } from "../lib/stores/runs.fixtures";
import type { SessionTurn, TurnStep } from "../lib/types";
import Sessions from "./Sessions.svelte";

const buttons = () => screen.queryAllByRole("button").map((b) => b.textContent?.trim());

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Sessions", () => {
  it("lists nothing to steer without sessions", () => {
    render(Sessions, { sessions: [], canControl: true });
    expect(document.body.textContent).toContain("Сессий пока нет");
  });

  it("names the cabinet model and the effort a session runs on (KD-9217)", () => {
    render(Sessions, {
      sessions: [agentSession({ id: "a", model: "or-x", effort: "high" }), agentSession({ id: "b" })],
      canControl: false,
    });
    const rows = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    expect(rows[0]).toContain("модель or-x");
    expect(rows[0]).toContain("усилие high");
    expect(rows[1]).not.toContain("модель");
  });

  it("offers stop to a live session and continue to an ended one", () => {
    render(Sessions, {
      sessions: [
        agentSession({ id: "a", state: "working" }),
        agentSession({ id: "b", state: "idle" }),
        agentSession({ id: "c", state: "failed", error: "exit 1" }),
        agentSession({ id: "d", state: "stopped" }),
        agentSession({ id: "e", state: "queued" }),
      ],
      canControl: true,
    });
    // a session waiting for a slot can be stopped too (the server's live states)
    expect(buttons().filter((b) => b !== "Ход")).toEqual(["Остановить", "Остановить", "Продолжить", "Продолжить", "Остановить"]);
  });

  it("says a session's failure in words, the code in its title", () => {
    render(Sessions, {
      sessions: [
        agentSession({ id: "a", state: "failed", error: "exit 143" }),
        agentSession({ id: "b", state: "failed", error: "host_restart" }),
      ],
      canControl: false,
    });
    const text = document.body.textContent ?? "";
    expect(text).toContain("процесс сессии завершился (код 143)");
    expect(text).toContain("хост сессий перезапустился");
    expect(text).not.toContain("host_restart");
    expect(screen.getByTitle("exit 143")).toBeTruthy();
  });

  it("shows no buttons to whoever may not steer the agent", () => {
    render(Sessions, { sessions: [agentSession({ state: "failed" })], canControl: false });
    expect(buttons()).toEqual([]);
  });

  it("sends continue and stop for its session and tells the page", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json({ id: "c9", kind: "continue" }));
    vi.stubGlobal("fetch", fetch);
    const onchanged = vi.fn();
    render(Sessions, {
      sessions: [agentSession({ id: "a", state: "working" }), agentSession({ id: "b", state: "exited" })],
      canControl: true,
      onchanged,
    });
    await fireEvent.click(screen.getByRole("button", { name: "Остановить" }));
    await fireEvent.click(screen.getByRole("button", { name: "Продолжить" }));
    await vi.waitFor(() => expect(onchanged).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls.map((c) => [c[0], c[1]?.method])).toEqual([
      ["/api/sessions/a/stop", "POST"],
      ["/api/sessions/b/continue", "POST"],
    ]);
  });

  describe("turns (KD-9028)", () => {
    const step = (over: Partial<TurnStep>): TurnStep => ({
      seq: 1, kind: "text", body: "", tool: "", id: "", ok: null, size: null, output: false, status: null, ...over,
    });
    const TURNS: SessionTurn[] = [
      { command: "c2", kind: "message", text: "[board] KD-1: a message for you", initiator: "user:owner",
        state: "failed", error: "turn_failed", created: "2026-10-18T10:05:00Z", updated: "2026-10-18T10:06:00Z",
        cost: 0.5, steps: [] },
      { command: "c1", kind: "assigned", text: "[board] KD-1 assigned to you", initiator: "user:owner",
        state: "done", error: "", created: "2026-10-18T10:00:00Z", updated: "2026-10-18T10:01:00Z", cost: 0.25,
        steps: [
          step({ seq: 3, body: "Смотрю карточку" }),
          step({ seq: 4, kind: "tool_call", body: "Bash: ls -la", tool: "Bash", id: "toolu_1" }),
          step({ seq: 5, kind: "tool_result", tool: "Bash", id: "toolu_1", ok: true, size: 2048, output: true }),
          step({ seq: 6, kind: "tool_call", body: "Read: /srv/x.md", tool: "Read", id: "toolu_2" }),
          step({ seq: 7, kind: "tool_result", tool: "Read", id: "toolu_2", ok: false, size: 12 }),
          step({ seq: 8, kind: "status", body: "denied: Bash", status: { key: "sys.session.denied", params: { tool: "Bash" } } }),
        ] },
    ];

    function serve(...answers: Array<[number, unknown]>) {
      const fetch = vi.fn(async (url: string) => {
        const [status, body] = answers.length > 1 ? answers.shift()! : answers[0];
        if (url.includes("/output/")) return json({ text: "total 0\ndrwx 2 digi digi", size: 2048, truncated: true });
        return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      });
      vi.stubGlobal("fetch", fetch);
      return fetch;
    }

    // a turn names the models that served it
    it("a turn names the model that served it, with every one in the hint", async () => {
      serve([200, [{ ...TURNS[0], served: ["claude-opus-5-5", "claude-haiku-4-5"] }, TURNS[1]]]);
      render(Sessions, { sessions: [agentSession({ id: "s1", state: "working" })], canControl: true });
      await fireEvent.click(screen.getByRole("button", { name: "Ход" }));
      await vi.waitFor(() => expect(document.querySelectorAll("details.turn")).toHaveLength(2));
      const [newer, older] = [...document.querySelectorAll("details.turn > summary")];
      expect(newer.textContent).toContain("claude-opus-5-5 +1");
      expect(newer.querySelector(".served")?.getAttribute("title")).toBe("Модели хода: claude-opus-5-5, claude-haiku-4-5");
      expect(older.querySelector(".served")).toBeNull(); // a turn that reported no usage names none
    });

    it("shows the turns of a session on «Ход»", async () => {
      const fetch = serve([200, TURNS]);
      render(Sessions, { sessions: [agentSession({ id: "s1", state: "working" })], canControl: true });
      await fireEvent.click(screen.getByRole("button", { name: "Ход" }));
      await vi.waitFor(() => expect(document.body.textContent).toContain("Смотрю карточку"));
      const turns = [...document.querySelectorAll("details.turn > summary")].map((s) => s.textContent ?? "");
      expect(turns).toHaveLength(2);
      expect(turns[0]).toContain("Сообщение");
      expect(turns[0]).toContain("✗");
      expect(turns[0]).toContain("ход не завершился");
      expect(turns[0]).toContain("$0.50");
      expect(turns[1]).toContain("Назначение");
      expect(turns[1]).toContain("[board] KD-1 assigned to you");
      expect(turns[1]).toContain("✓");
      expect(turns[1]).toContain("$0.25");
      const steps = [...document.querySelectorAll(".step")].map((s) => s.textContent?.replace(/\s+/g, " ").trim());
      expect(steps).toEqual([
        "Смотрю карточку",
        "Bash: ls -la ✓ 2 KB Вывод",
        "Read: /srv/x.md ✗ 12 B",
        "Отказано в инструменте: Bash",
      ]);
      await fireEvent.click(screen.getByRole("button", { name: "Ход" })); // hide
      await fireEvent.click(screen.getByRole("button", { name: "Ход" })); // show again: no second load
      expect(fetch.mock.calls.map((c) => c[0])).toEqual(["/api/sessions/s1/turns"]);
    });

    it("a compaction is a line of its turn with the context before and after (KD-9462)", async () => {
      const compacted = (key: string, params: Record<string, string | number>) =>
        step({ seq: 9, kind: "status", body: "context compacted", status: { key, params } });
      serve([200, [{ ...TURNS[1], steps: [
        step({ seq: 3, body: "Смотрю карточку" }),
        compacted("sys.session.compacted", { trigger: "auto", before: 412345, after: 45005 }),
        { ...compacted("sys.session.compacted_before", { trigger: "auto", before: 98765 }), seq: 10 },
      ] }]]);
      render(Sessions, { sessions: [agentSession({ id: "s1", state: "working" })], canControl: true });
      await fireEvent.click(screen.getByRole("button", { name: "Ход" }));
      await vi.waitFor(() => expect(document.body.textContent).toContain("Смотрю карточку"));
      const steps = [...document.querySelectorAll(".step")].map((s) => s.textContent?.replace(/\s+/g, " ").trim());
      expect(steps).toEqual([
        "Смотрю карточку",
        "Контекст сжат: 412 345 → 45 005 токенов",
        "Контекст сжат: было 98 765 токенов",
      ]);
    });

    it("retries the turns after an error", async () => {
      serve([500, { error: "unavailable", message: "x" }], [200, TURNS]);
      render(Sessions, { sessions: [agentSession({ id: "s1", state: "idle" })], canControl: true });
      await fireEvent.click(screen.getByRole("button", { name: "Ход" }));
      await vi.waitFor(() => expect(document.body.textContent).toContain("Не удалось загрузить ход"));
      await fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
      await vi.waitFor(() => expect(document.body.textContent).toContain("Смотрю карточку"));
    });

    it("loads a tool output on «Вывод»", async () => {
      const fetch = serve([200, TURNS]);
      render(Sessions, { sessions: [agentSession({ id: "s1", state: "working" })], canControl: true });
      await fireEvent.click(screen.getByRole("button", { name: "Ход" }));
      await vi.waitFor(() => expect(screen.getAllByRole("button", { name: "Вывод" })).toHaveLength(1));
      await fireEvent.click(screen.getByRole("button", { name: "Вывод" }));
      await vi.waitFor(() => expect(document.querySelector("pre.output")?.textContent).toBe("total 0\ndrwx 2 digi digi"));
      expect(document.body.textContent).toContain("обрезано");
      expect(fetch.mock.calls.map((c) => c[0])).toContain("/api/sessions/s1/output/5");
    });

    it("no «Ход» without control", () => {
      render(Sessions, { sessions: [agentSession({ state: "working" })], canControl: false });
      expect(screen.queryByRole("button", { name: "Ход" })).toBeNull();
    });

    it("a turn without steps says so", async () => {
      serve([200, [TURNS[0]]]);
      render(Sessions, { sessions: [agentSession({ id: "s1", state: "exited" })], canControl: true });
      await fireEvent.click(screen.getByRole("button", { name: "Ход" }));
      await vi.waitFor(() => expect(document.body.textContent).toContain("Шаги не записаны"));
    });
  });

  it("lists the card's worker jobs with spec, state and cost", () => {
    render(Sessions, {
      sessions: [],
      jobs: [
        workerJob({ id: "j1", state: "running", slot: "dev/worker1",
          usage: { models: { "claude-sonnet-5-5": { in: 1, out: 2, cache_read: 0, cache_write: 0, cost: 0.25 },
                             "claude-haiku-5-5": { in: 1, out: 1, cache_read: 0, cache_write: 0, cost: 0.1 } },
                   requests: 3 } }),
        workerJob({ id: "j2", spec: "reviewer", state: "failed", reason: "registry changed: the texts differ" }),
        workerJob({ id: "j3", state: "failed", reason: "host_restart" }),
      ],
      canControl: true,
    });
    const text = document.body.textContent ?? "";
    expect(text).toContain("Рабочие сессии (worker)");
    expect(text).toContain("implementer · выполняется · dev/worker1 · $0.35");
    expect(text).toContain("reviewer · сбой · — · $0.00");
    expect(text).toContain("registry changed: the texts differ"); // a reason in words is shown as it is
    expect(text).toContain("хост сессий перезапустился"); // a code in the catalog's words
    expect(text).not.toContain("Сессий пока нет");
    expect(buttons()).toEqual([]); // a job is cancelled by BOSS (kd-worker cancel), not here
  });
});
