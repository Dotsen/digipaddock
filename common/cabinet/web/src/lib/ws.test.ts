import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backoff, connectWs } from "./ws";

class FakeSocket {
  static all: FakeSocket[] = [];
  sent: unknown[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  readyState = 0;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  receive(message: unknown) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

function start(extra = {}) {
  const onEvent = vi.fn();
  const onBadge = vi.fn();
  const onUnauthorized = vi.fn();
  const live = connectWs({
    onEvent,
    onBadge,
    onUnauthorized,
    url: "ws://test/ws",
    socket: (url) => new FakeSocket(url) as unknown as WebSocket,
    ...extra,
  });
  return { live, onEvent, onBadge, onUnauthorized };
}

describe("replay before the link is open", () => {
  it("asks after the watermark once the first connection opens, after the subscription", () => {
    const { live } = start();
    live.subscribeCard("KD-5");
    live.replay(40);
    live.replay(42); // the earliest asked wins
    FakeSocket.all[0].open();
    expect(FakeSocket.all[0].sent).toEqual([{ subscribe: { all: false, card: "KD-5" } }, { after: 40 }]);
  });

  it("asks after the earlier of the watermark and the last seen seq on a reconnect", () => {
    const { live } = start();
    FakeSocket.all[0].open();
    FakeSocket.all[0].receive({ hello: { seq: 50 } });
    FakeSocket.all[0].drop();
    live.replay(45);
    vi.advanceTimersByTime(backoff(0));
    FakeSocket.all[1].open();
    expect(FakeSocket.all[1].sent).toEqual([{ after: 45 }]);
  });
});

describe("wake", () => {
  it("reconnects at once instead of waiting out the backoff", () => {
    const { live } = start();
    FakeSocket.all[0].open();
    for (let i = 0; i < 4; i += 1) {
      FakeSocket.all.at(-1)!.drop();
      vi.advanceTimersByTime(backoff(i));
    }
    const before = FakeSocket.all.length;
    FakeSocket.all.at(-1)!.drop(); // the next try is 16 s away
    live.wake();
    expect(FakeSocket.all.length).toBe(before + 1);
    vi.advanceTimersByTime(30_000);
    expect(FakeSocket.all.length).toBe(before + 1); // the planned try was dropped
  });

  it("leaves an open link alone", () => {
    const { live } = start();
    FakeSocket.all[0].open();
    live.wake();
    expect(FakeSocket.all).toHaveLength(1);
  });
});

describe("ws", () => {
  it("backs off exponentially up to 30 s", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 10].map(backoff)).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000,
    ]);
  });

  it("delivers events and badges and remembers the last seq", () => {
    const { live, onEvent, onBadge } = start();
    const socket = FakeSocket.all[0];
    socket.open();
    socket.receive({ badge: { inbox: 4 } });
    socket.receive({ event: { seq: 17, kind: "message.added", card: "KD-1" } });
    socket.receive({ subscribed: { all: false, cards: [] } });
    expect(onBadge).toHaveBeenCalledWith({ inbox: 4 });
    expect(onEvent).toHaveBeenCalledWith({ seq: 17, kind: "message.added", card: "KD-1" });
    expect(live.lastSeq()).toBe(17);
  });

  it("reconnects, asks for what it missed and restores the subscription", () => {
    const { live } = start();
    FakeSocket.all[0].open();
    live.subscribeCard("KD-5");
    expect(FakeSocket.all[0].sent).toEqual([{ subscribe: { all: false, card: "KD-5" } }]);
    FakeSocket.all[0].receive({ event: { seq: 9, kind: "card.updated" } });
    FakeSocket.all[0].drop();
    vi.advanceTimersByTime(999);
    expect(FakeSocket.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(2);
    FakeSocket.all[1].open();
    expect(FakeSocket.all[1].sent).toEqual([{ subscribe: { all: false, card: "KD-5" } }, { after: 9 }]);
  });

  it("asks after the hello's seq on a reconnect even when it saw no event", () => {
    start();
    FakeSocket.all[0].open();
    FakeSocket.all[0].receive({ hello: { seq: 0 } });
    FakeSocket.all[0].drop();
    vi.advanceTimersByTime(1000);
    FakeSocket.all[1].open();
    expect(FakeSocket.all[1].sent).toEqual([{ after: 0 }]);
    FakeSocket.all[1].receive({ hello: { seq: 7 } });
    FakeSocket.all[1].drop();
    vi.advanceTimersByTime(1000);
    FakeSocket.all[2].open();
    expect(FakeSocket.all[2].sent).toEqual([{ after: 7 }]); // the hub replayed up to its hello already
  });

  it("reloads when the hub cannot replay what it missed", () => {
    const onReload = vi.fn();
    start({ onReload });
    FakeSocket.all[0].open();
    FakeSocket.all[0].receive({ reload: true });
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("subscribes to every event for the feed and the board", () => {
    const { live } = start();
    live.subscribeAll(true);
    FakeSocket.all[0].open();
    expect(FakeSocket.all[0].sent).toEqual([{ subscribe: { all: true } }]);
  });

  it("stops on a closed session instead of retrying", () => {
    const { onUnauthorized } = start();
    FakeSocket.all[0].open();
    FakeSocket.all[0].drop(4401);
    vi.advanceTimersByTime(60000);
    expect(FakeSocket.all).toHaveLength(1);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not reconnect after close()", () => {
    const { live } = start();
    FakeSocket.all[0].open();
    live.close();
    FakeSocket.all[0].drop(1000);
    vi.advanceTimersByTime(60000);
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("waits longer after each failed attempt and resets once connected", () => {
    start();
    FakeSocket.all[0].drop();
    vi.advanceTimersByTime(1000);
    FakeSocket.all[1].drop();
    vi.advanceTimersByTime(1999);
    expect(FakeSocket.all).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(3);
    FakeSocket.all[2].open();
    FakeSocket.all[2].drop();
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.all).toHaveLength(4);
  });
});

describe("replay", () => {
  it("asks the hub for the events after a seq", () => {
    const { live } = start();
    const socket = FakeSocket.all[0];
    socket.open();
    live.replay(42);
    expect(socket.sent.at(-1)).toEqual({ after: 42 });
  });
});
