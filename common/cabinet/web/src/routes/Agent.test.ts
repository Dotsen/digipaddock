import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { agentDetail, facts } from "../lib/agents.fixtures";
import { setLocale } from "../lib/i18n.svelte";
import { session, type Me } from "../lib/session.svelte";
import { thread } from "../lib/stores/thread.svelte";
import { toasts } from "../lib/toast.svelte";
import type { AgentDetail } from "../lib/types";
import Agent from "./Agent.svelte";

const me: Me = {
  id: "owner", name: "Owner", language: "ru", timezone: "Etc/GMT-3", permissions: { "*": ["*"] }, projects: ["digipaddock"],
  csrf: "c", push_public_key: "k", second_factor: { passkey: true, totp: false, password: false }, must_setup: false,
};

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

function serve(found: AgentDetail) {
  vi.stubGlobal("fetch", vi.fn(async () => json(found)));
}

beforeEach(() => {
  setLocale("ru");
  toasts.items = [];
});
afterEach(() => {
  cleanup();
  thread.close();
  session.clear();
  vi.unstubAllGlobals();
});

describe("Agent", () => {
  it("an agent in error shows what failed, where, in words", async () => {
    serve(agentDetail({
      state: "error",
      facts: facts({ failed: 1, failures: [{ card: "KD-4", title: "Failed", reason: "exit 1", at: "2026-10-17T09:00:00Z" }] }),
    }));
    render(Agent, { name: "dev" });
    expect(await screen.findByText("Ошибка")).toBeTruthy();
    expect(screen.getByText("сбоев: 1")).toBeTruthy();
    expect(screen.getByText(/процесс сессии завершился \(код 1\)/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /KD-4/ }).getAttribute("href")).toBe("/cards/KD-4");
  });

  it("an agent waiting for the person links to its questions", async () => {
    serve(agentDetail({
      state: "waiting_you",
      facts: facts({ waiting_you: 1, questions: [{ decision: "d1", card: "KD-1", title: "Asks" }] }),
    }));
    render(Agent, { name: "dev" });
    expect(await screen.findByText("Ждёт вашего ответа")).toBeTruthy();
    expect(screen.getByRole("link", { name: /KD-1/ }).getAttribute("href")).toBe("/cards/KD-1?decision=d1");
  });

  it("a closed agent page's late read does not reopen its chat over the next page's (KD-9007)", async () => {
    session.signedIn(me);
    let old!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn((url: string) => url === "/api/agents/analyst"
      ? new Promise<Response>((resolve) => (old = resolve))
      : Promise.resolve(json(url.endsWith("/dev") ? agentDetail({ name: "dev" }) : { messages: [], watermark: 0 }))));
    const first = render(Agent, { name: "analyst" });
    await vi.waitFor(() => expect(old).toBeTypeOf("function"));
    first.unmount();
    render(Agent, { name: "dev" });
    await vi.waitFor(() => expect(thread.agent).toBe("dev"));
    old(json(agentDetail({ name: "analyst" })));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(thread.agent).toBe("dev");
  });

  it("a closed agent page's failed read shows the next page no toast (KD-9007)", async () => {
    let old!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn((url: string) => url === "/api/agents/analyst"
      ? new Promise<Response>((resolve) => (old = resolve))
      : Promise.resolve(json(agentDetail({ name: "dev" })))));
    const first = render(Agent, { name: "analyst" });
    await vi.waitFor(() => expect(old).toBeTypeOf("function"));
    first.unmount();
    render(Agent, { name: "dev" });
    expect(await screen.findByRole("heading", { name: "@dev" })).toBeTruthy();
    old(new Response(JSON.stringify({ error: "forbidden", message: "no" }), { status: 403, headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(toasts.items).toEqual([]);
  });

  it("an agent off the session host: the honest limit, no «later» stub", async () => {
    serve(agentDetail({ state: "unknown", host: null }));
    render(Agent, { name: "dev" });
    expect(await screen.findByText("Состояние неизвестно")).toBeTruthy();
    expect(screen.getAllByText(/не в хосте сессий/)).toHaveLength(1);
    expect(screen.queryByText(/переедет|позже/)).toBeNull();
  });

  it("the coordinator's page is its chat; sessions and closed probes wait under «Ещё» (KD-9043)", async () => {
    session.signedIn(me);
    const probe = { id: "KD-9", n: 9, title: "Проба", status: "Done", assignee: "@boss", project: "digipaddock", labels: [] };
    const live = { ...probe, id: "KD-10", n: 10, title: "Уборка", status: "Inbox" };
    const reply = { seq: 1, author: "agent:boss", kind: "text", body: "Голосовые уже есть в KD-9032", payload: {},
      created: "2026-10-20T09:00:00Z", author_name: "@boss" };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => json(String(url).includes("/messages")
      ? { messages: [reply], watermark: 1 }
      : agentDetail({ name: "boss", cards: [probe, live] as never, sessions: [] }))));
    render(Agent, { name: "boss" });
    expect(await screen.findByRole("heading", { name: "BOSS" })).toBeTruthy();
    expect(screen.getByText("Готов к поручениям")).toBeTruthy();
    expect(screen.queryByText(/слот/)).toBeNull(); // no slot counts or sessions on the first screen
    expect(screen.queryByText("Сессии")).toBeNull();
    screen.getByRole("button", { name: "Задачи" }).click();
    expect(await screen.findByText("Уборка")).toBeTruthy();
    expect(screen.queryByText("Проба")).toBeNull(); // a closed probe is not a task
    expect((await screen.findAllByText("KD-9032")).some(el => el.closest("a")?.getAttribute("href") === "/cards/KD-9032")).toBe(true);
  });
});
