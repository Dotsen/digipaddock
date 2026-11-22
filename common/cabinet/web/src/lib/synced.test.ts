import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { resyncAll } from "./live.svelte";
import { staleness, synced, type Synced } from "./synced.svelte";

let made: Synced[] = [];
const make = (load: () => Promise<void>) => {
  const s = synced(load, { retryDelay: () => 1000 });
  made.push(s);
  return s;
};

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  made.forEach((s) => s.dispose());
  made = [];
  vi.useRealTimers();
});

describe("synced", () => {
  it("retries a failed load until it succeeds and clears stale", async () => {
    const load = vi.fn().mockRejectedValueOnce(new ApiError(500, "unavailable", "x")).mockResolvedValueOnce(undefined);
    const s = make(load);
    await s.run();
    expect(s.stale).toBe(true);
    expect(staleness.count).toBe(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(load).toHaveBeenCalledTimes(2);
    expect(s.stale).toBe(false);
    expect(s.updated).toBeGreaterThan(0);
    expect(staleness.count).toBe(0);
  });

  it("coalesces runs: calls during one load make exactly one more load", async () => {
    let release!: () => void;
    const load = vi.fn(() => new Promise<void>((resolve) => (release = resolve)));
    const s = make(load);
    const first = s.run();
    void s.run();
    void s.run();
    release();
    await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    release();
    await first;
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("a run asked for just as a load ends is not lost", async () => {
    // whatever microtask the request lands in after the load, it makes one more load
    for (let ticks = 0; ticks < 8; ticks++) {
      let done!: () => void;
      const loaded = new Promise<void>((resolve) => (done = resolve));
      const load = vi.fn(async () => done());
      const s = make(load);
      void s.run();
      await loaded;
      for (let i = 0; i < ticks; i++) await Promise.resolve();
      void s.run();
      await vi.advanceTimersByTimeAsync(0);
      expect(load, `after ${ticks} ticks`).toHaveBeenCalledTimes(2);
    }
  });

  it("does not retry what the server refused", async () => {
    const load = vi.fn().mockRejectedValue(new ApiError(404, "not_found", "gone"));
    const onError = vi.fn();
    const s = synced(load, { retryDelay: () => 1000, onError });
    made.push(s);
    await s.run();
    await vi.advanceTimersByTimeAsync(5000);
    expect(load).toHaveBeenCalledTimes(1);
    expect(s.stale).toBe(false);
    expect((s.error as ApiError).status).toBe(404);
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("does not report a failure it retries", async () => {
    const load = vi.fn().mockRejectedValueOnce(new ApiError(0, "network", "x")).mockResolvedValueOnce(undefined);
    const onError = vi.fn();
    const s = synced(load, { retryDelay: () => 1000, onError });
    made.push(s);
    await s.run();
    await vi.advanceTimersByTimeAsync(1000);
    expect(onError).not.toHaveBeenCalled();
    expect(s.stale).toBe(false);
  });

  it("runs again when the app resyncs", async () => {
    const load = vi.fn().mockResolvedValue(undefined);
    make(load);
    resyncAll();
    await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(1));
  });

  it("a load that fails after dispose tells nobody and plans no retry (KD-9007)", async () => {
    let fail!: (exc: unknown) => void;
    const load = vi.fn(() => new Promise<void>((_, reject) => (fail = reject)));
    const onError = vi.fn();
    const s = synced(load, { retryDelay: () => 1000, onError });
    made.push(s);
    const run = s.run();
    s.dispose();
    fail(new ApiError(404, "not_found", "gone"));
    await run;
    expect(onError).not.toHaveBeenCalled();
    expect(s.error).toBeNull();
    const retried = synced(() => Promise.reject(new ApiError(500, "unavailable", "x")), { retryDelay: () => 1000 });
    made.push(retried);
    const second = retried.run();
    retried.dispose();
    await second;
    expect(retried.stale).toBe(false);
    expect(staleness.count).toBe(0);
  });

  it("a run after dispose loads nothing", async () => {
    const load = vi.fn(async () => undefined);
    const s = make(load);
    s.dispose();
    await s.run();
    expect(load).not.toHaveBeenCalled();
  });
});
