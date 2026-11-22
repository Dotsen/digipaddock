import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { startRouter } from "../lib/router.svelte";
import { answer } from "../lib/stores/overview.fixtures";
import { overview } from "../lib/stores/overview.svelte";
import type { Overview } from "../lib/types";
import Dashboard from "./Dashboard.svelte";

const live = vi.hoisted(() => ({ listener: null as ((e: unknown) => void) | null }));
vi.mock("../lib/live.svelte", async (original) => ({
  ...(await original<object>()),
  onLiveEvent: (listener: (e: unknown) => void) => {
    live.listener = listener;
    return () => (live.listener = null);
  },
}));

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const FULL: Overview = answer({
  needs: [
    { kind: "question", card: "KD-1", title: "Asks", project: "digipaddock", decision: "d1", waiting: "@dev", since: "2026-10-17T09:00:00Z", link: "/cards/KD-1?decision=d1" },
    { kind: "review", card: "KD-2", title: "Review me", project: "digipaddock", decision: "", waiting: "@dev", since: "2026-10-17T09:00:00Z", link: "/cards/KD-2?tab=result" },
  ],
  work: [
    { card: "KD-3", title: "Working", project: "digipaddock", assignee: "@dev", status: "In Progress", stage: "In Progress", run: "working", last_step: { at: "2026-10-17T09:00:00Z", text: "Собрал данные" } },
  ],
  problems: [
    { card: "KD-4", title: "Failed", project: "digipaddock", agent: "@dev", kind: "error", reason: "exit 1", since: "2026-10-17T09:00:00Z" },
    { card: "KD-5", title: "Blocked", project: "digipaddock", agent: "@dev", kind: "waiting", reason: "deps", since: "2026-10-17T09:00:00Z", deps: ["KD-6"] },
  ],
  changes: [
    { seq: 6, kind: "review", card: "KD-2", title: "Review me", actor: "agent:dev", actor_name: "@dev", at: "2026-10-17T09:00:00Z", new: true },
    { seq: 4, kind: "created", card: "KD-1", title: "Asks", actor: "user:owner", actor_name: "Владелец", at: "2026-10-17T08:00:00Z", new: false },
  ],
  team: [{ name: "dev", state: "working", working: 1, waiting_you: 1, queued: 0, failed: 1 }],
  counts: { needs: 2, work: 1, problems: 2, changes_new: 1 },
  seen: 5,
  watermark: 7,
});

let posted: unknown[] = [];
let reads = 0;

function serve(found: Overview = FULL) {
  posted = [];
  reads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/dashboard/seen") {
        posted.push(JSON.parse(String(init?.body)));
        return json({ overview_seen: 7 });
      }
      if (url.startsWith("/api/dashboard")) {
        reads += 1;
        return json(found);
      }
      return json([]);
    }),
  );
}

