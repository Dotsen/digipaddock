// the card's councils as the page keeps them — read with the card, read again on a council's event or
// a critic's review, cancelled by whoever manages them.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("../chats.svelte", async original => ({...(await original<object>()), refreshChats: vi.fn()}));
import { council } from "../council.fixtures";
import { setLocale } from "../i18n.svelte";
import { councils } from "./councils.svelte";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const answer = (over = {}, can_manage = true) => ({ councils: [council(over)], can_manage });

beforeEach(() => setLocale("ru"));
afterEach(() => {
  councils.close();
  vi.unstubAllGlobals();
});

describe("councils store", () => {
  it("loads the card's councils and whether the person manages them", async () => {
    const fetch = vi.fn(async (_url: string) => json(answer()));
    vi.stubGlobal("fetch", fetch);
    await councils.load("KD-5");
    expect(fetch.mock.calls[0][0]).toBe("/api/cards/KD-5/councils");
    expect(councils.of("KD-5").map((c) => c.id)).toEqual(["c1"]);
    expect(councils.of("KD-6")).toEqual([]);
    expect(councils.canManage).toBe(true);
  });

  it("another card's councils are not shown while its own load", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(answer())));
    await councils.load("KD-5");
    let arrive!: (r: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (arrive = resolve))));
    const loading = councils.load("KD-6");
    expect(councils.of("KD-6")).toEqual([]);
    expect(councils.canManage).toBe(false);
    arrive(json(answer({ id: "c6" }, false)));
    await loading;
    expect(councils.of("KD-6").map((c) => c.id)).toEqual(["c6"]);
  });

  it("a council's event and a critic's review read again; a plain message and another card do not", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(answer())));
    await councils.load("KD-5");
    const fetch = vi.fn(async () => json(answer({ state: "running", round: 2 })));
    vi.stubGlobal("fetch", fetch);
    await councils.onEvent({ seq: 1, kind: "message.added", card: "KD-5", payload: { kind: "text" } });
    await councils.onEvent({ seq: 2, kind: "council.round", card: "KD-9", payload: {} });
    await councils.onEvent({ seq: 3, kind: "card.changed", card: "KD-5", payload: {} });
    expect(fetch).not.toHaveBeenCalled();
    await councils.onEvent({ seq: 4, kind: "council.round", card: "KD-5", payload: {} });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(councils.of("KD-5")[0].round).toBe(2);
    await councils.onEvent({ seq: 5, kind: "message.added", card: "KD-5", payload: { kind: "review", via: "council" } });
    await councils.onEvent({ seq: 6, kind: "message.added", card: "KD-5", payload: { kind: "review_reply", via: "council" } });
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("cancel posts and shows what the server answered", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(answer())));
    await councils.load("KD-5");
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => json(answer({ state: "cancelled", end_reason: "owner" })));
    vi.stubGlobal("fetch", fetch);
    await councils.cancel("KD-5", "c1");
    expect([fetch.mock.calls[0][0], fetch.mock.calls[0][1]?.method]).toEqual(["/api/cards/KD-5/councils/c1/cancel", "POST"]);
    expect(councils.of("KD-5")[0].state).toBe("cancelled");
    expect(councils.busy).toBe(false);
  });

  it("a refused cancel is the caller's to show; the list stays", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(answer())));
    await councils.load("KD-5");
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "refused", message: "ended" }, 409)));
    await expect(councils.cancel("KD-5", "c1")).rejects.toThrow();
    expect(councils.of("KD-5")[0].state).toBe("needs_writer");
    expect(councils.busy).toBe(false);
  });

  it("a closed store reads nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(answer())));
    await councils.load("KD-5");
    councils.close();
    const fetch = vi.fn(async () => json(answer()));
    vi.stubGlobal("fetch", fetch);
    await councils.onEvent({ seq: 1, kind: "council.round", card: "KD-5", payload: {} });
    expect(fetch).not.toHaveBeenCalled();
    expect(councils.of("KD-5")).toEqual([]);
  });
});
