import { describe, expect, it, vi } from "vitest";
import { checkForNewWorker, reloadOnNewWorker } from "./swupdate";

function container(controller: object | null) {
  const target = new EventTarget() as EventTarget & { controller: object | null };
  target.controller = controller;
  return target;
}

describe("a new service worker after a rollout", () => {
  it("reloads a page an older worker served, once, when the new one takes over", () => {
    // 17.10.2026: the old worker served the old bundle from its precache; the new worker took
    // control a second later and the page kept the old screens until a second reload
    const sw = container({});
    const reload = vi.fn();
    reloadOnNewWorker(sw, reload);
    sw.dispatchEvent(new Event("controllerchange"));
    sw.dispatchEvent(new Event("controllerchange"));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("leaves a page alone that no worker served (the first visit already runs the newest bundle)", () => {
    const sw = container(null);
    const reload = vi.fn();
    reloadOnNewWorker(sw, reload);
    sw.dispatchEvent(new Event("controllerchange"));
    expect(reload).not.toHaveBeenCalled();
  });

  it("does nothing without service workers", () => {
    expect(() => reloadOnNewWorker(undefined, vi.fn())).not.toThrow();
  });
});

describe("an open tab after a rollout (KD-9223)", () => {
  function setup() {
    const update = vi.fn(async () => undefined);
    const workers = Object.assign(new EventTarget(), {
      controller: {}, getRegistration: vi.fn(async () => ({ update })),
    });
    const doc = Object.assign(new EventTarget(), { visibilityState: "hidden" });
    return { update, workers, doc };
  }

  it("asks for a newer worker when the tab comes back into view and every ten minutes", async () => {
    vi.useFakeTimers();
    try {
      const { update, workers, doc } = setup();
      const stop = checkForNewWorker(workers, doc);
      doc.dispatchEvent(new Event("visibilitychange")); // hidden: nothing
      await vi.advanceTimersByTimeAsync(0);
      expect(update).not.toHaveBeenCalled();
      doc.visibilityState = "visible";
      doc.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(update).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(10 * 60_000);
      expect(update).toHaveBeenCalledTimes(2);
      stop();
      await vi.advanceTimersByTimeAsync(10 * 60_000);
      expect(update).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a failed check waits for the next one", async () => {
    const { workers, doc } = setup();
    workers.getRegistration = vi.fn(async () => ({ update: vi.fn(async () => { throw new Error("offline"); }) }));
    checkForNewWorker(workers, doc, 1_000_000)();
    doc.visibilityState = "visible";
    expect(() => doc.dispatchEvent(new Event("visibilitychange"))).not.toThrow();
  });

  it("does nothing without service workers", () => {
    expect(() => checkForNewWorker(undefined, Object.assign(new EventTarget(), { visibilityState: "visible" }))()).not.toThrow();
  });
});
