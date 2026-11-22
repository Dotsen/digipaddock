import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, expect, it } from "vitest";
import { t } from "../lib/i18n.svelte";
import type { UsageRow } from "../lib/types";
import UsageChart from "./UsageChart.svelte";

afterEach(cleanup);

// an agent's row opens into its models; the history gives cost per day and model
const rows: UsageRow[] = [
  { day: "2026-10-21", agent: "dev", model: "claude-opus-5-5", requests: 5, input: 10, output: 10, cost_usd: 3, cost_source: "reported" },
  { day: "2026-10-22", agent: "dev", model: "claude-fable-5-1", requests: 1, input: 5, output: 5, cost_usd: 4, cost_source: "reported" },
];

it("opens an agent's row into its models", async () => {
  render(UsageChart, { rows });
  expect(screen.queryByRole("rowheader", { name: "fable-5-1" })).toBeNull();
  const toggle = screen.getByRole("button", { name: "▸ @dev" });
  await fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  const fable = screen.getAllByRole("rowheader", { name: "fable-5-1" })[0].closest("tr")!;
  expect(fable.classList.contains("sub")).toBe(true);
  expect(within(fable).getByText("$4.00")).toBeTruthy();
  await fireEvent.click(toggle);
  expect(document.querySelectorAll("tr.sub")).toHaveLength(0);
});

it("shows the history newest day first with a column per model", () => {
  render(UsageChart, { rows });
  const history = screen.getByText(t("usage.history")).closest("details")!;
  const heads = within(history).getAllByRole("columnheader").map((h) => h.textContent);
  expect(heads).toEqual([t("usage.day"), "fable-5-1", "opus-5-5", t("usage.total")]);
  const days = within(history).getAllByRole("rowheader").map((h) => h.textContent);
  expect(days).toEqual(["10-22", "10-21"]);
});
