import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { coalesce } from "./coalesce";

describe("coalesce", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("fires once, 300 ms after a single event", () => {
    const fn = vi.fn();
    const refresh = coalesce(fn);
    refresh();
    vi.advanceTimersByTime(299);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(5000);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("fires within maxWait under a steady stream", () => {
    const fn = vi.fn();
    const refresh = coalesce(fn);
    const fired: number[] = [];
    fn.mockImplementation(() => fired.push(Date.now()));
    const start = Date.now();
    for (let at = 0; at < 3000; at += 200) {
      refresh();
      vi.advanceTimersByTime(200);
    }
    expect(fired.length).toBeGreaterThanOrEqual(3);
    expect(fired[0] - start).toBeLessThanOrEqual(1000);
    fired.slice(1).forEach((at, i) => expect(at - fired[i]).toBeLessThanOrEqual(1000));
  });

  it("cancel drops a pending refresh", () => {
    const fn = vi.fn();
    const refresh = coalesce(fn);
    refresh();
    refresh.cancel();
    vi.advanceTimersByTime(2000);
    expect(fn).not.toHaveBeenCalled();
    refresh();
    vi.advanceTimersByTime(300);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
