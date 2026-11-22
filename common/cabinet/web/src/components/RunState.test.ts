import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { agentSession, command } from "../lib/stores/runs.fixtures";
import RunState, { workingAgent } from "./RunState.svelte";

const NOW = Date.parse("2026-10-17T10:00:10Z");
const text = () => document.body.textContent?.replace(/\s+/g, " ").trim() ?? "";

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("RunState", () => {
  it("shows nothing without a command", () => {
    render(RunState, { commands: [], sessions: [], now: NOW });
    expect(text()).toBe("");
  });

  it("a queued request is saved and waits", () => {
    render(RunState, { commands: [command()], sessions: [], now: NOW });
    expect(text()).toContain("Запрос сохранён · в очереди");
  });

  it("a delivered request names the agent, or the slot it waits for", () => {
    const delivered = command({ state: "delivered", session: "s1" });
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "new" })], now: NOW });
    expect(text()).toContain("Передано @analyst");
    cleanup();
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "queued" })], now: NOW });
    expect(text()).toContain("Ждёт свободного слота");
  });

  it("a delivery older than 30 s that never started says the start is delayed", () => {
    const delivered = command({ state: "delivered", session: "s1", updated: "2026-10-17T09:59:30Z" });
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "starting" })], now: NOW });
    expect(text()).toContain("Запуск задержан");
  });

  it("a request behind the agent's running turn is next, never delayed", () => {
    const delivered = command({ state: "delivered", session: "s1", updated: "2026-10-17T09:58:00Z" });
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "working" })], now: NOW });
    expect(text()).toContain("@analyst работает · запрос следующий");
    expect(text()).not.toContain("Запуск задержан");
  });

  it("a request waiting for a slot shows its thread's place in the agent's queue and how long it waits (KD-9047)", () => {
    const delivered = command({ state: "delivered", session: "s1" });
    const queued = agentSession({ state: "queued", queue_position: 3, queued_since: "2026-10-17T09:55:00Z" });
    render(RunState, { commands: [delivered], sessions: [queued], now: NOW });
    expect(text()).toContain("Ждёт свободного слота · впереди тредов: 2 · ждёт 5 мин");
    expect(text()).not.toMatch(/осталось|примерно/);  // a place, never a time it would mean
    cleanup();
    render(RunState, { commands: [delivered], sessions: [{ ...queued, queue_position: 1 }], now: NOW });
    expect(text()).toContain("Ждёт свободного слота · тред следующий в очереди агента · ждёт 5 мин");
    cleanup();
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "queued" })], now: NOW });
    expect(text()).toBe("Ждёт свободного слота");  // an older host reports no place
  });

  it("a request behind its own thread's turn counts the thread's requests ahead (KD-9047)", () => {
    const first = command({ id: "c1", state: "delivered", session: "s1", created: "2026-10-17T09:58:00Z" });
    const second = command({ id: "c2", state: "delivered", session: "s1", created: "2026-10-17T09:59:00Z",
      updated: "2026-10-17T09:59:00Z" });
    render(RunState, { commands: [second, first], sessions: [agentSession({ state: "working" })], now: NOW });
    expect(text()).toContain("@analyst работает · впереди запросов этого треда: 1 · ждёт 1 мин");
  });

  it("a started request says the agent works", () => {
    render(RunState, { commands: [command({ state: "started", session: "s1" })], sessions: [agentSession()], now: NOW });
    expect(text()).toContain("@analyst работает");
  });

  it("a started request whose session's heartbeat is older than 2 min confirms no work (KD-9036)", () => {
    const later = Date.parse("2026-10-17T10:03:01Z"); // the heartbeat of 10:01:00 is 121 s old
    const started = command({ state: "started", session: "s1" });
    render(RunState, { commands: [started], sessions: [agentSession()], now: later });
    expect(text()).toContain("@analyst: нет пульса");
    expect(text()).not.toContain("работает");
    expect(document.querySelector(".spin")).toBeNull();
    cleanup();
    render(RunState, { commands: [started], sessions: [], now: NOW }); // no session at all: nothing beats
    expect(text()).toContain("нет пульса");
    cleanup();
    const delivered = command({ state: "delivered", session: "s1" }); // behind a turn that no longer beats
    render(RunState, { commands: [delivered], sessions: [agentSession()], now: later });
    expect(text()).toContain("нет пульса");
    expect(document.querySelector(".spin")).toBeNull();
  });

  it("the indicator follows the clock without any event (KD-9036)", async () => {
    const view = render(RunState, { commands: [command({ state: "started", session: "s1" })], sessions: [agentSession()], now: NOW });
    expect(document.querySelector(".spin")).not.toBeNull();
    await view.rerender({ now: Date.parse("2026-10-17T10:03:01Z") });
    expect(document.querySelector(".spin")).toBeNull();
    expect(text()).toContain("нет пульса");
  });

  it("a done request is quiet unless the agent waits for an answer", () => {
    render(RunState, { commands: [command({ state: "done" })], sessions: [], now: NOW });
    expect(text()).toBe("");
    cleanup();
    render(RunState, { commands: [command({ state: "done" })], sessions: [], waiting: true, now: NOW });
    expect(text()).toContain("Ждёт вашего ответа");
  });

  it("a failed request says why and retries its session", async () => {
    const oncontinue = vi.fn();
    const failed = command({ state: "failed", session: "s1", error: "turn failed" });
    render(RunState, { commands: [failed], sessions: [], now: NOW, canControl: true, oncontinue });
    expect(text()).toContain("Не удалось: ход не завершился");
    expect(screen.getByTitle("turn failed")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(oncontinue).toHaveBeenCalledWith("s1");
    cleanup();
    render(RunState, { commands: [failed], sessions: [], now: NOW, canControl: false, oncontinue });
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("a failure's machine code reads as words: the reason first, an unknown code generically", () => {
    const cases: [Partial<Parameters<typeof command>[0]>, string][] = [
      [{ reason: "host_restart" }, "хост сессий перезапустился"],
      [{ reason: "delivery_expired", error: "host_unavailable" }, "не доставлено за сутки"],
      [{ reason: "host_refused", error: "refused: analyst is not on this session host" }, "хост сессий отказал"],
      [{ error: "exit 143" }, "процесс сессии завершился (код 143)"],
      [{ error: "error_during_execution" }, "ошибка во время хода"],
      [{ error: "something_new" }, "ошибка исполнения"],
    ];
    for (const [fields, words] of cases) {
      render(RunState, { commands: [command({ state: "failed", session: "s1", ...fields })], sessions: [], now: NOW });
      expect(text()).toContain(`Не удалось: ${words}`);
      cleanup();
    }
  });

  it("a stop is not the card's run: the request before it is", () => {
    const stop = command({ id: "c2", kind: "stop", state: "done" });
    render(RunState, { commands: [stop, command({ state: "started", session: "s1" })], sessions: [agentSession()], now: NOW });
    expect(text()).toContain("@analyst работает");
  });

  it("an agent at work spins its chip (KD-9036)", () => {
    render(RunState, { commands: [command({ state: "started", session: "s1" })], sessions: [agentSession()], now: NOW });
    expect(document.querySelector(".chip .spin")).not.toBeNull();
    cleanup();
    render(RunState, { commands: [command()], sessions: [], now: NOW });
    expect(document.querySelector(".spin")).toBeNull();
  });

  it("names the agent whose session on the card works or starts", () => {
    expect(workingAgent([agentSession({ state: "idle" }), agentSession({ id: "s2", agent: "dev", state: "working" })], NOW)).toBe("@dev");
    expect(workingAgent([agentSession({ state: "starting" })], NOW)).toBe("@analyst");
    expect(workingAgent([agentSession({ state: "idle" }), agentSession({ state: "failed" })], NOW)).toBe("");
  });

  it("a session whose heartbeat is older than 2 min works for nobody (KD-9036)", () => {
    const stale = agentSession({ agent: "dev", heartbeat: "2026-10-19T11:00:00Z", updated: "2026-10-19T11:00:00Z" });
    expect(workingAgent([stale], Date.parse("2026-10-19T11:02:00Z"))).toBe("@dev"); // 120 s: still fresh, as team.FRESH
    expect(workingAgent([stale], Date.parse("2026-10-19T12:00:00Z"))).toBe("");
    // no heartbeat yet: its last update stands for it, as on the server
    const unbeaten = agentSession({ heartbeat: "", updated: "2026-10-19T11:00:00Z" });
    expect(workingAgent([unbeaten], Date.parse("2026-10-19T11:01:00Z"))).toBe("@analyst");
    expect(workingAgent([unbeaten], Date.parse("2026-10-19T12:00:00Z"))).toBe("");
  });

  it("a request of a role paused for a reinstall says why, not «waits for a slot» (KD-9088)", () => {
    const delivered = command({ state: "delivered", session: "s1" });
    const pause = { agent: "@analyst", held: "operation" as const, operation: "7".repeat(32), card: "KD-9045",
      phase: "draining", until: "2026-10-17T10:46:51Z", busy: [{ agent: "@analyst", card: "KD-9064" }] };
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "queued" })], pauses: [pause], now: NOW });
    expect(text()).toContain("@analyst на паузе: переустановка по KD-9045 · ждёт окончания хода @analyst на KD-9064 · до");
    expect(text()).not.toContain("Ждёт свободного слота");
    cleanup();
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "queued" })],
      pauses: [{ ...pause, busy: [], phase: "installing" }], now: NOW });
    expect(text()).toContain("@analyst на паузе: переустановка по KD-9045 идёт");
    cleanup();
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "queued" })],
      pauses: [{ ...pause, agent: "@boss" }], now: NOW });
    expect(text()).toContain("Ждёт свободного слота");  // another role's pause is not this one's
    cleanup();
    render(RunState, { commands: [delivered], sessions: [agentSession({ state: "queued" })],
      pauses: [{ ...pause, held: "failed" as const, busy: [], until: "", phase: "installing" }], now: NOW });
    expect(text()).toContain("@analyst удержан после неудачной переустановки по KD-9045 — снимает владелец");
  });
});
