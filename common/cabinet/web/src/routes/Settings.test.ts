import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, expect, it, vi } from "vitest";
import { session } from "../lib/session.svelte";
import type { UsageRow } from "../lib/types";
import Settings from "./Settings.svelte";

afterEach(() => { cleanup(); session.clear(); vi.unstubAllGlobals(); });

const person = (second_factor: { passkey: boolean; totp: boolean; password: boolean }) =>
  session.signedIn({ id: "owner", name: "Owner", language: "ru", timezone: "Etc/GMT-3",
    permissions: {}, projects: [], csrf: "x", push_public_key: "", second_factor, must_setup: false });

it("gives a code for a new device with its time left (KD-9091)", async () => {
  person({ passkey: true, totp: false, password: false });
  const expires = new Date(Date.now() + 15 * 60_000).toISOString();
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url === "/api/me/code") return Promise.resolve(Response.json({ user: "owner", code: "Abc-123_xyz", expires }));
    return Promise.resolve(Response.json(url.endsWith("notifications") ? { kinds: {}, quiet: "" } : []));
  }));
  render(Settings);
  await fireEvent.click(screen.getByRole("button", { name: "Код для нового устройства" }));
  expect(await screen.findByText("Abc-123_xyz")).toBeTruthy();
  expect(screen.getByText(/^Действует ещё 1[45]:\d\d$/)).toBeTruthy();
});

it("a person without a passkey or TOTP is told to add one first (KD-9091)", () => {
  person({ passkey: false, totp: false, password: false });
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(Response.json([]))));
  render(Settings);
  expect(screen.queryByRole("button", { name: "Код для нового устройства" })).toBeNull();
  expect(screen.getByText(/добавьте ключ входа или TOTP/)).toBeTruthy();
});

it("opens the Hindsight page only on a host that runs it (KD-9192)", async () => {
  vi.stubGlobal("fetch", vi.fn((url: string) =>
    Promise.resolve(Response.json(url.endsWith("notifications") ? { kinds: {}, quiet: "" } : []))));
  for (const memory of [false, true]) {
    session.signedIn({ id: "owner", name: "Owner", language: "ru", timezone: "Etc/GMT-3",
      permissions: { "*": ["settings"] }, projects: [], csrf: "x", push_public_key: "",
      second_factor: { passkey: false, totp: false, password: true }, must_setup: false, memory });
    const { container, unmount } = render(Settings);
    expect(!!container.querySelector('a[href="/settings/hindsight"]')).toBe(memory);
    expect(container.querySelector('a[href="/settings/projects"]')).not.toBeNull();
    unmount();
  }
});

it("reloads the price when switching from 7 to 30 days and back", async () => {
  session.signedIn({ id: "owner", name: "Owner", language: "ru", timezone: "Etc/GMT-3",
    permissions: { "*": ["settings"] }, projects: [], csrf: "x", push_public_key: "",
    second_factor: { passkey: false, totp: false, password: true }, must_setup: false });
  const row: UsageRow = { day: "2026-10-14", agent: "hindsight", category: "hindsight", coverage: "full",
    model: "claude-haiku-4-5", requests: 1, input: 100, output: 1, cache_write: 0, cache_read: 0,
    cost_usd: 3, cost_source: "estimated", price: { model: "claude-haiku-4-5", input: 1, output: 5,
      cache_write: 1.25, cache_write_1h: 2, cache_read: 0.1,
      source: "https://platform.claude.com/docs/en/about-claude/pricing", updated: "2026-10-21" } };
  const requests: { url: string; finish: (r: Response) => void }[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url.startsWith("/api/usage")) return new Promise<Response>((finish) => requests.push({ url, finish }));
    return Promise.resolve(Response.json(url.endsWith("notifications") ? { kinds: {}, quiet: "" } : []));
  }));
  render(Settings);
  await vi.waitFor(() => expect(requests).toHaveLength(1));
  requests[0].finish(Response.json([row]));
  await screen.findAllByText("≈ $3.00");
  expect(screen.getByText("claude-haiku-4-5")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Тарифы Anthropic" }).getAttribute("href")).toBe(row.price!.source);
  const period = screen.getByRole("combobox", { name: "Период" });
  await fireEvent.change(period, { target: { value: 30 } });
  await vi.waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1].url).toBe("/api/usage?days=30");
  expect(screen.queryByText("≈ $3.00")).toBeNull();
  requests[1].finish(Response.json([row, { ...row, day: "2026-10-01", cost_usd: 5 }]));
  await screen.findAllByText("≈ $8.00");
  await fireEvent.change(period, { target: { value: 7 } });
  await vi.waitFor(() => expect(requests).toHaveLength(3));
  requests[2].finish(Response.json([row]));
  const hs = (await screen.findByRole("rowheader", { name: "Hindsight" })).closest("tr")!;
  expect(within(hs).getByText("≈ $3.00")).toBeTruthy();
});
