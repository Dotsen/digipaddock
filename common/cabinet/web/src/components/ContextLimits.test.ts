// Settings → Models → «Context limit»: one row per role with its value in force, a number or the explicit «no limit»,
// a value out of range refused in the owner's words before anything is sent.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { ContextLimit } from "../lib/contextLimit";
import { toasts } from "../lib/toast.svelte";
import ContextLimits from "./ContextLimits.svelte";

beforeEach(() => setLocale("ru"));
afterEach(() => {
  cleanup();
  toasts.items = [];
  vi.unstubAllGlobals();
});

const ROWS: ContextLimit[] = [
  { agent: "analyst", limit: 400000, set: false },
  { agent: "dev", limit: 250000, set: true },
  { agent: "sysadmin", limit: "none", set: true },
];

function serve(refuse = false) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      if (refuse) {
        return Response.json({ ok: false, error: "invalid", message: "range", reason: "context_limit_range" }, { status: 400 });
      }
      const agent = url.split("/").pop()!;
      return Response.json({ agent, limit: JSON.parse(String(init.body)).limit, set: true });
    }
    return Response.json(ROWS);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const puts = (fetch: ReturnType<typeof serve>) =>
  fetch.mock.calls.filter(([, init]) => init?.method === "PUT").map(([url, init]) => [url, JSON.parse(String(init!.body))]);

const row = async (agent: string) => (await screen.findByText(`@${agent}`)).closest("li") as HTMLElement;
const plain = (text: string | null) => (text ?? "").replace(/\s/g, " ");

it("shows every role's value in force, the default marked", async () => {
  serve();
  render(ContextLimits);
  const analyst = await row("analyst");
  expect(plain(analyst.textContent)).toContain("Сейчас: 400 000 токенов");
  expect(within(analyst).getByText("по умолчанию")).toBeTruthy();
  expect(plain((await row("dev")).textContent)).toContain("Сейчас: 250 000 токенов");
  expect(within(await row("dev")).queryByText("по умолчанию")).toBeNull();
  const sysadmin = await row("sysadmin");
  expect(sysadmin.textContent).toContain("без предела — окно модели");
  expect((within(sysadmin).getByLabelText("Без предела") as HTMLInputElement).checked).toBe(true);
  expect((within(sysadmin).getByLabelText("Предел @sysadmin, токенов") as HTMLInputElement).disabled).toBe(true);
});

it("saves a role's new limit and only that role's", async () => {
  const fetch = serve();
  render(ContextLimits);
  const dev = await row("dev");
  const save = within(dev).getByRole("button", { name: "Сохранить" }) as HTMLButtonElement;
  expect(save.disabled).toBe(true); // nothing changed yet
  await fireEvent.input(within(dev).getByLabelText("Предел @dev, токенов"), { target: { value: "300 000" } });
  await fireEvent.click(save);
  await vi.waitFor(() => expect(puts(fetch)).toEqual([["/api/context-limits/dev", { limit: 300000 }]]));
  await vi.waitFor(() => expect(plain(dev.textContent)).toContain("Сейчас: 300 000 токенов"));
  expect(toasts.items.map((t) => t.text)).toEqual(["Предел @dev сохранён"]);
});

it("saves «no limit» as an explicit choice, and a number again after it", async () => {
  const fetch = serve();
  render(ContextLimits);
  const analyst = await row("analyst");
  await fireEvent.click(within(analyst).getByLabelText("Без предела"));
  await fireEvent.click(within(analyst).getByRole("button", { name: "Сохранить" }));
  await vi.waitFor(() => expect(puts(fetch)).toEqual([["/api/context-limits/analyst", { limit: "none" }]]));
  const sysadmin = await row("sysadmin");
  await fireEvent.click(within(sysadmin).getByLabelText("Без предела"));
  const input = within(sysadmin).getByLabelText("Предел @sysadmin, токенов") as HTMLInputElement;
  expect(input.disabled).toBe(false);
  expect(plain(input.value)).toBe("400000"); // the default to start from
  await fireEvent.click(within(sysadmin).getByRole("button", { name: "Сохранить" }));
  await vi.waitFor(() => expect(puts(fetch).at(-1)).toEqual(["/api/context-limits/sysadmin", { limit: 400000 }]));
});

it.each(["99999", "1000001", "abc", "400000.5", ""])("refuses %j in plain words and sends nothing", async (typed) => {
  const fetch = serve();
  render(ContextLimits);
  const dev = await row("dev");
  await fireEvent.input(within(dev).getByLabelText("Предел @dev, токенов"), { target: { value: typed } });
  expect(within(dev).getByRole("alert").textContent).toBe(
    "Целое число токенов от 100 000 до 1 000 000 — или отметьте «Без предела»",
  );
  const save = within(dev).getByRole("button", { name: "Сохранить" }) as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  await fireEvent.submit(save.closest("form")!);
  expect(puts(fetch)).toEqual([]);
  expect(within(await row("analyst")).queryByRole("alert")).toBeNull(); // another role's row says nothing
});

it("says the server's refusal in the owner's words", async () => {
  serve(true);
  render(ContextLimits);
  const dev = await row("dev");
  await fireEvent.input(within(dev).getByLabelText("Предел @dev, токенов"), { target: { value: "300000" } });
  await fireEvent.click(within(dev).getByRole("button", { name: "Сохранить" }));
  await vi.waitFor(() =>
    expect(toasts.items.map((t) => t.text)).toEqual([
      "Предел контекста — целое число токенов от 100 000 до 1 000 000 или «без предела»",
    ]),
  );
});
