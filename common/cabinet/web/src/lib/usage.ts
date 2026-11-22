// Token spend from kd-usage's ledger: per day and per agent. "Tokens" are what a model processed and was
// paid for (input, output, cache writes); cache reads are shown apart, they are cheap.
// The Hindsight server's own calls are a category of their own, not an agent: its rows carry
// category "hindsight" and a coverage — full, partial, or none (a row without numbers: no data, not a zero).
import type { Coverage, TokenPrice, UsageRow } from "./types";

export interface UsageSum {
  key: string;
  requests: number;
  tokens: number;
  cache_read: number;
  cost: number;
  cost_max: number;
  estimated: boolean;
  unpriced: boolean;
}

export interface HindsightSum extends UsageSum {
  coverage: Coverage;
}

export interface UsageDay {
  key: string;
  agents: number;
  /** Hindsight's tokens summed over its models; null — no data that day (before counting, or not read). */
  hindsight: number | null;
  coverage: Coverage | null;
}

export const isHindsight = (row: UsageRow): boolean => row.category === "hindsight";

export const tokens = (row: UsageRow): number => (row.input ?? 0) + (row.output ?? 0) + (row.cache_write ?? 0);

function sum(rows: UsageRow[], keyOf: (row: UsageRow) => string): UsageSum[] {
  const out = new Map<string, UsageSum>();
  for (const row of rows) {
    const key = keyOf(row);
    const into = out.get(key) ?? emptySum(key);
    into.requests += row.requests ?? 0;
    into.tokens += tokens(row);
    into.cache_read += row.cache_read ?? 0;
    into.cost += row.cost_usd ?? 0;
    into.cost_max += row.cost_max_usd ?? row.cost_usd ?? 0;
    into.estimated ||= row.cost_source === "estimated";
    into.unpriced ||= row.cost_usd === undefined || row.cost_source === "unknown";
    out.set(key, into);
  }
  return [...out.values()].map((s) => ({ ...s, cost: Math.round(s.cost * 1e6) / 1e6, cost_max: Math.round(s.cost_max * 1e6) / 1e6 }));
}

const emptySum = (key: string): UsageSum =>
  ({ key, requests: 0, tokens: 0, cache_read: 0, cost: 0, cost_max: 0, estimated: false, unpriced: false });

/** Only models used in the selected period; dated snapshots share their model's tariff. */
export function prices(rows: UsageRow[]): TokenPrice[] {
  const found = new Map<string, TokenPrice>();
  for (const row of rows) if (row.price) found.set(row.price.model, row.price);
  return [...found.values()].sort((a, b) => a.model.localeCompare(b.model));
}

/** Missing prices stay visible; a confirmed zero is $0.00. */
export function money(row: UsageSum): string {
  if (row.unpriced && row.cost_max === 0) return "—";
  const dollars = (n: number) => `$${n.toFixed(2)}`;
  const value = row.cost_max > row.cost ? `${dollars(row.cost)}–${dollars(row.cost_max)}` : dollars(row.cost);
  return `${row.unpriced ? "≥ " : row.estimated ? "≈ " : ""}${value}`;
}

const agentRows = (rows: UsageRow[]) => rows.filter((r) => !isHindsight(r));

export const byAgent = (rows: UsageRow[]): UsageSum[] =>
  sum(agentRows(rows), (r) => r.agent).sort((a, b) => b.tokens - a.tokens || a.key.localeCompare(b.key));

/** A model without its dated snapshot suffix: claude-haiku-4-5-20251001 → claude-haiku-4-5. */
export const modelOf = (row: UsageRow): string => row.price?.model ?? (row.model || "?").replace(/-\d{8}$/, "");

const byCost = (a: UsageSum, b: UsageSum) => b.cost - a.cost || b.tokens - a.tokens || a.key.localeCompare(b.key);

/** One agent's spend per model, the costliest first. */
export const agentModels = (rows: UsageRow[], agent: string): UsageSum[] =>
  sum(agentRows(rows).filter((r) => r.agent === agent), modelOf).sort(byCost);

export interface ModelHistory {
  /** The period's models, the costliest first — the history's columns. */
  models: string[];
  /** Newest day first; cells keyed by model, a model unused that day is absent. */
  days: { key: string; cells: Record<string, UsageSum>; total: UsageSum }[];
}

/** The agents' spend per day and model (Hindsight is apart, as in the chart). */
export function modelHistory(rows: UsageRow[]): ModelHistory {
  const mine = agentRows(rows);
  const models = sum(mine, modelOf).sort(byCost).map((s) => s.key);
  const days = sum(mine, (r) => r.day)
    .sort((a, b) => b.key.localeCompare(a.key))
    .map((total) => {
      const cells: Record<string, UsageSum> = {};
      for (const cell of sum(mine.filter((r) => r.day === total.key), modelOf)) cells[cell.key] = cell;
      return { key: total.key, cells, total };
    });
  return { models, days };
}

const RANK: Record<Coverage, number> = { none: 0, partial: 1, full: 2 };
const coverageOf = (row: UsageRow): Coverage => (row.coverage && row.coverage in RANK ? row.coverage : "none");
const worse = (a: Coverage, b: Coverage): Coverage => (RANK[a] <= RANK[b] ? a : b);

/** Every day of the ledger: the agents' tokens and Hindsight's, its models summed. */
export function days(rows: UsageRow[]): UsageDay[] {
  const out = new Map<string, UsageDay>();
  const day = (key: string) => {
    const found = out.get(key) ?? { key, agents: 0, hindsight: null, coverage: null };
    out.set(key, found);
    return found;
  };
  for (const row of rows) {
    const into = day(row.day);
    if (!isHindsight(row)) {
      into.agents += tokens(row);
      continue;
    }
    const coverage = coverageOf(row);
    into.coverage = into.coverage === null ? coverage : worse(into.coverage, coverage);
    if (coverage !== "none") into.hindsight = (into.hindsight ?? 0) + tokens(row);
  }
  for (const d of out.values()) if (d.coverage === "none") d.hindsight = null;
  return [...out.values()].sort((a, b) => a.key.localeCompare(b.key));
}

/** Hindsight over the period: known numbers only; coverage — the worst of its days. null — no rows at all. */
export function hindsight(rows: UsageRow[]): HindsightSum | null {
  const mine = rows.filter(isHindsight);
  if (mine.length === 0) return null;
  const out: HindsightSum = { ...sum(mine, () => "hindsight")[0], coverage: "full" };
  for (const row of mine) {
    out.coverage = worse(out.coverage, coverageOf(row));
  }
  // a day without data in a period that has some is a gap: the period is partial, not "no data"
  if (out.coverage === "none" && mine.some((r) => coverageOf(r) !== "none")) out.coverage = "partial";
  return out;
}

/** The period's total: the agents and Hindsight, each counted once. */
export function total(rows: UsageRow[]): UsageSum {
  return sum(rows, () => "total")[0] ?? emptySum("total");
}

/** 1234 → "1.2 k", 3_400_000 → "3.4 M". */
export function short(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)} k`;
  return String(n);
}
