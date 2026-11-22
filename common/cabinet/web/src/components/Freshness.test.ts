import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/api";
import { setLocale } from "../lib/i18n.svelte";
import type { Synced } from "../lib/synced.svelte";
import Freshness from "./Freshness.svelte";

function data(over: Partial<Synced> = {}): Synced {
  return { stale: false, updated: 0, error: null, run: vi.fn(async () => {}), dispose: vi.fn(), ...over };
}

const at = (h: number, m: number) => new Date(2026, 9, 17, h, m).getTime();

beforeEach(() => setLocale("ru"));
afterEach(cleanup);

describe("Freshness", () => {
  it("says when the data was read", () => {
    render(Freshness, { data: data({ updated: at(9, 5) }) });
    expect(screen.getByText("Обновлено 09:05")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("a failed refresh over shown data offers a retry", async () => {
    const shown = data({ updated: at(9, 5), stale: true, error: new ApiError(0, "network", "") });
    render(Freshness, { data: shown });
    expect(screen.getByText(/Не удалось обновить/)).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(shown.run).toHaveBeenCalledTimes(1);
  });

  it("a first load that failed shows the error and a retry instead of loading", async () => {
    const none = data({ stale: true, error: new ApiError(503, "unavailable", "") });
    render(Freshness, { data: none });
    expect(screen.queryByText(/Обновлено/)).toBeNull();
    expect(screen.getByRole("alert").textContent).not.toBe("");
    await fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(none.run).toHaveBeenCalledTimes(1);
  });

  it("shows nothing while the first load is going", () => {
    const { container } = render(Freshness, { data: data() });
    expect(container.textContent?.trim()).toBe("");
  });
});
