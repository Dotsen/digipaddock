import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// Chat refreshes are independent subscribers of the same resync bus.
vi.mock("../chats.svelte", async original => ({...(await original<object>()), refreshChats: vi.fn()}));
import { setLocale } from "../i18n.svelte";
import { resyncAll } from "../live.svelte";
import { toasts } from "../toast.svelte";
import type { Card, JournalEvent } from "../types";
import { brief } from "./brief.svelte";
import { cardPage, checklistCounts } from "./card.svelte";

function full(over: Partial<Card> = {}): Card {
  return {
    id: "KD-5",
    n: 5,
    status: "In Progress",
    assignee: "@dev",
    project: "digipaddock",
    title: "Страница карточки",
    labels: [],
    servers: [],
    priority: "",
    due: "",
    parent: "",
    dependencies: [],
    revision: 3,
    created: "2026-10-15T10:00:00Z",
    updated: "2026-10-15T10:00:00Z",
    description: "d",
    acceptance: [
      { text: "a", checked: true },
      { text: "b", checked: false },
    ],
    dod: "- [ ] #1 tests\n- [x] #2 review",
    plan: "",
    notes: "",
    summary: "",
    created_by: "@owner",
    thread: 1,
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

async function loaded(card = full()) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => (url.startsWith("/api/decisions") ? json([]) : json(card))),
  );
  await cardPage.load("KD-5");
}

beforeEach(() => setLocale("ru"));
afterEach(() => {
  brief.close();
  vi.unstubAllGlobals();
  toasts.items = [];
});

