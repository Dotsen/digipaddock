import { describe, expect, it } from "vitest";
import type { UsageRow } from "./types";
import { agentModels, byAgent, days, hindsight, modelHistory, modelOf, money, prices, tokens, total } from "./usage";

const rows = [
  { day: "2026-10-14", agent: "dev", model: "a", requests: 3, input: 100, output: 50, cache_write: 10, cache_read: 900 },
  { day: "2026-10-14", agent: "dev", model: "b", requests: 1, input: 10, output: 5, cache_write: 0, cache_read: 0, cost_usd: 0.25 },
  { day: "2026-10-15", agent: "boss", model: "a", requests: 2, input: 20, output: 10 },
];

describe("usage", () => {
  it("counts the tokens a model was paid for (cache reads apart)", () => {
    expect(tokens(rows[0])).toBe(160);
    expect(tokens(rows[2])).toBe(30);
  });

  it("sums by agent, the busiest first", () => {
    expect(byAgent(rows).map((r) => [r.key, r.tokens])).toEqual([
      ["dev", 175],
      ["boss", 30],
    ]);
  });
});

// an agent's spend per model and the history per day and model
describe("usage by model", () => {
  const priced: UsageRow[] = [
    { day: "2026-10-14", agent: "dev", model: "claude-opus-5-5", requests: 5, input: 10, output: 10, cost_usd: 3 },
    { day: "2026-10-14", agent: "dev", model: "claude-fable-5-1", requests: 1, input: 5, output: 5, cost_usd: 4 },
    { day: "2026-10-15", agent: "dev", model: "claude-haiku-4-5-20251001", requests: 2, input: 1, output: 1, cost_usd: 0.5 },
    { day: "2026-10-15", agent: "boss", model: "claude-opus-5-5", requests: 1, input: 1, output: 1, cost_usd: 1 },
    { day: "2026-10-15", agent: "hindsight", category: "hindsight", coverage: "full", model: "claude-opus-5-5", requests: 9, input: 9, output: 9, cost_usd: 9 },
  ];

  it("drops the snapshot date from a model", () => {
    expect(modelOf(priced[2])).toBe("claude-haiku-4-5");
    expect(modelOf({ day: "d", agent: "a" })).toBe("?");
  });

  it("splits one agent by model, the costliest first", () => {
    expect(agentModels(priced, "dev").map((m) => [m.key, m.cost, m.requests])).toEqual([
      ["claude-fable-5-1", 4, 1],
      ["claude-opus-5-5", 3, 5],
      ["claude-haiku-4-5", 0.5, 2],
    ]);
    expect(agentModels(priced, "hindsight")).toEqual([]);
  });

  it("gives the agents' history newest day first, models as columns, Hindsight apart", () => {
    const h = modelHistory(priced);
    expect(h.models).toEqual(["claude-opus-5-5", "claude-fable-5-1", "claude-haiku-4-5"]);
    expect(h.days.map((d) => [d.key, d.total.cost, Object.keys(d.cells).sort()])).toEqual([
      ["2026-10-15", 1.5, ["claude-haiku-4-5", "claude-opus-5-5"]],
      ["2026-10-14", 7, ["claude-fable-5-1", "claude-opus-5-5"]],
    ]);
    expect(h.days[0].cells["claude-opus-5-5"].cost).toBe(1);
  });
});

// the Hindsight server's spend — a category of its own, models summed, no data is not a zero
const hs = (day: string, coverage: "full" | "partial" | "none", n?: number, model = "claude-haiku-4-5"): UsageRow =>
  n === undefined
    ? { day, agent: "hindsight", category: "hindsight", coverage, model: "-" }
    : { day, agent: "hindsight", category: "hindsight", coverage, model, requests: 2, input: n, output: 0, cache_write: 0, cache_read: 7 };

const withHs: UsageRow[] = [
  ...rows,
  hs("2026-10-14", "full", 1000),
  hs("2026-10-14", "full", 500, "claude-sonnet-5-5"),
  hs("2026-10-15", "partial", 40),
  hs("2026-10-16", "none"),
];

describe("usage with Hindsight", () => {
  it("keeps Hindsight out of the agents' sums", () => {
    expect(byAgent(withHs).map((r) => r.key)).toEqual(["dev", "boss"]);
    expect(byAgent(withHs)).toEqual(byAgent(rows));
  });

  it("gives every day the agents and Hindsight apart, its models summed, no data as null", () => {
    expect(days(withHs)).toEqual([
      { key: "2026-10-14", agents: 175, hindsight: 1500, coverage: "full" },
      { key: "2026-10-15", agents: 30, hindsight: 40, coverage: "partial" },
      { key: "2026-10-16", agents: 0, hindsight: null, coverage: "none" },
    ]);
    expect(days(rows).every((d) => d.hindsight === null && d.coverage === null)).toBe(true);
  });

  it("sums Hindsight over the period with its worst coverage", () => {
    expect(hindsight(withHs)).toEqual({ key: "hindsight", requests: 6, tokens: 1540, cache_read: 21, coverage: "partial", cost: 0, cost_max: 0, estimated: false, unpriced: true });
    expect(hindsight([hs("2026-10-16", "none")])).toEqual({ key: "hindsight", requests: 0, tokens: 0, cache_read: 0, coverage: "none", cost: 0, cost_max: 0, estimated: false, unpriced: true });
    expect(hindsight(rows)).toBeNull();
  });

  it("counts Hindsight once in the total and marks unpriced usage", () => {
    expect(total(withHs)).toEqual({ key: "total", requests: 12, tokens: 175 + 30 + 1540, cache_read: 921, cost: 0.25, cost_max: 0.25, estimated: false, unpriced: true });
    expect(total(rows).tokens).toBe(205);
  });

  it("includes Hindsight's cost and updates sums for a longer period", () => {
    const recent: UsageRow[] = [
      { ...rows[1], cost_usd: 2, cost_source: "reported" },
      { ...hs("2026-10-14", "full", 1000), cost_usd: 3, cost_source: "estimated" },
    ];
    expect(money(total(recent))).toBe("≈ $5.00");
    expect(money(hindsight(recent)!)).toBe("≈ $3.00");
    expect(money(total([...recent, { ...rows[0], cost_usd: 10, cost_source: "estimated" }]))).toBe("≈ $15.00");
  });

  it("distinguishes zero, unknown, incomplete and tiered cost", () => {
    expect(money(total([{ ...rows[0], cost_usd: 0 }]))).toBe("$0.00");
    expect(money(total([rows[0]]))).toBe("—");
    expect(money(total(rows))).toBe("≥ $0.25");
    expect(money(total([{ ...rows[0], cost_usd: 1, cost_max_usd: 5, cost_source: "estimated" }]))).toBe("≈ $1.00–$5.00");
    expect(prices(rows)).toEqual([]);
  });
});
