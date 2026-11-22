<script lang="ts">
  // Token spend: two bars per day — the agents and the Hindsight server (its models summed) — and a
  // table per agent with Hindsight as its own row and the period's total, from kd-usage's ledger. A day
  // without Hindsight data has no Hindsight bar (not a zero); a partial day's bar is faded and says so.
  // An agent's row opens into its models; the history below gives the agents' cost per day and model.
  import { t } from "../lib/i18n.svelte";
  import type { Coverage, UsageRow } from "../lib/types";
  import {
    agentModels, byAgent, days as perDay, hindsight as hindsightSum, modelHistory, money, prices, short, total as totalOf,
  } from "../lib/usage";

  let { rows }: { rows: UsageRow[] } = $props();

  let open = $state<Record<string, boolean>>({});
  const days = $derived(perDay(rows));
  const agents = $derived(byAgent(rows));
  const history = $derived(modelHistory(rows));
  const bare = (model: string) => model.replace(/^claude-/, "");
  const hs = $derived(hindsightSum(rows));
  const total = $derived(totalOf(rows));
  const rates = $derived(prices(rows));
  const top = $derived(Math.max(1, ...days.map((d) => Math.max(d.agents, d.hindsight ?? 0))));
  const W = 28;
  const H = 120;
  const BAR = (W - 10) / 2;
  const height = (n: number) => Math.max(2, Math.round((n / top) * H));
  const coverageNote = (c: Coverage | null) =>
    c === "partial" ? t("usage.partial") : c === "full" ? "" : t("usage.nodata");
  const hsTitle = (n: number | null, c: Coverage | null) =>
    `${t("usage.hindsight")}: ${n === null ? t("usage.nodata") : short(n)}${c === "partial" ? ` (${t("usage.partial")})` : ""}`;
</script>

