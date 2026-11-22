import { describe, expect, it } from "vitest";
import { deviceId, holdPresence, RENEW_MS } from "./presence";

function fake(active: boolean) {
  const sent: boolean[] = [];
  let tick: () => void = () => undefined;
  let look: () => void = () => undefined;
  const state = { active };
  const stop = holdPresence("boss", {
    active: () => state.active,
    send: async (on) => sent.push(on),
    every: (fn, ms) => {
      expect(ms).toBe(RENEW_MS);
      tick = fn;
      return () => (tick = () => undefined);
    },
    listen: (fn) => {
      look = fn;
      return () => (look = () => undefined);
    },
  });
  return { sent, state, stop, tick: () => tick(), look: () => look() };
}

describe("presence in an agent chat (KD-9043)", () => {
  it("an active window holds and renews it, a hidden one gives it up", () => {
    const page = fake(true);
    expect(page.sent).toEqual([true]);
    page.tick();
    expect(page.sent).toEqual([true, true]); // the renewal
    page.state.active = false;
    page.look();
    expect(page.sent).toEqual([true, true, false]);
    page.tick();
    expect(page.sent).toEqual([true, true, false]); // nothing to take back again
    page.state.active = true;
    page.look();
    page.stop();
    expect(page.sent).toEqual([true, true, false, true, false]);
  });

  it("a background tab never claims it", () => {
    const page = fake(false);
    page.tick();
    page.stop();
    expect(page.sent).toEqual([]);
  });

  it("keeps one id per tab", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    const id = deviceId(storage);
    expect(id).toMatch(/^tab-[0-9a-f]{24}$/);
    expect(deviceId(storage)).toBe(id);
  });
});
