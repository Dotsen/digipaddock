// A card's tile: its project chip in the project's colour; a card put off until a time is shaded as a whole.
import { cleanup, render } from "@testing-library/svelte";
import { afterEach, expect, it } from "vitest";
import { projects } from "../lib/stores/projects.svelte";
import { compact, type CardCompact } from "../lib/types";
import CardRow from "./CardRow.svelte";

const CARD: CardCompact = compact({
  id: "KD-7", n: 7, status: "To Do", assignee: "@dev", project: "shop", title: "Nightly backup check", labels: [],
  servers: [], priority: "", due: "", parent: "", dependencies: [], revision: 1, created: "2026-11-25T10:00:00Z",
  updated: "2026-11-25T10:00:00Z", defer: null,
});

afterEach(() => {
  cleanup();
  projects.reset();
});

it("edges the project chip in the project's colour once the projects are read", async () => {
  const { container, rerender } = render(CardRow, { card: CARD });
  const chip = () => container.querySelector<HTMLElement>(".project")!;
  expect(chip().textContent).toBe("shop");
  expect(chip().style.getPropertyValue("--pc")).toBe("");  // not read yet: the chip's plain edge
  projects.list = [{ id: "shop", name: "Shop", route: "@dev" }, { id: "alert", name: "Alert", route: "@dev" }];
  await rerender({ card: { ...CARD } });
  expect(chip().textContent).toBe("Shop");
  expect(chip().style.getPropertyValue("--pc")).toMatch(/^var\(--project-[0-5]\)$/);
});

it("shades a card put off until a time and leaves a card in work as it is", () => {
  const { container } = render(CardRow, { card: { ...CARD, defer: { until: "2026-11-26T09:00:00Z", source: "owner", by: "owner", reason: "после релиза",
                                                                  created: "2026-11-25T10:00:00Z" } } });
  const tile = container.querySelector<HTMLElement>("a.bc")!;
  expect(tile.classList.contains("deferred")).toBe(true);
  expect(tile.querySelector(".defer")?.getAttribute("title")).toBe("после релиза");
  cleanup();
  const plain = render(CardRow, { card: CARD }).container.querySelector<HTMLElement>("a.bc")!;
  expect(plain.classList.contains("deferred")).toBe(false);
});