{#if rows.length === 0}
  <p class="empty small">{t("usage.empty")}</p>
{:else}
  <div class="chart">
    <p class="legend">
      <span class="key agents"></span>{t("usage.agents")}
      <span class="key hs"></span>{t("usage.hindsight")}
    </p>
    <svg viewBox="0 0 {days.length * W} {H + 18}" width={days.length * W} height={H + 18} role="img" aria-label={t("usage.chart")}>
      {#each days as day, i (day.key)}
        <g>
          <title>{day.key}: {t("usage.agents")} {short(day.agents)} · {hsTitle(day.hindsight, day.coverage)}</title>
          <rect x={i * W + 5} y={H - height(day.agents)} width={BAR} height={height(day.agents)} rx="2" class="bar" />
          {#if day.hindsight !== null}
            <rect
              x={i * W + 5 + BAR}
              y={H - height(day.hindsight)}
              width={BAR}
              height={height(day.hindsight)}
              rx="2"
              class="bar hs"
              class:partial={day.coverage === "partial"}
            />
          {/if}
          <text x={i * W + W / 2} y={H + 14} class="label">{day.key.slice(8)}</text>
        </g>
      {/each}
    </svg>
  </div>
  <div class="wrap">
    <table class="usage spend">
      <thead>
        <tr>
          <th scope="col">{t("usage.agent")}</th>
          <th scope="col">{t("usage.requests")}</th>
          <th scope="col">{t("usage.tokens")}</th>
          <th scope="col">{t("usage.cache_read")}</th>
          <th scope="col">{t("usage.cost")}</th>
        </tr>
      </thead>
      <tbody>
        {#each agents as row (row.key)}
          <tr>
            <th scope="row">
              <button
                type="button"
                class="toggle"
                aria-expanded={!!open[row.key]}
                title={t("usage.by_model")}
                onclick={() => (open[row.key] = !open[row.key])}
              >{open[row.key] ? "▾" : "▸"} @{row.key}</button>
            </th>
            <td>{row.requests}</td>
            <td>{short(row.tokens)}</td>
            <td>{short(row.cache_read)}</td>
            <td>{money(row)}</td>
          </tr>
          {#if open[row.key]}
            {#each agentModels(rows, row.key) as model (model.key)}
              <tr class="sub">
                <th scope="row">{bare(model.key)}</th>
                <td>{model.requests}</td>
                <td>{short(model.tokens)}</td>
                <td>{short(model.cache_read)}</td>
                <td>{money(model)}</td>
              </tr>
            {/each}
          {/if}
        {/each}
        {#if hs}
          <tr class="hs-row">
            <th scope="row">
              {t("usage.hindsight")}
              {#if hs.coverage !== "full"}<span class="note">{coverageNote(hs.coverage)}</span>{/if}
            </th>
            {#if hs.coverage === "none"}
              <td>—</td><td>—</td><td>—</td>
            {:else}
              <td>{hs.requests}</td>
              <td>{short(hs.tokens)}</td>
              <td>{short(hs.cache_read)}</td>
            {/if}
            <td>{money(hs)}</td>
          </tr>
        {/if}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row">{t("usage.total")}</th>
          <td>{total.requests}</td>
          <td>{short(total.tokens)}</td>
          <td>{short(total.cache_read)}</td>
          <td>{money(total)}</td>
        </tr>
      </tfoot>
    </table>
  </div>
  {#if history.days.length}
    <details class="history">
      <summary>{t("usage.history")}</summary>
      <div class="wrap">
        <table class="usage spend">
          <thead><tr>
            <th scope="col">{t("usage.day")}</th>
            {#each history.models as model (model)}<th scope="col">{bare(model)}</th>{/each}
            <th scope="col">{t("usage.total")}</th>
          </tr></thead>
          <tbody>
            {#each history.days as day (day.key)}
              <tr>
                <th scope="row">{day.key.slice(5)}</th>
                {#each history.models as model (model)}
                  <td title={day.cells[model] ? `${short(day.cells[model].tokens)} · ${day.cells[model].requests}` : ""}>
                    {day.cells[model] ? money(day.cells[model]) : ""}
                  </td>
                {/each}
                <td>{money(day.total)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="muted small">{t("usage.history_hint")}</p>
    </details>
  {/if}
  <p class="muted small">{t("usage.cost_basis")}</p>
  {#if total.cost_max > total.cost}<p class="muted small">{t("usage.long_context")}</p>{/if}
  {#if total.unpriced}<p class="note">{t("usage.unpriced")}</p>{/if}
  {#if rates.length}
    <details class="prices">
      <summary>{t("usage.prices")}</summary>
      <p class="muted small">{t("usage.prices_hint")}</p>
      <div class="wrap">
        <table class="usage">
          <thead><tr>
            <th scope="col">{t("usage.model")}</th>
            <th scope="col">{t("usage.input")}</th>
            <th scope="col">{t("usage.output")}</th>
            <th scope="col">{t("usage.write_5m")}</th>
            <th scope="col">{t("usage.write_1h")}</th>
            <th scope="col">{t("usage.cache_read")}</th>
          </tr></thead>
          <tbody>
            {#each rates as rate (rate.model)}
              <tr>
                <th scope="row">{rate.model}{#if rate.long_context} ≤100k{/if}</th>
                <td>${rate.input}</td><td>${rate.output}</td><td>${rate.cache_write}</td><td>${rate.cache_write_1h}</td><td>${rate.cache_read}</td>
              </tr>
              {#if rate.long_context}
                <tr>
                  <th scope="row">{rate.model} &gt;100k</th>
                  <td>${rate.long_context.input}</td><td>${rate.long_context.output}</td><td>${rate.long_context.cache_write}</td>
                  <td>${rate.long_context.cache_write_1h}</td><td>${rate.long_context.cache_read}</td>
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
      <p class="muted small"><a href={rates[0].source} target="_blank" rel="noreferrer">{t("usage.prices_source")}</a> · {rates[0].updated}</p>
    </details>
  {/if}
{/if}

<style>
  .chart { overflow-x: auto; padding: 4px 0 8px; }
  .bar { fill: var(--accent); }
  .bar.hs { fill: var(--info); }
  .bar.hs.partial { opacity: 0.45; }
  .label { fill: var(--muted); font-size: 10px; text-anchor: middle; }
  .legend { display: flex; align-items: center; gap: 6px; margin: 0 0 4px; color: var(--muted); font-size: 12px; }
  .legend .key { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-left: 6px; }
  .legend .key:first-child { margin-left: 0; }
  .legend .key.agents { background: var(--accent); }
  .legend .key.hs { background: var(--info); }
  .wrap { overflow-x: auto; }
  .usage { border-collapse: collapse; width: 100%; font-size: 14px; }
  .usage th, .usage td { padding: 7px 6px; border-bottom: 1px solid var(--line-soft); text-align: right; white-space: nowrap; }
  .usage th[scope="row"], .usage thead th:first-child { text-align: left; }
  .usage thead th { font-size: 12px; color: var(--muted); font-weight: 500; }
  .usage tfoot th, .usage tfoot td { font-weight: 600; border-bottom: 0; }
  .note { margin-left: 6px; font-size: 12px; font-weight: 400; color: var(--warn); }
  .small { padding: 8px 0; }
  .prices summary, .history summary { cursor: pointer; font-weight: 500; }
  .history { margin-top: 8px; }
  .toggle { all: unset; cursor: pointer; }
  .toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .sub th, .sub td { color: var(--muted); font-size: 13px; }
  .sub th[scope="row"] { padding-left: 22px; }
  @media (max-width: 600px) {
    .spend { font-size: 12px; }
    .spend th, .spend td { padding: 7px 3px; }
    .spend thead th { font-size: 11px; }
  }
</style>
