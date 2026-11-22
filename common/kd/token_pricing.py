"""Reference API prices in USD / million tokens, checked on 2026-10-21.

Keep reported Claude Code costs. Older daily ledgers have only token counts: price
those as estimates, with 5-minute cache writes. Daily Haiku 5.5 totals cannot reveal
which requests crossed 100k prompt tokens, so expose both bounds instead of guessing.
"""

import math
import re

SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
UPDATED = "2026-10-21"
# input, output, cache read; cache writes are 1.25x / 2x input for 5m / 1h.
RATES = {
    "claude-fable-5-1": (10, 50, 0.25),
    "claude-opus-5-5": (4, 20, 0.20),
    "claude-sonnet-5-5": (2, 10, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-opus-5": (5, 25, 0.50),
    "claude-opus-4-8": (5, 25, 0.50),
    "claude-opus-4-7": (5, 25, 0.50),
    "claude-opus-4-6": (5, 25, 0.50),
    "claude-opus-4-5": (5, 25, 0.50),
    "claude-sonnet-5": (2, 10, 0.20),
    "claude-sonnet-4-6": (3, 15, 0.30),
    "claude-sonnet-4-5": (3, 15, 0.30),
    "claude-haiku-4-5": (1, 5, 0.10),
}


def price_for(model, table=None):
    """The prices of ``model``: the cabinet's own models first (``table`` — slug to its ``llm_models`` row,
    one cache-write price for both lifetimes), then the reference list."""
    if table and model in table:
        row = table[model]
        return {
            "model": model,
            "input": row["price_in"],
            "output": row["price_out"],
            "cache_write": row["price_cache_write"],
            "cache_write_1h": row["price_cache_write"],
            "cache_read": row["price_cache_read"],
            "source": "cabinet",
            "updated": row.get("updated", ""),
        }
    model = re.sub(r"-\d{8}$", "", str(model or ""))
    if model not in RATES:
        return None
    incoming, outgoing, cached = RATES[model]
    price = {
        "model": model,
        "input": incoming,
        "output": outgoing,
        "cache_write": incoming * 1.25,
        "cache_write_1h": incoming * 2,
        "cache_read": cached,
        "source": SOURCE,
        "updated": UPDATED,
    }
    if model == "claude-haiku-5-5":
        price["long_context"] = {
            "input": 0.50,
            "output": 2.50,
            "cache_write": 0.625,
            "cache_write_1h": 1,
            "cache_read": 0.05,
            "threshold": 100000,
        }
    return price


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def with_price(row, table=None):
    """Enrich a copy for the UI; never rewrite the historical ledger."""
    out = dict(row)
    price = price_for(row.get("model"), table)
    if price:
        out["price"] = price
    if row.get("category") == "hindsight" and row.get("coverage") == "none":
        out.pop("cost_usd", None)
        out["cost_source"] = "unknown"
    elif _number(row.get("cost_usd")):
        out["cost_source"] = "reported"
    else:
        fields = ("input", "output", "cache_write", "cache_read")
        if price and all(_number(row.get(f)) for f in fields):
            out["cost_usd"] = round(
                sum(row[f] * price[f] for f in fields) / 1_000_000, 6
            )
            if "long_context" in price:
                out["cost_max_usd"] = round(
                    sum(row[f] * price["long_context"][f] for f in fields) / 1_000_000,
                    6,
                )
            out["cost_source"] = "estimated"
        elif row.get("coverage") == "full" and row.get("requests") == 0:
            out.update(cost_usd=0, cost_source="estimated")
        else:
            out.pop("cost_usd", None)
            out["cost_source"] = "unknown"
    return out
