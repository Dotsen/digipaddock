import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { session, type Me } from "../lib/session.svelte";
import type { PlatformAlert } from "../lib/types";
import PlatformAlerts from "./PlatformAlerts.svelte";

const live = vi.hoisted(() => ({ listener: null as ((e: unknown) => void) | null }));
vi.mock("../lib/live.svelte", async (original) => ({
  ...(await original<object>()),
  onLiveEvent: (listener: (e: unknown) => void) => {
    live.listener = listener;
    return () => (live.listener = null);
  },
}));

const NOW = Date.parse("2026-11-22T12:00:00Z");

function me(permissions: Record<string, string[]>): Me {
  return {
    id: "owner", name: "Владелец", language: "ru", timezone: "Etc/GMT-3", permissions, projects: ["admin"], csrf: "c",
    push_public_key: "k", second_factor: { passkey: true, totp: false, password: false }, must_setup: false,
  };
}

function alert(seq: number, ts: string, over: Partial<PlatformAlert> = {}): PlatformAlert {
  return {
    seq, ts, source: "alert", level: "critical", key: "notice.alert.unit",
    params: { host: "example-stage", unit: `unit-${seq}.service`, reason: "boom" }, ...over,
  };
}

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

let asked: string[] = [];
/** Pages by `before`: the first page is the one without it. */
function serve(pages: Record<string, { items: PlatformAlert[]; next: number | null }>) {
  asked = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    asked.push(url);
    const before = new URL(url, "http://cabinet.invalid").searchParams.get("before") ?? "";
    return json(pages[before] ?? { items: [], next: null });
  }));
}

const texts = (root: HTMLElement) => [...root.querySelectorAll(".platform-alert p")].map((p) => p.textContent?.trim());

