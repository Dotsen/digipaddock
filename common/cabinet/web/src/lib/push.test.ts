import { afterEach, describe, expect, it, vi } from "vitest";
import { deviceName, pushDetails, pushState } from "./push";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function browserWith(subscription: { endpoint: string } | null, permission = "default") {
  vi.stubGlobal("Notification", { permission });
  vi.stubGlobal("PushManager", function PushManager() {});
  vi.stubGlobal("navigator", {
    userAgent: "test",
    serviceWorker: {
      getRegistration: async () => ({ pushManager: { getSubscription: async () => subscription } }),
    },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("pushState", () => {
  it("is on only when the device's subscription is the person's own", async () => {
    // global review 15.10.2026: a subscription of whoever signed in here before is not this person's
    browserWith({ endpoint: "https://push.example/abc" });
    const fetch = vi.fn().mockResolvedValueOnce(json({ mine: true })).mockResolvedValueOnce(json({ mine: false }));
    vi.stubGlobal("fetch", fetch);
    expect(await pushState()).toBe("on");
    expect(fetch.mock.calls[0][0]).toBe("/api/push/state");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ endpoint: "https://push.example/abc" });
    expect(await pushState()).toBe("off");
  });

  it("is off without a subscription, and when the cabinet cannot be asked", async () => {
    browserWith(null);
    vi.stubGlobal("fetch", vi.fn());
    expect(await pushState()).toBe("off");
    browserWith({ endpoint: "https://push.example/abc" });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new TypeError("offline")));
    expect(await pushState()).toBe("off");
  });

  it("is lost when notifications were allowed here but the subscription is gone (KD-9043)", async () => {
    browserWith(null, "granted");
    vi.stubGlobal("fetch", vi.fn());
    expect(await pushState()).toBe("lost");
    browserWith({ endpoint: "https://push.example/abc" }, "granted");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ mine: false })));
    expect(await pushState()).toBe("lost");
  });

  it("brings this device's last delivery", async () => {
    browserWith({ endpoint: "https://push.example/abc" }, "granted");
    const last = { result: "201", at: "2026-10-20T10:00:00Z", sent: true };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json({ mine: true, last })));
    expect(await pushDetails()).toEqual({ state: "on", last });
  });
});

describe("device names", () => {
  it("names a browser and its system from a user agent", () => {
    const windows = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0 Safari/537.36";
    expect(deviceName(windows)).toBe("Chrome · Windows");
    const iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";
    expect(deviceName(iphone)).toBe("Safari · iOS");
  });
});
