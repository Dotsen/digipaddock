import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setLocale } from "../lib/i18n.svelte";
import type { ContourRow } from "../lib/types";
import Contours from "./Contours.svelte";

function row(over: Partial<ContourRow> = {}): ContourRow {
  return { name: "app-ci", host: "local", components: ["digipaddock", "digipaddock-deploy"], exclusive: true, capacity: 1,
    state: "idle", running: [], deployed: {}, reason: "", since: "", ...over };
}

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("Contours", () => {
  it("names each contour with its state and capacity", () => {
    render(Contours, { rows: [row()] });
    expect(screen.getByText("Контуры проверок")).toBeTruthy();
    expect(screen.getByText("app-ci")).toBeTruthy();
    expect(screen.getByText("свободен")).toBeTruthy();
    expect(screen.getByText(/мест: 1/)).toBeTruthy();
    expect(screen.getByText(/digipaddock, digipaddock-deploy/)).toBeTruthy();
  });

  it("a busy contour links the card of what runs there", () => {
    const running = [{ operation: "a".repeat(32), card: "KD-50", project: "digipaddock", pr: 7, head: "b".repeat(40) }];
    render(Contours, { rows: [row({ state: "busy", running })] });
    expect(screen.getByText("идёт проверка")).toBeTruthy();
    const link = screen.getByRole("link", { name: /KD-50/ });
    expect(link.getAttribute("href")).toBe("/cards/KD-50");
    expect(link.textContent).toContain("digipaddock#7");
    expect(link.textContent).toContain("bbbbbbb");
  });

  it("a contour in maintenance says why", () => {
    render(Contours, { rows: [row({ state: "maintenance", reason: "диск заполнен" })] });
    expect(screen.getByText("на обслуживании")).toBeTruthy();
    expect(screen.getByText(/диск заполнен/)).toBeTruthy();
  });

  it("no contours — nothing on the page", () => {
    const { container } = render(Contours, { rows: [] });
    expect(container.textContent?.trim()).toBe("");
  });
});
