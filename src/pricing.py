"""Token counts to micro-USD. Pure: no AWS, no I/O, no clock, raises nothing."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

__all__ = ["Rates", "Usage", "Priced", "price", "usage_from_log", "RATES", "FALLBACK"]


class Rates(NamedTuple):
    """USD per million tokens, per token class."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


class Usage(NamedTuple):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class Priced(NamedTuple):
    micros: int
    rate_card: str
    is_fallback: bool


def _d(value: str) -> Decimal:
    return Decimal(value)


# USD per million tokens, on-demand, us-east-1. THESE MUST BE VERIFIED against
# https://aws.amazon.com/bedrock/pricing/ before enforcement is switched on, and
# reconciled monthly thereafter. See README.md, the watch-only week.
#
# Four rates per model, not two: cache reads bill at a tenth of input and these
# workloads lean on caching, so collapsing them overstates the expensive flows.
RATES: dict[str, Rates] = {
    "anthropic.claude-sonnet-4-5-20250929-v1:0": Rates(
        input=_d("3.00"),
        output=_d("15.00"),
        cache_read=_d("0.30"),
        cache_write=_d("3.75"),
    ),
    "anthropic.claude-haiku-4-5-20251001-v1:0": Rates(
        input=_d("1.00"),
        output=_d("5.00"),
        cache_read=_d("0.10"),
        cache_write=_d("1.25"),
    ),
    "anthropic.claude-opus-4-1-20250805-v1:0": Rates(
        input=_d("15.00"),
        output=_d("75.00"),
        cache_read=_d("1.50"),
        cache_write=_d("18.75"),
    ),
}

# Unlisted model. Deliberately above Opus: raising would leave the invocation
# unrecorded, which is an unlimited budget. `is_fallback` drives a daily alert.
FALLBACK = Rates(
    input=_d("20.00"),
    output=_d("100.00"),
    cache_read=_d("2.00"),
    cache_write=_d("25.00"),
)

# Cross-region inference profiles prefix the model id with a geography.
_GEO_PREFIXES = ("us.", "eu.", "apac.", "us-gov.", "global.")


def normalize_model_id(model_id: str) -> str:
    """Strip a geography prefix (`us.`, `eu.`...) so one card entry serves all."""
    mid = (model_id or "").strip()
    for prefix in _GEO_PREFIXES:
        if mid.startswith(prefix):
            return mid[len(prefix) :]
    return mid


def lookup(model_id: str, region: str | None = None) -> tuple[Rates, str, bool]:
    """Return (rates, rate_card_key, is_fallback). `region` is accepted but unused."""
    key = normalize_model_id(model_id)
    rates = RATES.get(key)
    if rates is None:
        return FALLBACK, "FALLBACK", True
    return rates, key, False


def price(usage: Usage, model_id: str, region: str | None = None) -> Priced:
    """Cost of one invocation in micro-USD. tokens * usd_per_million, no division."""
    rates, card, is_fallback = lookup(model_id, region)
    total = (
        Decimal(usage.input_tokens) * rates.input
        + Decimal(usage.output_tokens) * rates.output
        + Decimal(usage.cache_read_tokens) * rates.cache_read
        + Decimal(usage.cache_write_tokens) * rates.cache_write
    )
    micros = int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return Priced(micros=micros, rate_card=card, is_fallback=is_fallback)


def usage_from_log(entry: dict) -> Usage:
    """The four token counts from one log entry. A missing field means zero."""
    # The cache fields are real but absent from AWS's documented schema table.
    # Do not delete them after reading the reference -- confirm the spelling
    # against a real log line instead.
    inp = entry.get("input") or {}
    out = entry.get("output") or {}
    return Usage(
        input_tokens=int(inp.get("inputTokenCount") or 0),
        output_tokens=int(out.get("outputTokenCount") or 0),
        cache_read_tokens=int(inp.get("cacheReadInputTokenCount") or 0),
        cache_write_tokens=int(inp.get("cacheWriteInputTokenCount") or 0),
    )