beforeEach(() => {
  setLocale("ru");
  session.signedIn(me({ "*": ["*"] }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PlatformAlerts", () => {
  it("lists the alerts of the last week, newest first, with their level and text", async () => {
    serve({ "": { items: [alert(9, "2026-11-21T15:50:52Z"), alert(7, "2026-11-20T10:00:00Z", { level: "warn" })], next: null } });
    const { container } = render(PlatformAlerts, { now: NOW });
    await screen.findByRole("heading", { name: "Тревоги платформы" });
    expect(texts(container)).toEqual([
      "⚠️ example-stage · сбой unit-9.service: boom",
      "⚠️ example-stage · сбой unit-7.service: boom",
    ]);
    const badges = [...container.querySelectorAll(".platform-alert .monitor-badge")].map((b) => b.textContent?.trim());
    expect(badges).toEqual(["Критично", "Предупреждение"]);
    expect(screen.queryByRole("button", { name: "Показать прежние" })).toBeNull();
    expect(asked).toEqual(["/api/alerts?limit=5"]);
  });

  it("keeps the alerts older than a week behind a button and pages further back", async () => {
    serve({
      "": { items: [alert(9, "2026-11-21T15:50:52Z"), alert(5, "2026-11-01T10:00:00Z")], next: 5 },
      "5": { items: [alert(3, "2026-10-20T10:00:00Z")], next: null },
    });
    const { container } = render(PlatformAlerts, { now: NOW });
    await waitFor(() => expect(texts(container)).toHaveLength(1));
    await fireEvent.click(screen.getByRole("button", { name: "Показать прежние" }));
    await waitFor(() => expect(texts(container)).toHaveLength(2)); // the loaded older one first, no request
    expect(asked).toHaveLength(1);
    await fireEvent.click(screen.getByRole("button", { name: "Показать прежние" }));
    await waitFor(() => expect(texts(container)).toHaveLength(3));
    expect(asked[1]).toBe("/api/alerts?limit=5&before=5");
    expect(screen.queryByRole("button", { name: "Показать прежние" })).toBeNull();
  });

  it("says a quiet week in one line and still opens the older ones", async () => {
    serve({ "": { items: [alert(5, "2026-11-01T10:00:00Z")], next: null } });
    const { container } = render(PlatformAlerts, { now: NOW });
    await screen.findByText("За последние 7 дней тревог не было.");
    expect(texts(container)).toEqual([]);
    await fireEvent.click(screen.getByRole("button", { name: "Показать прежние" }));
    await waitFor(() => expect(texts(container)).toHaveLength(1));
  });

  it("is not there with no alert at all, and not asked for without the settings", async () => {
    serve({});
    const { container } = render(PlatformAlerts, { now: NOW });
    await waitFor(() => expect(asked).toHaveLength(1));
    expect(container.querySelector(".platform-alerts")).toBeNull();
    cleanup();
    session.signedIn(me({ "*": ["servers.read", "cards.read"] }));
    serve({ "": { items: [alert(9, "2026-11-21T15:50:52Z")], next: null } });
    const other = render(PlatformAlerts, { now: NOW });
    await Promise.resolve();
    expect(asked).toEqual([]);
    expect(other.container.querySelector(".platform-alerts")).toBeNull();
  });

  it("reads again when the platform posts a notice without a card", async () => {
    serve({ "": { items: [alert(9, "2026-11-21T15:50:52Z")], next: null } });
    render(PlatformAlerts, { now: NOW });
    await waitFor(() => expect(asked).toHaveLength(1));
    live.listener?.({ seq: 10, kind: "notice", card: "KD-1", payload: {} });
    live.listener?.({ seq: 11, kind: "card.changed", card: "", payload: {} });
    expect(asked).toHaveLength(1);
    live.listener?.({ seq: 12, kind: "notice", card: "", payload: {} });
    await waitFor(() => expect(asked).toHaveLength(2));
  });

  it("keeps the older pages the reader opened when a new alert comes", async () => {
    const pages = {
      "": { items: [alert(9, "2026-11-21T15:50:52Z"), alert(5, "2026-11-01T10:00:00Z")], next: 5 as number | null },
      "5": { items: [alert(3, "2026-10-20T10:00:00Z")], next: null },
    };
    serve(pages);
    const { container } = render(PlatformAlerts, { now: NOW });
    await waitFor(() => expect(texts(container)).toHaveLength(1));
    await fireEvent.click(screen.getByRole("button", { name: "Показать прежние" }));
    await fireEvent.click(screen.getByRole("button", { name: "Показать прежние" }));
    await waitFor(() => expect(texts(container)).toHaveLength(3));
    pages[""] = { items: [alert(10, "2026-11-22T11:00:00Z"), alert(9, "2026-11-21T15:50:52Z")], next: 9 };
    live.listener?.({ seq: 10, kind: "notice", card: "", payload: {} });
    await waitFor(() => expect(texts(container)).toHaveLength(4));
    expect(texts(container).map((line) => line?.match(/unit-(\d+)/)?.[1])).toEqual(["10", "9", "5", "3"]);
    expect(screen.queryByRole("button", { name: "Показать прежние" })).toBeNull(); // the end was reached before
  });

  it("starts the list again when the new alerts no longer reach the ones it had", async () => {
    const pages = { "": { items: [alert(9, "2026-11-21T15:50:52Z"), alert(8, "2026-11-21T15:00:00Z")], next: 8 as number | null } };
    serve(pages);
    const { container } = render(PlatformAlerts, { now: NOW });
    await waitFor(() => expect(texts(container)).toHaveLength(2));
    // a burst: the newest page ends above everything loaded — what lies between must not fall into a gap
    pages[""] = { items: [alert(15, "2026-11-22T11:05:00Z"), alert(14, "2026-11-22T11:04:00Z")], next: 14 };
    live.listener?.({ seq: 15, kind: "notice", card: "", payload: {} });
    await waitFor(() => expect(texts(container).map((line) => line?.match(/unit-(\d+)/)?.[1])).toEqual(["15", "14"]));
    await fireEvent.click(screen.getByRole("button", { name: "Показать прежние" }));
    await waitFor(() => expect(asked[asked.length - 1]).toBe("/api/alerts?limit=5&before=14"));
  });

  it("an unknown key is still a line, not a hole", async () => {
    serve({ "": { items: [alert(9, "2026-11-21T15:50:52Z", { key: "notice.later.kind", params: {} })], next: null } });
    const { container } = render(PlatformAlerts, { now: NOW });
    await waitFor(() => expect(texts(container)).toEqual(["Уведомление системы"]));
  });
});
