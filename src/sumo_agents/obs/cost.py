"""Pricing table + cost calculation for OpenAI Responses API usage
(IMPLEMENTATION_PLAN.md section 1.2, STEPS.md Step 10).

Kept separate from `agents/llm.py` so the pricing table can be updated (the
plan's own prices are already a snapshot -- OpenAI's list changes) without
touching the call site itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_PER_MILLION = Decimal(1) / Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Pricing:
    """USD per 1,000,000 tokens."""

    input_per_1m: Decimal
    output_per_1m: Decimal
    cached_input_per_1m: Decimal


# IMPLEMENTATION_PLAN.md section 1.2's table. `cached_input_per_1m` prices
# cache HITS (a subset of input_tokens, not additional tokens) at the
# discounted rate; the rest of input_tokens is priced at input_per_1m.
PRICING: dict[str, Pricing] = {
    "gpt-6-astra": Pricing(Decimal("10.00"), Decimal("50.00"), Decimal("1.00")),
    "gpt-5.6-sol": Pricing(Decimal("4.00"), Decimal("20.00"), Decimal("0.40")),
    "gpt-5.6-terra": Pricing(Decimal("2.00"), Decimal("12.00"), Decimal("0.20")),
    "gpt-5.6-luna": Pricing(Decimal("0.20"), Decimal("1.20"), Decimal("0.02")),
}


def compute_cost_usd(model: str, *, input_tokens: int, cached_tokens: int, output_tokens: int) -> Decimal:
    """Cost of one Responses API call.

    `output_tokens` already includes reasoning tokens (OpenAI bills reasoning
    output at the same output rate, it is a breakdown of output_tokens, not
    an addition to it) so it is used as-is, not summed with
    `reasoning_tokens` again.
    """
    pricing = PRICING.get(model)
    if pricing is None:
        raise ValueError(f"no pricing entry for model {model!r} -- add it to obs/cost.py PRICING")

    uncached_input_tokens = max(input_tokens - cached_tokens, 0)
    return (
        uncached_input_tokens * pricing.input_per_1m
        + cached_tokens * pricing.cached_input_per_1m
        + output_tokens * pricing.output_per_1m
    ) * _PER_MILLION
