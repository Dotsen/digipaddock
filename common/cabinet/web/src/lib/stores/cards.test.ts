import { afterEach, describe, expect, it, vi } from "vitest";
import type { CardCompact } from "../types";
import { activeFilters, applyCardEvent, board, boardStatus, byStatus, doneSince, filterCards } from "./cards.svelte";

function card(id: string, over: Partial<CardCompact> = {}): CardCompact {
  return {
    id,
    n: Number(id.split("-")[1]),
    status: "To Do",
    assignee: "@dev",
    project: "digipaddock",
    title: `Card ${id}`,
    labels: [],
    servers: [],
    priority: "",
    due: "",
    parent: "",
    dependencies: [],
    revision: 1,
    created: "2026-10-15T10:00:00Z",
    updated: "2026-10-15T10:00:00Z",
    ...over,
  };
}

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("cards", () => {
  it("applies card.changed to the card at once", () => {
    const list = [card("KD-1"), card("KD-2")];
    const event = {
      seq: 7,
      kind: "card.changed",
      card: "KD-2",
      payload: {
        fields: ["status"],
        title: "Renamed",
        before: { status: "To Do", assignee: "@dev" },
        after: { status: "Review", assignee: "@boss" },
      },
    };
    const next = applyCardEvent(list, event);
    expect(next[1]).toMatchObject({ id: "KD-2", status: "Review", assignee: "@boss", title: "Renamed" });
    expect(next[0]).toBe(list[0]);
    expect(list[1].status).toBe("To Do");
    expect(applyCardEvent(list, { seq: 8, kind: "message.added", card: "KD-1", payload: {} })).toBe(list);
  });

  it("shows what the latest load read, not a slower earlier one", async () => {
    let first!: (r: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockReturnValueOnce(new Promise<Response>((resolve) => (first = resolve)))
        .mockResolvedValueOnce(json([card("KD-2", { project: "alert" })])),
    );
    const slow = board.load({ project: "digipaddock" });
    await board.load({ project: "alert" });
    expect(board.cards.map((c) => c.id)).toEqual(["KD-2"]);
    first(json([card("KD-1")]));
    await slow;
    expect(board.cards.map((c) => c.id)).toEqual(["KD-2"]);
  });

  it("refetches the changed card for the fields the event does not carry", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json([card("KD-1")])));
    await board.load({ project: "digipaddock" });
    const full = { ...card("KD-1", { labels: ["spec:reviewer"], revision: 2, status: "Review" }), description: "d" };
    const fetch = vi.fn().mockResolvedValueOnce(json(full));
    vi.stubGlobal("fetch", fetch);
    await board.onEvent({
      seq: 9,
      kind: "card.changed",
      card: "KD-1",
      payload: { fields: ["labels", "status"], title: "Card KD-1", before: {}, after: { status: "Review", assignee: "@dev" } },
    });
    expect(fetch.mock.calls[0][0]).toBe("/api/cards/KD-1");
    expect(board.cards[0]).toMatchObject({ labels: ["spec:reviewer"], revision: 2 });
    expect("description" in board.cards[0]).toBe(false);
  });

  it("adds a created card of the shown project and skips others", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json([card("KD-1")])));
    await board.load({ project: "digipaddock" });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json(card("KD-3", { project: "alert" })))
        .mockResolvedValueOnce(json(card("KD-4"))),
    );
    await board.onEvent({ seq: 10, kind: "card.created", card: "KD-3", payload: { project: "alert" } });
    await board.onEvent({ seq: 11, kind: "card.created", card: "KD-4", payload: { project: "digipaddock" } });
    expect(board.cards.map((c) => c.id)).toEqual(["KD-1", "KD-4"]);
  });

  it("filters by agent, specialization and label", () => {
    const list = [
      card("KD-1", { assignee: "@dev", labels: ["spec:reviewer", "ui"] }),
      card("KD-2", { assignee: "@sysadmin", labels: ["ui"] }),
      card("KD-3", { assignee: "@dev", labels: ["spec:implementer"] }),
    ];
    expect(filterCards(list, { assignee: "@dev" }).map((c) => c.id)).toEqual(["KD-1", "KD-3"]);
    expect(filterCards(list, { spec: "spec:reviewer" }).map((c) => c.id)).toEqual(["KD-1"]);
    expect(filterCards(list, { label: "ui", assignee: "@dev" }).map((c) => c.id)).toEqual(["KD-1"]);
    expect(filterCards(list, {})).toHaveLength(3);
  });

  it("hides cancelled cards by default and keeps them separate from Done", () => {
    const list = [
      card("KD-1", { status: "Review" }),
      card("KD-2", { status: "Done" }),
      card("KD-3", { status: "Cancelled" }),
      card("KD-4", { status: "Inbox" }),
    ];
    expect(filterCards(list, { status: "Review" }).map((c) => c.id)).toEqual(["KD-1"]);
    expect(filterCards(list, { status: "Done" }).map((c) => c.id)).toEqual(["KD-2"]);
    expect(filterCards(list, { status: "Cancelled" }).map((c) => c.id)).toEqual(["KD-3"]);
    expect(filterCards(list, { status: "" }).map((c) => c.id)).toEqual(["KD-1", "KD-2", "KD-4"]);
    expect(filterCards(list, { label: "missing", status: "Cancelled" })).toEqual([]);
    // one column for the chosen status, every column without one
    expect(byStatus(list, "Review").map((g) => g.status)).toEqual(["Review"]);
    expect(byStatus(list, "")).toHaveLength(6);
    expect(byStatus(list).find((g) => g.status === "Done")?.cards.map((c) => c.id)).toEqual(["KD-2"]);
    expect(byStatus(list, "Cancelled").map((g) => [g.status, g.cards.map((c) => c.id)])).toEqual([["Cancelled", ["KD-3"]]]);
  });

  it("reads only a known status from the address", () => {
    expect(boardStatus("Review")).toBe("Review");
    expect(boardStatus("In Progress")).toBe("In Progress");
    expect(boardStatus("Cancelled")).toBe("Cancelled");
    expect(boardStatus("nonsense")).toBe("");
    expect(boardStatus(null)).toBe("");
  });

  it("counts the active filters behind the phone's «Фильтры»", () => {
    expect(activeFilters({})).toBe(0);
    expect(activeFilters({ assignee: "@dev", spec: "spec:reviewer", label: "", status: "Done" })).toBe(3);
  });

  it("keeps only the last week of Done, every other status whole", () => {
    const now = Date.parse("2026-10-15T12:00:00Z");
    const list = [
      card("KD-1", { status: "Done", updated: "2026-10-09T12:00:00Z" }),
      card("KD-2", { status: "Done", updated: "2026-10-08T11:00:00Z" }),
      card("KD-3", { status: "Inbox", updated: "2026-09-01T00:00:00Z" }),
      card("KD-4", { status: "Cancelled", updated: "2026-09-01T00:00:00Z" }),
    ];
    expect(doneSince(list, now).map((c) => c.id)).toEqual(["KD-1", "KD-3", "KD-4"]);
    expect(doneSince(list, now, 30).map((c) => c.id)).toEqual(["KD-1", "KD-2", "KD-3", "KD-4"]);
  });

  it("groups by status in board order", () => {
    const groups = byStatus([card("KD-1", { status: "Done" }), card("KD-2", { status: "Inbox" })]);
    expect(groups.map((g) => [g.status, g.cards.length])).toEqual([
      ["Inbox", 1],
      ["To Do", 0],
      ["In Progress", 0],
      ["Review", 0],
      ["Blocked", 0],
      ["Done", 1],
    ]);
  });

  it("a search reads itself again on a card event instead of one card's GET", async () => {
    const fetch = vi.fn(async (_url: string) => json([card("KD-1")]));
    vi.stubGlobal("fetch", fetch);
    await board.load({ q: "кеш" });
    const again = await board.onEvent({ seq: 9, kind: "card.created", card: "KD-2", payload: {} });
    expect(again).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1); // the page re-runs the search (coalesced); no GET of KD-2
    const quiet = await board.onEvent({ seq: 10, kind: "message.added", card: "KD-1", payload: {} });
    expect(quiet).toBe(false);
  });

  it("drops one card's answer that comes after the query changed", async () => {
    let release: (r: Response) => void = () => {};
    const slow = new Promise<Response>((resolve) => (release = resolve));
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json([card("KD-1")]))
      .mockReturnValueOnce(slow)
      .mockResolvedValueOnce(json([card("KD-5", { project: "alert" })]));
    vi.stubGlobal("fetch", fetch);
    await board.load({ project: "digipaddock" });
    const event = board.onEvent({ seq: 9, kind: "card.created", card: "KD-2", payload: {} });
    await board.load({ project: "alert" });
    release(json(card("KD-2", { project: "alert" })));
    expect(await event).toBe(false);
    expect(board.cards.map((c) => c.id)).toEqual(["KD-5"]);
  });
});

