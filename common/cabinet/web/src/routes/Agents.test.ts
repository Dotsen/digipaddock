import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { agentSummary, facts } from "../lib/agents.fixtures";
import { setLocale } from "../lib/i18n.svelte";
import Agents from "./Agents.svelte";

const live = vi.hoisted(() => ({ listener: null as ((e: unknown) => void) | null }));
vi.mock("../lib/live.svelte", async (original) => ({
  ...(await original<object>()),
  onLiveEvent: (listener: (e: unknown) => void) => {
    live.listener = listener;
    return () => (live.listener = null);
  },
}));

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

const DEV = agentSummary({
  name: "dev",
  state: "working",
  facts: facts({
    working: 1, waiting_you: 1, queued: 2,
    questions: [{ decision: "d1", card: "KD-1", title: "Asks" }],
    heartbeat: "2026-10-17T09:58:00Z",
    last_action: { at: "2026-10-17T09:50:00Z", card: "KD-3", text: "Собрал данные" },
    progress: "2026-10-17T09:55:00Z",
  }),
  cards: { inbox: 1, to_do: 0, in_progress: 2, review: 0, blocked: 0 },
});
const PANEL = agentSummary({ name: "sysadmin", state: "unknown", host: null });

beforeEach(() => {
  setLocale("ru");
  vi.stubGlobal("fetch", vi.fn(async () => json([DEV, PANEL])));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const tile = async (name: string) => (await screen.findByRole("link", { name: `@${name}` })).closest("article") as HTMLElement;

describe("Agents", () => {
  it("shows the state and every fact that is not zero", async () => {
    render(Agents);
    const dev = within(await tile("dev"));
    expect(dev.getByText("Работает")).toBeTruthy();
    for (const text of ["работает: 1", "ждёт ответа: 1", "в очереди: 2"]) expect(dev.getByText(text)).toBeTruthy();
    expect(dev.queryByText(/сбоев/)).toBeNull();
  });

  it("links to the questions waiting for the person", async () => {
    render(Agents);
    const dev = within(await tile("dev"));
    expect(dev.getByRole("link", { name: /KD-1/ }).getAttribute("href")).toBe("/cards/KD-1?decision=d1");
  });

  it("counts the cards in every status from Inbox on, and only those it has", async () => {
    render(Agents);
    const dev = within(await tile("dev"));
    expect(dev.getByText("Inbox: 1")).toBeTruthy();
    expect(dev.getByText("In Progress: 2")).toBeTruthy();
    expect(dev.queryByText(/Review:/)).toBeNull();
  });

  it("keeps the heartbeat and the last action apart from progress", async () => {
    render(Agents);
    const dev = within(await tile("dev"));
    expect(dev.getByText(/пульс/)).toBeTruthy();
    expect(dev.getByText(/последнее действие/)).toBeTruthy();
    expect(dev.getByText(/Собрал данные/)).toBeTruthy();
    expect(dev.getByText(/отчёт о ходе/)).toBeTruthy();
  });

  it("says plainly that an agent off the session host does not run", async () => {
    render(Agents);
    const panel = within(await tile("sysadmin"));
    expect(panel.getByText("Состояние неизвестно")).toBeTruthy();
    expect(panel.getByText(/не в хосте сессий/)).toBeTruthy();
    expect(panel.queryByText(/позже|переедет/)).toBeNull();
  });

  it("a tool call does not read the agents again; a message does", async () => {
    const fetch = vi.fn(async () => json([DEV, PANEL]));
    vi.stubGlobal("fetch", fetch);
    render(Agents);
    await tile("dev");
    expect(fetch).toHaveBeenCalledTimes(1);
    live.listener?.({ seq: 20, kind: "message.added", card: "KD-3", payload: { kind: "tool_call" } });
    live.listener?.({ seq: 21, kind: "message.added", card: "KD-3", payload: { kind: "tool_result" } });
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(fetch).toHaveBeenCalledTimes(1);
    live.listener?.({ seq: 22, kind: "message.added", card: "KD-3", payload: { kind: "text" } });
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2), { timeout: 1500 });
  });
});
