"""Token counts to micro-USD. Pure: no AWS, no I/O, no clock, raises nothing."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

__all__ = [
    "Rates",
    "Usage",
    "PricedUsage",
    "price",
    "usage_from_log",
    "has_any_count_field",
    "MAX_PLAUSIBLE_TOKENS",
    "FALLBACK",
]

# The four count fields, spelled once. The cache two are real but absent from
# AWS's documented schema table -- do not delete them after reading the
# reference, confirm the spelling against a real log line instead.
INPUT_TOKEN_FIELD = "inputTokenCount"
OUTPUT_TOKEN_FIELD = "outputTokenCount"
CACHE_READ_FIELD = "cacheReadInputTokenCount"
CACHE_WRITE_FIELD = "cacheWriteInputTokenCount"

# A sanity bound, not a security boundary. The largest advertised Bedrock
# context is around a million tokens, so anything past this is corrupt or
# forged, never a real invocation. See the threat model in README.md.
MAX_PLAUSIBLE_TOKENS = 2_000_000


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


class PricedUsage(NamedTuple):
    micros: int
    rate_card: str
    is_fallback: bool


def _rate(value: str) -> Decimal:
    return Decimal(value)


# The only rates in this file. Used when the rate card has no row for a model --
# because it is new, because MODEL_IDS has no line for it, or because no refresh
# has run yet. Deliberately expensive: raising instead would leave the invocation
# unrecorded, which is an unlimited budget. `is_fallback` drives a daily alert.
FALLBACK = Rates(
    input=_rate("20.00"),
    output=_rate("100.00"),
    cache_read=_rate("2.00"),
    cache_write=_rate("25.00"),
)

# Cross-region inference profiles prefix the model id with a geography.
_GEO_PREFIXES = ("us.", "eu.", "apac.", "us-gov.", "global.")


def normalize_model_id(model_id: str) -> str:
    """Strip a geography prefix (`us.`, `eu.`...) so one card entry serves all."""
    normalized = (model_id or "").strip()
    for prefix in _GEO_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def price(usage: Usage, model_id: str, rates: Rates | None = None) -> PricedUsage:
    """Cost of one invocation in micro-USD. tokens * usd_per_million, no division."""
    # None means the rate card has no row for this model. There is no second card
    # to try: FALLBACK is the answer, and it is loud.
    is_fallback = rates is None
    rate_card_key = normalize_model_id(model_id) if rates is not None else "FALLBACK"
    rates = FALLBACK if rates is None else rates
    total_micros = (
        Decimal(usage.input_tokens) * rates.input
        + Decimal(usage.output_tokens) * rates.output
        + Decimal(usage.cache_read_tokens) * rates.cache_read
        + Decimal(usage.cache_write_tokens) * rates.cache_write
    )
    micros = int(total_micros.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return PricedUsage(micros=micros, rate_card=rate_card_key, is_fallback=is_fallback)


def usage_from_log(entry: dict) -> Usage:
    """The four token counts from one log entry. A missing field means zero."""
    input_counts = entry.get("input") or {}
    output_counts = entry.get("output") or {}
    return Usage(
        input_tokens=int(input_counts.get(INPUT_TOKEN_FIELD) or 0),
        output_tokens=int(output_counts.get(OUTPUT_TOKEN_FIELD) or 0),
        cache_read_tokens=int(input_counts.get(CACHE_READ_FIELD) or 0),
        cache_write_tokens=int(input_counts.get(CACHE_WRITE_FIELD) or 0),
    )


def has_any_count_field(entry: dict) -> bool:
    """True if the entry carries at least one of the four counts, at any value.

    Distinct from "all four are zero", which is a legitimate log line. A schema
    change that renames the fields would otherwise price every call at nothing.
    """
    input_counts = entry.get("input") or {}
    output_counts = entry.get("output") or {}
    return OUTPUT_TOKEN_FIELD in output_counts or any(
        name in input_counts
        for name in (INPUT_TOKEN_FIELD, CACHE_READ_FIELD, CACHE_WRITE_FIELD)
    )
