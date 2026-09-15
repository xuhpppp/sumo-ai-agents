"""Unit tests for obs/cost.py (STEPS.md Step 10) -- pure arithmetic, no network."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sumo_agents.obs.cost import compute_cost_usd


def test_all_uncached_input_priced_at_input_rate() -> None:
    # 1,000,000 uncached input tokens @ $0.20/1M + 0 output = $0.20 exactly.
    cost = compute_cost_usd("gpt-5.6-luna", input_tokens=1_000_000, cached_tokens=0, output_tokens=0)
    assert cost == Decimal("0.20")


def test_cached_tokens_priced_at_discounted_rate_not_input_rate() -> None:
    # Same 1M input tokens, but all served from cache: luna's cached rate is
    # $0.02/1M, ten times cheaper than the $0.20/1M input rate.
    cost = compute_cost_usd("gpt-5.6-luna", input_tokens=1_000_000, cached_tokens=1_000_000, output_tokens=0)
    assert cost == Decimal("0.02")


def test_output_tokens_priced_separately() -> None:
    cost = compute_cost_usd("gpt-5.6-luna", input_tokens=0, cached_tokens=0, output_tokens=1_000_000)
    assert cost == Decimal("1.20")


def test_mixed_cached_and_uncached_input_plus_output() -> None:
    # 500k cached + 500k uncached input, 100k output.
    cost = compute_cost_usd("gpt-5.6-luna", input_tokens=1_000_000, cached_tokens=500_000, output_tokens=100_000)
    expected = Decimal("500000") * Decimal("0.20") / Decimal(1_000_000) + Decimal("500000") * Decimal(
        "0.02"
    ) / Decimal(1_000_000) + Decimal("100000") * Decimal("1.20") / Decimal(1_000_000)
    assert cost == expected


def test_unknown_model_raises() -> None:
    with pytest.raises(ValueError, match="no pricing entry"):
        compute_cost_usd("not-a-real-model", input_tokens=1, cached_tokens=0, output_tokens=1)
