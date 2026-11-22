import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { session, type Me } from "../lib/session.svelte";
import type { Card, CardCompact } from "../lib/types";
import CardRow from "./CardRow.svelte";
import DeferControl from "./DeferControl.svelte";

const api = vi.hoisted(() => ({ apiPost: vi.fn(async () => ({})) }));
vi.mock("../lib/api", async (original) => ({ ...(await original<object>()), apiPost: api.apiPost }));

const me: Me = {
  id: "owner", name: "Владелец", language: "ru", timezone: "Etc/GMT-3", permissions: { "*": ["*"] },
  projects: ["digipaddock"], csrf: "c", push_public_key: "k", second_factor: { passkey: true, totp: false, password: false },
  must_setup: false,
};

const base: Card = {
  id: "KD-5", n: 5, status: "In Progress", assignee: "@dev", project: "digipaddock", title: "Отчёт", labels: [], servers: [],
  priority: "", due: "", parent: "", dependencies: [], revision: 3,
  created: "2026-11-08T10:00:00Z", updated: "2026-11-08T10:00:00Z",
  description: "", acceptance: [], dod: "", plan: "", notes: "", summary: "", created_by: "@owner", thread: 7,
};
const defer = { until: "2026-11-09T12:00:00Z", source: "boss" as const, by: "agent:boss", reason: "ждём релиза",
  created: "2026-11-08T10:00:00Z" };

beforeEach(() => {
  setLocale("ru");
  session.signedIn(me);
  api.apiPost.mockClear();
});
afterEach(cleanup);

describe("DeferControl (KD-9236)", () => {
  it("says until when in the person's time zone, who put it off and why, and wakes it now", async () => {
    const onchanged = vi.fn();
    render(DeferControl, { card: { ...base, defer }, onchanged });
    expect(screen.getByText("Отложена до 09.11 15:00")).toBeTruthy();
    expect(screen.getByText("отложил @boss — ждём релиза")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Разбудить сейчас" }));
    expect(api.apiPost).toHaveBeenCalledWith("/api/cards/KD-5/undefer");
    expect(onchanged).toHaveBeenCalled();
  });

  it("puts a card off with the page's local moment and an optional reason", async () => {
    const onchanged = vi.fn();
    render(DeferControl, { card: base, onchanged });
    expect(screen.queryByRole("button", { name: "Разбудить сейчас" })).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Отложить до…" }));
    const when = document.querySelector("input[type=datetime-local]") as HTMLInputElement;
    const why = document.querySelector("input[type=text]") as HTMLInputElement;
    await fireEvent.input(when, { target: { value: "2026-11-10T09:30" } });
    await fireEvent.input(why, { target: { value: "после планёрки" } });
    await fireEvent.click(screen.getByRole("button", { name: "Отложить" }));
    expect(api.apiPost).toHaveBeenCalledWith("/api/cards/KD-5/defer", { until: "2026-11-10T09:30", reason: "после планёрки" });
    expect(onchanged).toHaveBeenCalled();
  });

  it("offers nothing on a closed card or to who may not move it", () => {
    render(DeferControl, { card: { ...base, status: "Done" }, onchanged: vi.fn() });
    expect(screen.queryByRole("button")).toBeNull();
    cleanup();
    session.signedIn({ ...me, permissions: { "*": ["cards.read"] } });
    render(DeferControl, { card: { ...base, defer }, onchanged: vi.fn() });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Отложена до 09.11 15:00")).toBeTruthy();
  });

  it("a board tile says until when and keeps the card where it is", () => {
    const card: CardCompact = { ...base, defer };
    render(CardRow, { card });
    expect(screen.getByText("⏰ до 09.11 15:00")).toBeTruthy();
  });
});
