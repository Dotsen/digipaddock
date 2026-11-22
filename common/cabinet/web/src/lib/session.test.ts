import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiPost, csrfToken } from "./api";
import { getLocale, setLocale } from "./i18n.svelte";
import { navigate, router } from "./router.svelte";
import { session, type Me } from "./session.svelte";

const push = vi.hoisted(() => ({ unsubscribePush: vi.fn(async () => "off" as const) }));
vi.mock("./push", () => push);

function me(over: Partial<Me> = {}): Me {
  return {
    id: "anna",
    name: "Анна",
    language: "en",
    timezone: "Etc/GMT-3",
    permissions: { alert: ["cards.read", "cards.comment"], "*": ["feed.read"] },
    projects: ["alert"],
    csrf: "c1",
    push_public_key: "BPk",
    second_factor: { passkey: false, totp: false, password: false },
    must_setup: true,
    ...over,
  };
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  session.clear();
  setLocale("ru");
});

afterEach(() => vi.unstubAllGlobals());

describe("session", () => {
  it("loads the person, the CSRF token and the language", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(200, me())));
    expect(await session.load()).toMatchObject({ id: "anna" });
    expect(session.me?.name).toBe("Анна");
    expect(session.loaded).toBe(true);
    expect(csrfToken()).toBe("c1");
    expect(getLocale()).toBe("en");
  });

  it("is signed out when /api/me answers 401", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(401, { error: "unauthorized" })));
    expect(await session.load()).toBeNull();
    expect(session.me).toBeNull();
    expect(session.loaded).toBe(true);
  });

  it("checks a permission per project and over every project", () => {
    session.signedIn(me());
    expect(session.can("cards.comment", "alert")).toBe(true);
    expect(session.can("cards.comment", "digipaddock")).toBe(false);
    expect(session.can("feed.read", "digipaddock")).toBe(true);
    expect(session.can("feed.read")).toBe(true);
    expect(session.can("cards.comment")).toBe(false);
    expect(session.can("cards.comment", "*")).toBe(false);
    session.signedIn(me({ permissions: { "*": ["*"] } }));
    expect(session.can("users.manage")).toBe(true);
    session.clear();
    expect(session.can("feed.read")).toBe(false);
  });

  it("knows whether a permission is held on any project", () => {
    session.signedIn(me());
    expect(session.canAny("cards.comment")).toBe(true);
    expect(session.canAny("users.manage")).toBe(false);
  });

  it("forgets the person when any call meets 401", async () => {
    session.signedIn(me({ csrf: "c2" }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(401, { error: "unauthorized" })));
    await apiPost("/api/cards", {}).catch(() => undefined);
    expect(session.me).toBeNull();
    expect(csrfToken()).toBe("");
  });

  it("signs out on the server and clears the cached answers", async () => {
    session.signedIn(me());
    const fetch = vi.fn().mockResolvedValueOnce(json(200, {}));
    vi.stubGlobal("fetch", fetch);
    const deleted: string[] = [];
    vi.stubGlobal("caches", {
      keys: async () => ["kd-api", "old-api", "workbox-precache"],
      delete: async (name: string) => (deleted.push(name), true),
    });
    await session.logout();
    expect(fetch.mock.calls[0][0]).toBe("/api/auth/logout");
    expect(session.me).toBeNull();
    // an API cache of an earlier name goes too: a device signed out keeps none
    expect(deleted).toEqual(["kd-api", "old-api"]);
    // global review 15.10.2026: this device's push subscription ends with the session
    expect(push.unsubscribePush).toHaveBeenCalled();
  });

  it("signs out even when the push subscription cannot be dropped", async () => {
    session.signedIn(me());
    push.unsubscribePush.mockRejectedValueOnce(new Error("no service worker"));
    const fetch = vi.fn().mockResolvedValueOnce(json(200, {}));
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("caches", { keys: async () => [], delete: async () => true });
    await session.logout();
    expect(fetch.mock.calls[0][0]).toBe("/api/auth/logout");
    expect(session.me).toBeNull();
  });

  it("signs out everywhere: every session of the person ends, this one too", async () => {
    session.signedIn(me());
    const fetch = vi.fn().mockResolvedValueOnce(json(200, {}));
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("caches", { keys: async () => [], delete: async () => true });
    await session.logout({ everywhere: true });
    expect(fetch.mock.calls[0][0]).toBe("/api/auth/logout-all");
    expect(session.me).toBeNull();
  });

  it("lands on the plain sign-in page: signing out keeps no page to return to", async () => {
    navigate("/settings");
    session.signedIn(me());
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(json(200, {})));
    vi.stubGlobal("caches", { keys: async () => [], delete: async () => true });
    await session.logout();
    expect(router.current.path).toBe("/login");
  });
});
