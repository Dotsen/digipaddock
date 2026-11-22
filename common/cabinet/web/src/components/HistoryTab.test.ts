import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("../lib/chats.svelte", async original => ({...(await original<object>()), refreshChats: vi.fn()}));
import { setLocale } from "../lib/i18n.svelte";
import { resyncAll } from "../lib/live.svelte";
import type { JournalEvent } from "../lib/types";
import HistoryTab from "./HistoryTab.svelte";

const live = vi.hoisted(() => ({ listener: null as ((e: unknown) => void) | null }));
vi.mock("../lib/live.svelte", async (original) => ({
  ...(await original<object>()),
  onLiveEvent: (listener: (e: unknown) => void) => {
    live.listener = listener;
    return () => (live.listener = null);
  },
}));

const event = (seq: number, to = "Review"): JournalEvent => ({
  seq,
  ts: "2026-10-17T10:00:00Z",
  actor: "agent:dev",
  kind: "card.changed",
  card: "KD-5",
  payload: { before: { status: "In Progress" }, after: { status: to }, fields: ["status"] },
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const seqs = () => [...document.querySelectorAll("li[data-seq]")].map((li) => Number(li.getAttribute("data-seq")));

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("HistoryTab", () => {
  it("resync fills a gap spanning several pages without discarding older loaded history", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ events: [event(9), event(8)], next: 8 }))
      .mockResolvedValueOnce(json({ events: [event(3)], next: null }))
      .mockResolvedValueOnce(json({ events: [event(15), event(14)], next: 14 }))
      .mockResolvedValueOnce(json({ events: [event(12), event(9)], next: 9 }));
    vi.stubGlobal("fetch", fetch);
    render(HistoryTab, { cardId: "KD-5" });
    await vi.waitFor(() => expect(seqs()).toEqual([9, 8]));
    await fireEvent.click(screen.getByRole("button", { name: "Показать ранее" }));
    await vi.waitFor(() => expect(seqs()).toEqual([9, 8, 3]));
    resyncAll();
    await vi.waitFor(() => expect(seqs()).toEqual([15, 14, 12, 9, 8, 3]));
    expect(screen.queryByRole("button", { name: "Показать ранее" })).toBeNull();
  });

  it("shows the newest page first and reads older pages on request", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ events: [event(9), event(8)], next: 8 }))
      .mockResolvedValueOnce(json({ events: [event(3)], next: null }));
    vi.stubGlobal("fetch", fetch);
    render(HistoryTab, { cardId: "KD-5" });
    await vi.waitFor(() => expect(seqs()).toEqual([9, 8]));
    await fireEvent.click(screen.getByRole("button", { name: "Показать ранее" }));
    await vi.waitFor(() => expect(seqs()).toEqual([9, 8, 3]));
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      "/api/cards/KD-5/events?limit=100",
      "/api/cards/KD-5/events?before=8&limit=100",
    ]);
    expect(screen.queryByRole("button", { name: "Показать ранее" })).toBeNull();
  });

  it("puts a live event of the card on top, once", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ events: [event(9)], next: null })));
    render(HistoryTab, { cardId: "KD-5" });
    await vi.waitFor(() => expect(seqs()).toEqual([9]));
    live.listener?.(event(12, "Done"));
    live.listener?.(event(12, "Done"));
    live.listener?.({ ...event(13), card: "KD-6" });
    live.listener?.({ seq: 14, kind: "message.added", card: "KD-5", payload: { via: "card.changed" } });
    await vi.waitFor(() => expect(seqs()).toEqual([12, 9]));
  });

  it("a failed read says so with a retry", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({}, 403)));
    render(HistoryTab, { cardId: "KD-5" });
    await screen.findByText("Не удалось загрузить историю");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ events: [event(9)], next: null })));
    await fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    await vi.waitFor(() => expect(seqs()).toEqual([9]));
  });

  it("keeps a live event that came before the first page", async () => {
    let release: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (release = resolve))));
    render(HistoryTab, { cardId: "KD-5" });
    live.listener?.(event(12, "Done"));
    release(json({ events: [event(9)], next: null })); // read before event 12 was written
    await vi.waitFor(() => expect(seqs()).toEqual([12, 9]));
  });
});
