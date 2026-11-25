// The board: project chips in their colours, the chosen one pressed; the search runs as one types and keeps what
// is being typed; a search from elsewhere (the menu, a link) shows in its field.
import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import { navigate, startRouter } from "../lib/router.svelte";
import { board } from "../lib/stores/cards.svelte";
import { projects } from "../lib/stores/projects.svelte";
import Board from "./Board.svelte";

vi.mock("../lib/live.svelte", async (original) => ({
  ...(await original<object>()),
  onLiveEvent: () => () => undefined,
}));

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

const card = (n: number, project: string, title: string) => ({
  id: `KD-${n}`, n, status: "To Do", assignee: "", project, title, labels: [], servers: [], priority: "", due: "",
  parent: "", dependencies: [], revision: 1, created: "2026-11-25T10:00:00Z", updated: "2026-11-25T10:00:00Z",
  defer: null,
});

const asked: string[] = [];

beforeEach(() => {
  setLocale("ru");
  asked.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const path = String(url);
    if (path.startsWith("/api/projects")) {
      return json([{ id: "shop", name: "Shop", route: "@dev" }, { id: "alert", name: "Alert", route: "@dev" }]);
    }
    if (path.startsWith("/api/assignees")) return json([]);
    asked.push(path);
    return json([card(1, "shop", "Nightly backup"), card(2, "alert", "Voice bot")]);
  }));
  history.replaceState(null, "", "/board");
  startRouter();
});

afterEach(() => {
  cleanup();
  projects.reset();
  board.cards = [];
  vi.unstubAllGlobals();
});

it("edges each project chip in its colour and presses the chosen one", async () => {
  history.replaceState(null, "", "/board?project=alert");
  startRouter();
  render(Board);
  const alert = await screen.findByRole("button", { name: "Alert" });
  const all = screen.getByRole("button", { name: "Все проекты" });
  expect(alert.style.getPropertyValue("--pc")).toMatch(/^var\(--project-[0-5]\)$/);
  expect(screen.getByRole("button", { name: "Shop" }).style.getPropertyValue("--pc")).not.toBe(
    alert.style.getPropertyValue("--pc"));
  expect([alert.getAttribute("aria-pressed"), all.getAttribute("aria-pressed")]).toEqual(["true", "false"]);
  expect(alert.classList.contains("on")).toBe(true);
});

it("searches as one types, and an echo of its own search does not undo what is typed since", async () => {
  render(Board);
  const field = (await screen.findByRole("searchbox", { name: "Поиск по карточкам" })) as HTMLInputElement;
  await fireEvent.input(field, { target: { value: "gol" } });
  await vi.waitFor(() => expect(location.search).toBe("?q=gol"));
  await vi.waitFor(() => expect(asked).toContain("/api/cards?q=gol"));
  // typed on while the board read "gol": the field keeps it, the board follows
  await fireEvent.input(field, { target: { value: "golos " } });
  expect(field.value).toBe("golos ");
  await vi.waitFor(() => expect(location.search).toBe("?q=golos"));
  expect(field.value).toBe("golos ");
  await fireEvent.input(field, { target: { value: "" } });
  await vi.waitFor(() => expect(location.search).toBe(""));
});

it("shows a search that came from elsewhere in its field", async () => {
  render(Board);
  const field = (await screen.findByRole("searchbox", { name: "Поиск по карточкам" })) as HTMLInputElement;
  navigate("/board?q=backup");
  await vi.waitFor(() => expect(field.value).toBe("backup"));
  await vi.waitFor(() => expect(asked).toContain("/api/cards?q=backup"));
});
