import { afterEach, describe, expect, it, vi } from "vitest";
import { feed } from "./feed.svelte";

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

const ev = (seq: number, kind = "card.created") => ({ seq, ts: "2026-10-15T09:00:00Z", actor: "agent:dev", kind, card: "KD-1", payload: {} });

afterEach(() => vi.unstubAllGlobals());

describe("feed", () => {
  it("reads the newest page, then older ones until nothing is left", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ events: [ev(9), ev(8)], last: 9, before: 8 }))
      .mockResolvedValueOnce(json({ events: [ev(3)], last: 9, before: 3 }))
      .mockResolvedValueOnce(json({ events: [], last: 9, before: 3 }));
    vi.stubGlobal("fetch", fetch);
    await feed.load({ project: "alert", group: "cards" });
    expect(fetch.mock.calls[0][0]).toBe("/api/feed?limit=50&project=alert&kinds=card.created%2Ccard.changed%2Ccard.imported");
    await feed.more();
    expect(fetch.mock.calls[1][0]).toContain("before=8");
    expect(feed.done).toBe(false);
    await feed.more();
    expect(feed.done).toBe(true);
    await feed.more();
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(feed.events.map((e) => e.seq)).toEqual([9, 8, 3]);
  });

  it("keeps scanning when a filtered page found nothing but moved on", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ events: [ev(900)], last: 900, before: 900 }))
        .mockResolvedValueOnce(json({ events: [], last: 900, before: 400 })),
    );
    await feed.load({});
    await feed.more();
    expect(feed.done).toBe(false);
  });

  it("puts newer events on top once", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ events: [ev(5)], last: 5, before: 5 }))
        .mockResolvedValueOnce(json({ events: [ev(5), ev(6), ev(7)], last: 7, before: null })),
    );
    await feed.load({});
    await feed.newer();
    expect(feed.events.map((e) => e.seq)).toEqual([7, 6, 5]);
  });

  it("resync continues past a full journal scan containing only filtered events", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ events: [ev(5)], last: 5, before: 5 }))
      .mockResolvedValueOnce(json({ events: [], last: 10005, before: null }))
      .mockResolvedValueOnce(json({ events: [ev(10006)], last: 10006, before: null }));
    vi.stubGlobal("fetch", fetch);
    await feed.load({ project: "alert" });
    await feed.sync({ project: "alert" });
    expect(fetch.mock.calls[2][0]).toContain("after=10005");
    expect(feed.events.map((e) => e.seq)).toEqual([10006, 5]);
  });

  it("drains more than one forward page on one resync", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json({ events: [ev(5)], last: 5, before: 5 }))
      .mockResolvedValueOnce(json({ events: Array.from({ length: 50 }, (_, i) => ev(i + 6)), last: 55, more: true }))
      .mockResolvedValueOnce(json({ events: [ev(56)], last: 56, more: false }));
    vi.stubGlobal("fetch", fetch);
    await feed.load({});
    await feed.sync({});
    expect(feed.events.map((e) => e.seq)).toEqual(Array.from({ length: 52 }, (_, i) => 56 - i));
  });

  it("sync reads the first page until it has one, then only what is newer", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValueOnce(json({ events: [ev(5)], last: 5, before: 5 }))
      .mockResolvedValueOnce(json({ events: [ev(6)], last: 6, before: 6 }))
      .mockResolvedValueOnce(json({ events: [ev(2)], last: 6, before: 2 }));
    vi.stubGlobal("fetch", fetch);
    await expect(feed.sync({ project: "a" })).rejects.toThrow();
    await feed.sync({ project: "a" });
    expect(fetch.mock.calls[1][0]).not.toContain("after=");
    await feed.sync({ project: "a" });
    expect(fetch.mock.calls[2][0]).toContain("after=5");
    expect(feed.events.map((e) => e.seq)).toEqual([6, 5]);
    await feed.sync({ project: "b" }); // another filter: its own first page
    expect(fetch.mock.calls[3][0]).not.toContain("after=");
    expect(feed.events.map((e) => e.seq)).toEqual([2]);
  });

  it("after reset the next sync reads the first page again (another person signs in)", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ events: [ev(5)], last: 5, before: 5 }))
      .mockResolvedValueOnce(json({ events: [ev(9)], last: 9, before: 9 }));
    vi.stubGlobal("fetch", fetch);
    await feed.sync({});
    feed.reset();
    expect(feed.events).toEqual([]);
    await feed.sync({});
    expect(fetch.mock.calls[1][0]).not.toContain("after=");
    expect(feed.events.map((e) => e.seq)).toEqual([9]);
  });
});