beforeEach(() => {
  overview.reset();
  setLocale("ru");
  history.replaceState(null, "", "/");
  startRouter();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Dashboard", () => {
  it("shows the five sections in order, the counts as links to them", async () => {
    serve();
    render(Dashboard);
    await screen.findByText("Собрал данные");
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.firstChild?.textContent?.trim());
    expect(headings).toEqual(["Ждут вас", "В работе", "Проблемы", "Изменения", "Команда"]);
    const counts = screen.getByRole("navigation", { name: "Сводка" });
    for (const [anchor, n] of [["#needs", "2"], ["#work", "1"], ["#problems", "1"]]) {
      const link = within(counts).getAllByRole("link").find((a) => a.getAttribute("href") === anchor);
      expect(link?.textContent).toContain(n);
    }
    expect(document.getElementById("needs")).toBeTruthy();
    expect(within(document.getElementById("needs")!).getByRole("link", { name: /Asks/ }).getAttribute("href")).toBe("/cards/KD-1?decision=d1");
  });

  it("tells a failure from waiting in words, and marks what is new", async () => {
    serve();
    render(Dashboard);
    await screen.findByText("Собрал данные");
    const problems = document.getElementById("problems")!;
    expect(within(problems).getByText(/Ошибка/)).toBeTruthy();
    expect(within(problems).getByText(/Ждёт зависимостей/)).toBeTruthy();
    const changes = document.getElementById("changes")!;
    expect(within(changes).getAllByText("новое")).toHaveLength(1);
    expect(within(changes).getByText("новых: 1")).toBeTruthy();
  });

  it("a stuck card is a problem in words, counted with the failures", async () => {
    serve({
      ...FULL,
      problems: [
        ...FULL.problems,
        { card: "KD-7", title: "Stuck", project: "digipaddock", agent: "@dev", kind: "stuck", reason: "missing_next_step", since: "2026-10-17T09:00:00Z" },
      ],
      counts: { ...FULL.counts, problems: 3 },
    });
    render(Dashboard);
    await screen.findByText("Собрал данные");
    const problems = document.getElementById("problems")!;
    expect(within(problems).getByText("Ход @dev закончился без следующего шага")).toBeTruthy();
    expect(within(problems).getByText("Застряла")).toBeTruthy();
    const counts = screen.getByRole("navigation", { name: "Сводка" });
    expect(within(counts).getAllByRole("link").find((a) => a.getAttribute("href") === "#problems")?.textContent).toContain("2");
  });

  it("keeps changes new until the operator explicitly acknowledges them", async () => {
    serve();
    render(Dashboard);
    await screen.findByText("Собрал данные");
    await new Promise((resolve) => setTimeout(resolve, 2100));
    expect(posted).toEqual([]);
    await fireEvent.click(screen.getByRole("button", { name: "Считать просмотренным" }));
    await vi.waitFor(() => expect(posted).toEqual([{ seq: 7 }]));
    await vi.waitFor(() => expect(within(document.getElementById("changes")!).queryByText("новое")).toBeNull());
  });

  it("a project filter cannot acknowledge changes hidden in the other projects", async () => {
    history.replaceState(null, "", "/?project=digipaddock");
    startRouter();
    serve();
    render(Dashboard);
    await screen.findByText("Собрал данные");
    expect(screen.queryByRole("button", { name: "Считать просмотренным" })).toBeNull();
    expect(posted).toEqual([]);
  });

  it("distinguishes actual execution from cards under way and waiting from errors", async () => {
    serve(answer({ ...FULL, work: [...FULL.work,
      { ...FULL.work[0], card: "KD-7", run: "none" },
      { ...FULL.work[0], card: "KD-8", run: "queued" },
    ], counts: { ...FULL.counts, work: 3 } }));
    render(Dashboard);
    await screen.findByRole("link", { name: "1 Выполняется" });
    expect(screen.getByRole("link", { name: "1 Ошибки выполнения" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "1 Очередь задач" })).toBeTruthy();
    expect(screen.getByText("Всего карточек в работе: 3")).toBeTruthy();
    expect(screen.getByText("Обычное ожидание: 1")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /^Серверы/ })).toBeNull(); // no servers.read: no tile
  });

  it("shows the servers tile and the open crit incidents first among the problems (KD-9055)", async () => {
    serve(answer({
      ...FULL,
      servers: { servers: 5, crit: 1, warn: 2, unreachable: 1 },
      problems: [
        ...FULL.problems,
        { kind: "incident", incident: "P-7", server: "node-c", check: "disk", subject: "/", value: "96%", since: "2026-10-17T09:00:00Z" },
      ],
    }));
    render(Dashboard);
    const tile = await screen.findByRole("link", { name: /^Серверы: crit 1 · warn 2 · недоступны 1 · всего 5/ });
    expect(tile.getAttribute("href")).toBe("/servers");
    const rows = within(document.getElementById("problems")!).getAllByRole("link");
    expect(rows[0].getAttribute("href")).toBe("/incidents/P-7");
    expect(rows[0].textContent).toContain("disk /: 96%");
    expect(screen.getByRole("link", { name: "1 Ошибки выполнения" })).toBeTruthy(); // an incident is not a run's error
  });

  it("a failed acknowledgement keeps changes new and can be retried", async () => {
    serve();
    render(Dashboard);
    await screen.findByText("Собрал данные");
    const original = globalThis.fetch;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => url === "/api/dashboard/seen" ? json({}, 403) : original(url, init)));
    await fireEvent.click(screen.getByRole("button", { name: "Считать просмотренным" }));
    await screen.findByRole("alert");
    expect(within(document.getElementById("changes")!).getByText("новое")).toBeTruthy();
    vi.stubGlobal("fetch", original);
    await fireEvent.click(screen.getByRole("button", { name: "Считать просмотренным" }));
    await vi.waitFor(() => expect(posted).toEqual([{ seq: 7 }]));
  });

  it("a burst of live events reads the overview once more", async () => {
    serve();
    render(Dashboard);
    await screen.findByText("Собрал данные");
    expect(reads).toBe(1);
    for (let i = 0; i < 5; i++) live.listener?.({ seq: 10 + i, kind: "message.added" });
    await vi.waitFor(() => expect(reads).toBe(2), { timeout: 1500 });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(reads).toBe(2);
  });

  it("an empty overview says nothing waits", async () => {
    serve(answer());
    render(Dashboard);
    expect(await screen.findByText("Ничего не ждёт вас")).toBeTruthy();
  });

  it("a tool call or a progress line does not read the overview again", async () => {
    serve();
    render(Dashboard);
    await screen.findByText("Собрал данные");
    for (const kind of ["tool_call", "tool_result", "progress"]) {
      live.listener?.({ seq: 20, kind: "message.added", card: "KD-3", payload: { kind } });
    }
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(reads).toBe(1);
  });

  it("a run whose session stopped beating says so", async () => {
    serve(answer({
      work: [{ card: "KD-3", title: "Silent", project: "digipaddock", assignee: "@dev", status: "In Progress", stage: "In Progress", run: "stale", last_step: null }],
      counts: { needs: 0, work: 1, problems: 0, changes_new: 0 },
    }));
    render(Dashboard);
    expect(await screen.findByText("нет пульса")).toBeTruthy();
  });
});
