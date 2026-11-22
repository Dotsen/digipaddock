import { afterEach, describe, expect, it, vi } from "vitest";
import { live, onResync, startLive, stopLive } from "./live.svelte";

class FakeSocket {
  static last: FakeSocket;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  readyState = 0;
  sent: unknown[] = [];
  constructor() {
    FakeSocket.last = this;
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {}
}

afterEach(() => {
  stopLive();
  vi.unstubAllGlobals();
});

describe("live", () => {
  it("a reload message resyncs in place instead of reloading the page", () => {
    vi.stubGlobal("WebSocket", FakeSocket);
    const reload = vi.fn();
    vi.stubGlobal("location", { ...location, reload });
    const resynced = vi.fn();
    const stop = onResync(resynced);
    startLive();
    FakeSocket.last.readyState = 1;
    FakeSocket.last.onopen?.();
    expect(live.link).toBe("online");
    FakeSocket.last.onmessage?.({ data: JSON.stringify({ reload: true }) });
    expect(resynced).toHaveBeenCalledTimes(1);
    expect(reload).not.toHaveBeenCalled();
    stop();
  });

  it("a link the app closed itself (sign-out) is not reported as lost", () => {
    vi.stubGlobal("WebSocket", FakeSocket);
    startLive();
    const socket = FakeSocket.last;
    socket.readyState = 1;
    socket.onopen?.();
    stopLive();
    socket.onclose?.({ code: 1000 }); // the browser reports the close later
    expect(live.link).toBe("connecting");
  });

  it("a first connection that fails reports reconnecting and resyncs once it opens", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeSocket);
    const resynced = vi.fn();
    const stop = onResync(resynced);
    startLive();
    expect(live.link).toBe("connecting");
    FakeSocket.last.onclose?.({ code: 1006 });
    expect(live.link).toBe("reconnecting");
    vi.advanceTimersByTime(1000);
    FakeSocket.last.readyState = 1;
    FakeSocket.last.onopen?.();
    expect(live.link).toBe("online");
    expect(resynced).toHaveBeenCalledTimes(1);
    stop();
    vi.useRealTimers();
  });

  it("a dropped link reports reconnecting and resyncs once it is back", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeSocket);
    const resynced = vi.fn();
    const stop = onResync(resynced);
    startLive();
    const first = FakeSocket.last;
    first.readyState = 1;
    first.onopen?.();
    first.onclose?.({ code: 1006 });
    expect(live.link).toBe("reconnecting");
    vi.advanceTimersByTime(1000);
    FakeSocket.last.readyState = 1;
    FakeSocket.last.onopen?.();
    expect(live.link).toBe("online");
    expect(resynced).toHaveBeenCalledTimes(1);
    stop();
    vi.useRealTimers();
  });
});
