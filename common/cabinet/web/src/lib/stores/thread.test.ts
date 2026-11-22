import { afterEach, describe, expect, it, vi } from "vitest";
import { replay } from "../live.svelte";
import type { Message } from "../types";
import { insertMessages, thread } from "./thread.svelte";

vi.mock("../live.svelte", async (original) => ({ ...(await original<object>()), replay: vi.fn() }));

function msg(seq: number, body = `m${seq}`): Message {
  return {
    seq,
    author: "agent:dev",
    author_name: "@dev",
    kind: "text",
    body,
    payload: {},
    created: "2026-10-15T12:00:00Z",
  };
}

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("thread", () => {
  it("inserts messages by seq without duplicates", () => {
    const list = [msg(1), msg(3)];
    const merged = insertMessages(list, [msg(4), msg(2), msg(3, "again")]);
    expect(merged.map((m) => m.seq)).toEqual([1, 2, 3, 4]);
    expect(merged[2].body).toBe("again");
    expect(list.map((m) => m.seq)).toEqual([1, 3]);
  });

  it("loads a card's thread and then only what is new", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ messages: [msg(1), msg(2)], watermark: 0 }))
      .mockResolvedValueOnce(json({ messages: [msg(3)], watermark: 0 }));
    vi.stubGlobal("fetch", fetch);
    await thread.load("KD-5");
    await thread.fetchNewer();
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      "/api/cards/KD-5/messages?limit=200",
      "/api/cards/KD-5/messages?after=2",
    ]);
    expect(thread.messages.map((m) => m.seq)).toEqual([1, 2, 3]);
  });

  it("takes a new message only for its own card", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ messages: [msg(1)], watermark: 0 })));
    await thread.load("KD-5");
    const fetch = vi.fn().mockResolvedValue(json({ messages: [msg(2)], watermark: 0 }));
    vi.stubGlobal("fetch", fetch);
    await thread.onEvent({ seq: 50, kind: "message.added", card: "KD-6", payload: { seq: 9 } });
    expect(fetch).not.toHaveBeenCalled();
    await thread.onEvent({ seq: 51, kind: "message.added", card: "KD-5", payload: { seq: 2 } });
    expect(thread.messages.map((m) => m.seq)).toEqual([1, 2]);
  });

  it("reads a changed message again in place (a file of it deleted, KD-9041)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ messages: [msg(1), msg(2)], watermark: 0 })));
    await thread.load("KD-5");
    const fetch = vi.fn().mockResolvedValue(json({ messages: [msg(2, "без файла")], watermark: 0 }));
    vi.stubGlobal("fetch", fetch);
    await thread.onEvent({ seq: 52, kind: "message.changed", card: "KD-6", payload: { seq: 2 } });
    await thread.onEvent({ seq: 53, kind: "message.changed", card: "KD-5", payload: { seq: 7 } }); // not shown
    expect(fetch).not.toHaveBeenCalled();
    await thread.onEvent({ seq: 54, kind: "message.changed", card: "KD-5", payload: { seq: 2 } });
    expect(fetch.mock.calls[0][0]).toBe("/api/cards/KD-5/messages?before=3&limit=1");
    expect(thread.messages.map((m) => m.body)).toEqual(["m1", "без файла"]);
  });

  it("a late snapshot does not erase newer messages", async () => {
    let answer!: (r: Response) => void;
    const fetch = vi.fn((url: string) =>
      url.includes("after=")
        ? Promise.resolve(json({ messages: [msg(2), msg(3)], watermark: 0 }))
        : new Promise<Response>((resolve) => (answer = resolve)),
    );
    vi.stubGlobal("fetch", fetch);
    const loading = thread.load("KD-7");
    thread.add(msg(3), thread.generation); // the person's own message came back from its POST meanwhile
    const event = thread.onEvent({ seq: 60, kind: "message.added", card: "KD-7", payload: { seq: 2 } });
    answer(json({ messages: [msg(1)], watermark: 0 }));
    await Promise.all([loading, event]);
    expect(thread.messages.map((m) => m.seq)).toEqual([1, 2, 3]);
    // what is new is asked after what the server sent, not after the own message
    expect(fetch.mock.calls.map((c) => c[0])).toContain("/api/cards/KD-7/messages?after=1");
  });

  it("a snapshot of the previous card is dropped", async () => {
    let first!: (r: Response) => void;
    const fetch = vi.fn((url: string) =>
      url.includes("KD-5")
        ? new Promise<Response>((resolve) => (first = resolve))
        : Promise.resolve(json({ messages: [msg(4)], watermark: 0 })),
    );
    vi.stubGlobal("fetch", fetch);
    const a = thread.load("KD-5");
    const b = thread.load("KD-6");
    first(json({ messages: [msg(1), msg(2)], watermark: 0 }));
    await Promise.all([a, b]);
    expect(thread.card).toBe("KD-6");
    expect(thread.messages.map((m) => m.seq)).toEqual([4]);
  });

  it("after the snapshot it asks the hub to replay from the watermark", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ messages: [msg(1)], watermark: 77 })));
    vi.mocked(replay).mockClear();
    await thread.load("KD-8");
    expect(replay).toHaveBeenCalledWith(77);
  });

  it("a failed fetch is retried and the message arrives without a new event", async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ messages: [msg(1)], watermark: 0 })));
      await thread.load("KD-9");
      const fetch = vi
        .fn()
        .mockResolvedValueOnce(new Response("{}", { status: 500 }))
        .mockResolvedValueOnce(json({ messages: [msg(2)], watermark: 0 }));
      vi.stubGlobal("fetch", fetch);
      await thread.onEvent({ seq: 70, kind: "message.added", card: "KD-9", payload: { seq: 2 } });
      expect(thread.stale).toBe(true);
      await vi.advanceTimersByTimeAsync(1000);
      expect(thread.messages.map((m) => m.seq)).toEqual([1, 2]);
      expect(thread.stale).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a closed thread reads nothing more", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ messages: [msg(1)], watermark: 0 })));
    await thread.load("KD-12");
    thread.close();
    const fetch = vi.fn(async () => json({ messages: [], watermark: 0 }));
    vi.stubGlobal("fetch", fetch);
    await thread.fetchNewer();
    await thread.onEvent({ seq: 80, kind: "message.added", card: "KD-12", payload: { seq: 2 } });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("a failed first load is kept as the thread's error and cleared by a retry", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response("{}", { status: 403 })));
    await thread.load("KD-10");
    expect(thread.error).not.toBeNull();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ messages: [msg(1)], watermark: 0 })));
    await thread.retry();
    expect(thread.error).toBeNull();
    expect(thread.messages.map((m) => m.seq)).toEqual([1]);
  });

  it("an agent's chat loads from the agent and replays from its watermark", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ messages: [msg(1)], watermark: 40 }))
      .mockResolvedValueOnce(json({ messages: [msg(2)], watermark: 41 }));
    vi.stubGlobal("fetch", fetch);
    vi.mocked(replay).mockClear();
    await thread.loadAgent("analyst");
    expect(thread.card).toBeNull();
    expect(thread.agent).toBe("analyst");
    expect(replay).toHaveBeenCalledWith(40);
    // a card's message is not the chat's; the chat's own message (no card, its agent) is
    await thread.onEvent({ seq: 41, kind: "message.added", card: "KD-5", payload: { agent: "analyst" } });
    await thread.onEvent({ seq: 42, kind: "message.added", card: "", payload: { agent: "dev" } });
    expect(fetch).toHaveBeenCalledTimes(1);
    await thread.onEvent({ seq: 43, kind: "message.added", card: "", payload: { agent: "analyst" } });
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      "/api/agents/analyst/messages?limit=200",
      "/api/agents/analyst/messages?after=1",
    ]);
    expect(thread.messages.map((m) => m.seq)).toEqual([1, 2]);
  });
  it("opens on the newest window and reads older pages on request", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ messages: [msg(201), msg(202)], watermark: 0, more: true }))
      .mockResolvedValueOnce(json({ messages: [msg(1), msg(200)], watermark: 0, more: false }));
    vi.stubGlobal("fetch", fetch);
    await thread.load("KD-20");
    expect(thread.older).toBe(true);
    await thread.loadOlder();
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      "/api/cards/KD-20/messages?limit=200",
      "/api/cards/KD-20/messages?before=201&limit=200",
    ]);
    expect(thread.messages.map((m) => m.seq)).toEqual([1, 200, 201, 202]);
    expect(thread.older).toBe(false);
    await thread.loadOlder(); // nothing older: no read
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("an older page of a card the page has left is dropped", async () => {
    let answer!: (r: Response) => void;
    const fetch = vi.fn((url: string) =>
      url.includes("before=")
        ? new Promise<Response>((resolve) => (answer = resolve))
        : Promise.resolve(json({ messages: [msg(5)], watermark: 0, more: true })),
    );
    vi.stubGlobal("fetch", fetch);
    await thread.load("KD-21");
    const older = thread.loadOlder();
    await thread.load("KD-22");
    answer(json({ messages: [msg(1)], watermark: 0, more: false }));
    await older;
    expect(thread.card).toBe("KD-22");
    expect(thread.messages.map((m) => m.seq)).toEqual([5]);
    expect(thread.older).toBe(true);
  });

  it("live reads stay after the cursor while older pages are read", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ messages: [msg(301)], watermark: 0, more: true }))
      .mockResolvedValueOnce(json({ messages: [msg(302)], watermark: 0, more: false }));
    vi.stubGlobal("fetch", fetch);
    await thread.load("KD-23");
    await thread.onEvent({ seq: 90, kind: "message.added", card: "KD-23", payload: { seq: 302 } });
    expect(fetch.mock.calls[1][0]).toBe("/api/cards/KD-23/messages?after=301");
    expect(thread.older).toBe(true); // a live read says nothing of older pages
  });
});
