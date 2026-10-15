"""One row holds every model's prices, under RATECARD#CURRENT in the budget table.

Exactly two rows exist at a time: CURRENT, and the card it replaced under
RATECARD#<the day that one was fetched>. Each refresh rotates them, so history
is one step deep and nothing accumulates.

There is no baseline card. A model absent from CURRENT prices at
pricing.FALLBACK, which is expensive and alerts daily, so a missing price is
loud rather than free. Validation lives here and is pure.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

import pricing
from errors import RateCardRejected, RateCardUnavailable

logger = logging.getLogger(__name__)

__all__ = [
    "RateCard",
    "CURRENT_SCOPE",
    "PREVIOUS_SCOPE_ATTR",
    "TOKEN_CLASSES",
    "STALE_AFTER_DAYS",
    "MAX_DELTA_PCT",
    "snapshot_scope",
    "day_of",
    "load",
    "read_current",
    "put_card",
    "delete_card",
    "rates_from_item",
    "item_from_rates",
    "merge_over_current",
    "validate",
    "reset_cache",
]

CURRENT_SCOPE = "RATECARD#CURRENT"
# Written on CURRENT so the next refresh knows which dated row to delete. Without
# it the dated rows would accumulate one a week forever.
PREVIOUS_SCOPE_ATTR = "previous_scope"
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_write")

# One read per container per quarter hour. Prices change weekly, so anything
# shorter buys nothing.
_MEMO_TTL_SECONDS = 15 * 60

# A card older than this means the weekly refresh stopped landing a month ago.
# Alerted daily by handler.apply_budget_policy.
STALE_AFTER_DAYS = 30

# A rate quoted per 1K tokens read as per million is a 1000x error, and that is
# the shape this catches. Overridable per deploy: see template.yaml.
MAX_DELTA_PCT = Decimal("25")


class RateCard(NamedTuple):
    rates: dict[str, pricing.Rates]
    fetched_at: str
    is_stale: bool

    def rate_for(self, model_id: str) -> pricing.Rates | None:
        """None means this card has no price for the model, so FALLBACK applies."""
        return self.rates.get(pricing.normalize_model_id(model_id))


_memo_cache: tuple[float, "RateCard | None"] | None = None



# {
#   "scope": "RATECARD#CURRENT",
#   "fetched_at": "2026-10-14T03:00:11+00:00",
#   "source": "AmazonBedrock:us-east-1",
#   "region": "us-east-1",
#   "previous_scope": "RATECARD#2026-10-07",
#   "models": {
#     "anthropic.claude-haiku-4-5-20251001-v1:0":  { "input": 1, "output": 5,  "cache_read": 0.1, "cache_write": 1.25 },
#     "anthropic.claude-sonnet-4-5-20250929-v1:0": { "input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75 }
#   }
# }



def snapshot_scope(day: str) -> str:
    """The dated row holding the card CURRENT replaced."""
    return f"RATECARD#{day}"


def reset_cache() -> None:
    """Drop the in-process card. For tests, and for a forced re-read."""
    global _memo_cache
    _memo_cache = None


def read_current(table) -> dict | None:
    """The stored CURRENT item. None means no row. Raises if the read failed."""
    try:
        return table.get_item(Key={"scope": CURRENT_SCOPE}).get("Item")
    except Exception as exc:
        raise RateCardUnavailable(f"rate card read failed: {exc!r}") from exc


def put_card(table, scope: str, item: dict) -> None:
    """Write a card row. Not wrapped: a failed write must not read as success."""
    table.put_item(Item={**item, "scope": scope})


def delete_card(table, scope: str) -> None:
    """Drop a dated row. Not wrapped, for the same reason as put_card."""
    table.delete_item(Key={"scope": scope})


def rates_from_item(item: dict) -> dict[str, pricing.Rates]:
    """The `models` map of a stored card. Raises on a shape it cannot trust."""
    models = item.get("models")
    if not isinstance(models, dict) or not models:
        raise RateCardUnavailable(f"rate card carries no models map: {item.get('scope')!r}")
    rates: dict[str, pricing.Rates] = {}
    for model_id, row in models.items():
        if not isinstance(row, dict):
            raise RateCardUnavailable(f"rate card entry for {model_id!r} is not a map")
        missing = [name for name in TOKEN_CLASSES if row.get(name) is None]
        if missing:
            # A missing class would price those tokens at zero, which is an
            # unlimited budget for whichever flow uses them.
            raise RateCardUnavailable(f"rate card entry for {model_id!r} is missing {missing}")
        try:
            rates[model_id] = pricing.Rates(
                **{name: Decimal(str(row[name])) for name in TOKEN_CLASSES}
            )
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise RateCardUnavailable(
                f"rate card entry for {model_id!r} is not numeric: {exc!r}"
            ) from exc
    return rates


def item_from_rates(
    rates: dict[str, pricing.Rates],
    *,
    fetched_at: str,
    source: str,
    region: str,
) -> dict:
    """The DynamoDB shape of a card. Decimal, because floats lose cents."""
    return {
        "fetched_at": fetched_at,
        "source": source,
        "region": region,
        "models": {
            model_id: {name: getattr(row, name) for name in TOKEN_CLASSES}
            for model_id, row in sorted(rates.items())
        },
    }


def _age_days(fetched_at: str | None, now: float) -> float | None:
    if not fetched_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp.timestamp()) / 86400.0


def day_of(fetched_at: str | None) -> str | None:
    """The UTC date part of a stored timestamp, for naming the dated row."""
    if not fetched_at:
        return None
    text = str(fetched_at)
    return text.split("T", 1)[0] or None


def load(table, now: float | None = None) -> RateCard | None:
    """The card to price with. None means no usable card, so every model is FALLBACK."""
    global _memo_cache
    now = time.time() if now is None else now

    if _memo_cache and _memo_cache[0] > now:
        return _memo_cache[1]

    # Deliberately before the memo write: a transient read failure raises and is
    # retried rather than being remembered, because pricing a whole batch at the
    # fallback rate would bake wrong numbers into the ledger.
    item = read_current(table)

    card: RateCard | None = None
    if not item:
        logger.warning("no %s row; every model prices at the fallback rate", CURRENT_SCOPE)
    else:
        try:
            rates = rates_from_item(item)
        except RateCardUnavailable as exc:
            # A bad row will not fix itself on retry, so this does not raise. It
            # falls to FALLBACK, which is expensive and alerts daily.
            logger.error("unusable rate card: %r", exc)
        else:
            age = _age_days(item.get("fetched_at"), now)
            card = RateCard(
                rates=rates,
                fetched_at=str(item.get("fetched_at") or ""),
                is_stale=age is None or age > STALE_AFTER_DAYS,
            )

    _memo_cache = (now + _MEMO_TTL_SECONDS, card)
    return card


def merge_over_current(
    fetched: dict[str, pricing.Rates],
    current: dict[str, pricing.Rates],
) -> dict[str, pricing.Rates]:
    """Fetched wins; a model the price list dropped keeps its last known rate."""
    # A merge, not a replacement, for the same reason budgets.load merges: a
    # model that vanishes from the price list would otherwise have no row and
    # start pricing at FALLBACK for a reason that has nothing to do with it.
    merged = dict(current)
    merged.update(fetched)
    return merged


def validate(
    fetched: dict[str, pricing.Rates],
    current: dict[str, pricing.Rates],
    *,
    max_delta_pct: Decimal = MAX_DELTA_PCT,
) -> None:
    """Raises RateCardRejected listing every reason. Pure, and runs before any write."""
    reasons: list[str] = []

    if not fetched:
        raise RateCardRejected("the fetched rate card is empty", reasons=["no models parsed"])

    for model_id, row in sorted(fetched.items()):
        for name in TOKEN_CLASSES:
            rate = getattr(row, name)
            if rate is None or rate <= 0:
                reasons.append(f"{model_id}.{name} is not above zero: {rate!r}")
        # Cache reads bill at about a tenth of input. A card where they cost
        # more is a token-class mix-up, which mis-meters every cached workload.
        if row.cache_read is not None and row.input is not None and row.cache_read > row.input:
            reasons.append(
                f"{model_id}.cache_read ({row.cache_read}) is above input ({row.input})"
            )

    for model_id, row in sorted(fetched.items()):
        before = current.get(model_id)
        if before is None:
            continue  # a new model has nothing to drift from
        for name in TOKEN_CLASSES:
            old, new = getattr(before, name), getattr(row, name)
            if old is None or new is None or old <= 0:
                continue
            delta = abs(new - old) * 100 / old
            if delta > max_delta_pct:
                reasons.append(
                    f"{model_id}.{name} moved {delta:.1f}% ({old} -> {new}), "
                    f"over the {max_delta_pct}% limit"
                )

    if reasons:
        raise RateCardRejected(
            f"fetched rate card rejected on {len(reasons)} check(s); "
            f"nothing was written and the current card is unchanged",
            reasons=reasons,
        )