describe("card page", () => {
  it("loads the card and its decisions", async () => {
    const fetch = vi.fn(async (url: string) =>
      url.startsWith("/api/decisions") ? json([{ decision: { id: "d1" }, view: {} }]) : json(full()),
    );
    vi.stubGlobal("fetch", fetch);
    await cardPage.load("KD-5");
    expect(fetch.mock.calls.map((c) => c[0]).sort()).toEqual([
      "/api/cards/KD-5",
      "/api/decisions?state=all&card=KD-5",
    ]);
    expect(cardPage.card?.title).toBe("Страница карточки");
    expect(cardPage.decisions.d1).toBeDefined();
  });

  it("ticks an AC item by sending the whole list with the revision", async () => {
    await loaded();
    const fetch = vi.fn().mockResolvedValueOnce(json(full({ revision: 4 })));
    vi.stubGlobal("fetch", fetch);
    expect(await cardPage.toggleAcceptance(1)).toBe(true);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/cards/KD-5");
    expect(JSON.parse(init.body)).toEqual({
      revision: 3,
      changes: {
        acceptance: [
          { text: "a", checked: true },
          { text: "b", checked: true },
        ],
      },
    });
    expect(cardPage.card?.revision).toBe(4);
  });

  it("ticks one DoD line and appends items", async () => {
    await loaded();
    const fetch = vi.fn().mockResolvedValue(json(full({ revision: 4 })));
    vi.stubGlobal("fetch", fetch);
    await cardPage.toggleDod(0);
    expect(JSON.parse(fetch.mock.calls[0][1].body).changes).toEqual({
      dod: "- [x] #1 tests\n- [x] #2 review",
    });
    await loaded();
    vi.stubGlobal("fetch", fetch);
    await cardPage.addDod("docs");
    expect(JSON.parse(fetch.mock.calls.at(-1)![1].body).changes).toEqual({
      dod: "- [ ] #1 tests\n- [x] #2 review\n- [ ] #3 docs",
    });
    await loaded();
    vi.stubGlobal("fetch", fetch);
    await cardPage.addAcceptance("c");
    expect(JSON.parse(fetch.mock.calls.at(-1)![1].body).changes.acceptance).toHaveLength(3);
  });

  it("on 409 says the card changed and reads it again", async () => {
    await loaded();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ error: "conflict", message: "stale" }, 409))
      .mockImplementation(async (url: string) =>
        url.startsWith("/api/decisions") ? json([]) : json(full({ revision: 9 })),
      );
    vi.stubGlobal("fetch", fetch);
    expect(await cardPage.toggleAcceptance(0)).toBe(false);
    expect(toasts.items.map((t) => t.text)).toEqual(["Карточка изменилась — обновите"]);
    expect(cardPage.card?.revision).toBe(9);
  });

  it("reads the card again on its own events only", async () => {
    await loaded();
    const fetch = vi.fn(async (url: string) =>
      url.startsWith("/api/decisions") ? json([]) : json(full({ revision: 6 })),
    );
    vi.stubGlobal("fetch", fetch);
    await cardPage.onEvent({ seq: 1, kind: "card.changed", card: "KD-9", payload: {} });
    expect(fetch).not.toHaveBeenCalled();
    await cardPage.onEvent({ seq: 2, kind: "card.changed", card: "KD-5", payload: {} });
    expect(cardPage.card?.revision).toBe(6);
    await cardPage.onEvent({ seq: 3, kind: "decision.approved", card: "KD-5", payload: {} });
    expect(fetch.mock.calls.at(-1)![0]).toBe("/api/decisions?state=all&card=KD-5");
  });

  it("the next step follows decisions, turns and the answer to a question (KD-9204)", async () => {
    await loaded();
    const asked = { decision: { id: "p1", state: "open" }, view: { asked_open: true } };
    const fetch = vi.fn(async (url: string) =>
      url.startsWith("/api/decisions") ? json([asked]) : json(full({ revision: 6 })),
    );
    vi.stubGlobal("fetch", fetch);
    const urls = async (event: Partial<JournalEvent>) => {
      fetch.mockClear();
      await cardPage.onEvent({ seq: 1, card: "KD-5", payload: {}, kind: "", ...event });
      return fetch.mock.calls.map((c) => c[0]).sort();
    };
    const both = ["/api/cards/KD-5", "/api/decisions?state=all&card=KD-5"];
    expect(await urls({ kind: "decision.opened" })).toEqual(both);
    expect(await urls({ kind: "turn.outcome" })).toEqual(["/api/cards/KD-5"]);
    expect(await urls({ kind: "problem.raised" })).toEqual(["/api/cards/KD-5"]);
    // an agent's words after the person's question: the agent may have answered it
    expect(await urls({ kind: "message.added", actor: "agent:boss", payload: { kind: "text" } })).toEqual(both);
    expect(await urls({ kind: "message.added", actor: "agent:boss", payload: { kind: "tool_call" } })).toEqual([]);
    expect(await urls({ kind: "message.added", actor: "user:owner", payload: { kind: "text" } })).toEqual([]);
  });

  it("an action posts to act with the shown revision and its comment, and shows the card it answers", async () => {
    await loaded(full({ status: "Review" }));
    const fetch = vi.fn(async () => json({ card: full({ status: "In Progress", revision: 4 }), commands: [] }));
    vi.stubGlobal("fetch", fetch);
    expect(await cardPage.act("return", "Нет теста")).toBe(true);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/cards/KD-5/act");
    expect(JSON.parse(String(init.body))).toEqual({ action: "return", revision: 3, comment: "Нет теста" });
    expect(cardPage.card?.status).toBe("In Progress");
  });

  it("act sends brief_version", async () => {
    await loaded(full({ status: "Inbox" }));
    const fetch = vi.fn(async () => json({ card: full({ status: "To Do", revision: 4 }), commands: [] }));
    vi.stubGlobal("fetch", fetch);
    expect(await cardPage.act("start", undefined, 2)).toBe(true);
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ action: "start", revision: 3, brief_version: 2 });
    await cardPage.act("start", undefined, null);
    const [, again] = fetch.mock.calls[1] as unknown as [string, RequestInit];
    expect(JSON.parse(String(again.body))).toEqual({ action: "start", revision: 4 });
  });

  it("409 brief_changed reloads the card and the brief", async () => {
    await loaded(full({ status: "Inbox" }));
    vi.stubGlobal("fetch", vi.fn(async () => json({ brief: {}, versions: [], current: {}, changes: [] })));
    await brief.load("KD-5");
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ error: "conflict", message: "v2 now", reason: "brief_changed" }, 409))
      .mockImplementation(async (url: string) =>
        url.endsWith("/brief")
          ? json({ brief: { state: "ready", version: 2 }, versions: [], current: {}, changes: [] })
          : json(full({ revision: 5 })),
      );
    vi.stubGlobal("fetch", fetch);
    expect(await cardPage.act("start", undefined, 1)).toBe(false);
    expect(toasts.items.map((t) => t.text)).toEqual(["ТЗ обновилось — проверьте новую версию"]);
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(
      expect.arrayContaining(["/api/cards/KD-5", "/api/cards/KD-5/brief"]),
    );
    expect(cardPage.card?.revision).toBe(5);
    expect(brief.view?.brief.version).toBe(2);
    brief.close();
  });

  it("a command, run, session or brief event of the card reads the card again: its buttons follow the brief", async () => {
    await loaded(full({ status: "Inbox", assignee: "@analyst" }));
    const retry = { id: "brief", label: "action.brief_retry" };
    const fetch = vi.fn(async (url: string) =>
      url.startsWith("/api/decisions") ? json([]) : json(full({ status: "Inbox", actions: [retry] as never })),
    );
    vi.stubGlobal("fetch", fetch);
    for (const kind of ["command.failed", "run.failed", "session.changed", "brief.version"]) {
      fetch.mockClear();
      await cardPage.onEvent({ seq: 1, kind, card: "KD-5", payload: {} });
      expect(fetch.mock.calls.map((c) => c[0])).toEqual(["/api/cards/KD-5"]);
    }
    expect(cardPage.card?.actions?.map((a) => a.label)).toEqual(["action.brief_retry"]);
    fetch.mockClear();
    await cardPage.onEvent({ seq: 2, kind: "command.failed", card: "KD-9", payload: {} });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("a 409 or a written action reads the brief again too", async () => {
    await loaded(full({ status: "Inbox" }));
    vi.stubGlobal("fetch", vi.fn(async () => json({ brief: {}, versions: [], current: {}, changes: [] })));
    await brief.load("KD-5");
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ error: "conflict", message: "stale" }, 409))
      .mockImplementation(async (url: string) =>
        url.endsWith("/brief")
          ? json({ brief: { state: "ready", version: 1 }, versions: [], current: {}, changes: [] })
          : url.endsWith("/act")
            ? json({ card: full({ status: "To Do", revision: 6 }), commands: [] })
            : json(full({ status: "Inbox", revision: 5 })),
      );
    vi.stubGlobal("fetch", fetch);
    expect(await cardPage.save({ description: "x" })).toBe(false);
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(expect.arrayContaining(["/api/cards/KD-5/brief"]));
    fetch.mockClear();
    expect(await cardPage.act("start", undefined, 1)).toBe(true);
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(["/api/cards/KD-5/act", "/api/cards/KD-5/brief"]);
    brief.close();
  });

  it("a manual status change sends its reason", async () => {
    await loaded(full({ status: "Review" }));
    const fetch = vi.fn(async () => json(full({ status: "Done", revision: 4 })));
    vi.stubGlobal("fetch", fetch);
    expect(await cardPage.override("Done", { code: "done_outside", text: "в чате" })).toBe(true);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/cards/KD-5");
    expect(JSON.parse(String(init.body))).toEqual({
      revision: 3,
      changes: { status: "Done" },
      override_reason_code: "done_outside",
      override_reason: "в чате",
    });
  });

  it("a duplicate sends the number of its original", async () => {
    await loaded(full({ status: "Inbox" }));
    const fetch = vi.fn(async () => json(full({ status: "Cancelled", revision: 4 })));
    vi.stubGlobal("fetch", fetch);
    expect(await cardPage.override("Cancelled", { code: "duplicate", text: "", canonical: "kd-7" })).toBe(true);
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      revision: 3,
      changes: { status: "Cancelled" },
      override_reason_code: "duplicate",
      override_reason: "",
      override_canonical: "kd-7",
    });
  });

  it("a late save of card A does not land on card B", async () => {
    await loaded();
    let answer!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (answer = resolve))));
    const saving = cardPage.save({ status: "Review" });
    expect(cardPage.saving).toBe(true);
    const patch = answer;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => (url.startsWith("/api/decisions") ? json([]) : json(full({ id: "KD-6", n: 6 })))),
    );
    await cardPage.load("KD-6");
    expect(cardPage.saving).toBe(false);
    patch(json(full({ revision: 4 })));
    await saving;
    expect(cardPage.card?.id).toBe("KD-6");
    expect(cardPage.saving).toBe(false);
  });

  it("a slower GET with an older revision does not roll the card back", async () => {
    await loaded();
    const answers: ((r: Response) => void)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        url.startsWith("/api/decisions")
          ? Promise.resolve(json([]))
          : new Promise<Response>((resolve) => answers.push(resolve)),
      ),
    );
    const older = cardPage.reloadCard();
    const newer = cardPage.reloadCard();
    answers[1](json(full({ revision: 4 })));
    await newer;
    answers[0](json(full({ revision: 3 })));
    await older;
    expect(cardPage.card?.revision).toBe(4);
  });

  it("a save may name the revision it was based on", async () => {
    await loaded();
    const fetch = vi.fn().mockResolvedValueOnce(json(full({ revision: 8 })));
    vi.stubGlobal("fetch", fetch);
    await cardPage.save({ description: "x" }, 2);
    expect(JSON.parse(fetch.mock.calls[0][1].body).revision).toBe(2);
  });

  it("a first load error is kept for the retry button", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "unavailable", message: "down" }, 500)));
    await cardPage.load("KD-11").catch(() => undefined);
    expect(cardPage.error?.status).toBe(500);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => (url.startsWith("/api/decisions") ? json([]) : json(full({ id: "KD-11" })))),
    );
    await cardPage.load("KD-11");
    expect(cardPage.error).toBeNull();
    expect(cardPage.card?.id).toBe("KD-11");
  });

  it("a closed page reads nothing when the app resyncs", async () => {
    await loaded();
    cardPage.close();
    const fetch = vi.fn(async () => json([]));
    vi.stubGlobal("fetch", fetch);
    await cardPage.onEvent({ seq: 9, kind: "card.changed", card: "KD-5", payload: {} });
    resyncAll();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("counts AC and DoD items", () => {
    expect(checklistCounts(full())).toEqual({ ac: [1, 2], dod: [1, 2] });
    expect(checklistCounts(full({ acceptance: [], dod: "text only" }))).toEqual({ ac: [0, 0], dod: [0, 0] });
  });
});
