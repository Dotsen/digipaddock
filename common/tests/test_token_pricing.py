"""Reference estimates fill missing costs without disguising incomplete data."""

import pytest

from kd.token_pricing import price_for, with_price


def test_hindsight_haiku_45_includes_cache_reads_and_writes():
    row = {
        "category": "hindsight",
        "coverage": "full",
        "model": "claude-haiku-4-5-20251001",
        "input": 25820,
        "output": 4376521,
        "cache_write": 6754755,
        "cache_read": 326926,
    }
    got = with_price(row)
    assert got["cost_usd"] == pytest.approx(30.384561)
    assert got["cost_source"] == "estimated"
    assert got["price"]["model"] == "claude-haiku-4-5"
    assert "cost_usd" not in row  # history stays untouched


def test_haiku_55_daily_totals_cannot_choose_a_per_request_tier():
    row = {
        "model": "claude-haiku-5-5",
        "input": 1_000_000,
        "output": 1_000_000,
        "cache_write": 1_000_000,
        "cache_read": 1_000_000,
    }
    got = with_price(row)
    assert got["cost_usd"] == 0.735
    assert got["cost_max_usd"] == 3.675
    assert got["price"]["long_context"]["threshold"] == 100000


def test_reported_cost_is_preserved_and_unknown_model_is_not_free():
    assert (
        with_price({"model": "new-model", "cost_usd": 7})["cost_source"] == "reported"
    )
    assert with_price({"model": "claude-opus-5-5", "cost_usd": 7})["cost_usd"] == 7
    assert with_price({"model": "?", "input": 100})["cost_source"] == "unknown"
    assert "cost_usd" not in with_price({"model": "?", "cost_usd": float("nan")})
    assert price_for("claude-opus-5-5-unknown") is None


def test_a_cabinet_models_table_prices_its_slug():
    """the cabinet's models (``llm_models``) price their own slugs — before the reference list, with their
    one cache-write price for both lifetimes."""
    row = {"id": "or-x", "price_in": 2, "price_out": 10, "price_cache_read": 0.2, "price_cache_write": 2.5,
           "updated": "2026-11-05T10:00:00Z"}
    table = {"or-x": row, "claude-opus-5-5": {**row, "id": "claude-opus-5-5"}}
    assert price_for("or-x", table=table) == {
        "model": "or-x", "input": 2, "output": 10, "cache_write": 2.5, "cache_write_1h": 2.5, "cache_read": 0.2,
        "source": "cabinet", "updated": "2026-11-05T10:00:00Z"}
    assert price_for("or-x") is None
    assert price_for("claude-opus-5-5", table=table)["source"] == "cabinet"
    assert price_for("claude-sonnet-5-5", table=table)["input"] == 2  # the reference list for the rest
    got = with_price({"model": "or-x", "input": 1_000_000, "output": 0, "cache_write": 0, "cache_read": 0}, table=table)
    assert got["cost_usd"] == 2.0 and got["cost_source"] == "estimated" and got["price"]["source"] == "cabinet"
    assert with_price({"model": "or-x", "input": 1})["cost_source"] == "unknown"


def test_missing_data_is_not_a_zero_but_confirmed_zero_is_priced():
    assert "cost_usd" not in with_price(
        {"category": "hindsight", "coverage": "none", "cost_usd": 0}
    )
    assert (
        with_price(
            {"category": "hindsight", "coverage": "full", "model": "-", "requests": 0}
        )["cost_usd"]
        == 0
    )
