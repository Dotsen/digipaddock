// A project's chip: its name, edged in the project's colour once the projects are read.
import { cleanup, render } from "@testing-library/svelte";
import { afterEach, expect, it } from "vitest";
import { projects } from "../lib/stores/projects.svelte";
import ProjectChip from "./ProjectChip.svelte";

afterEach(() => {
  cleanup();
  projects.reset();
});

it("names the project and wears its colour; before the projects are read, its id on a plain edge", async () => {
  const { container, rerender } = render(ProjectChip, { id: "shop" });
  const chip = () => container.querySelector<HTMLElement>(".chip")!;
  expect([chip().textContent, chip().style.getPropertyValue("--pc")]).toEqual(["shop", ""]);
  projects.list = [{ id: "shop", name: "Shop", route: "@dev" }];
  await rerender({ id: "shop" });
  expect(chip().textContent).toBe("Shop");
  expect(chip().style.getPropertyValue("--pc")).toMatch(/^var\(--project-[0-5]\)$/);
});
