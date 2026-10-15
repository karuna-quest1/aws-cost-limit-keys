"""Weekly: fetch Bedrock prices, validate the whole card, then rotate the rows.

Nothing is written until every check passes, so a bad fetch leaves DynamoDB
exactly as it was and the meter keeps pricing with the card it already has.
On success the old CURRENT becomes the dated row and the row before that is
deleted, so there are never more than two. See README.md.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import enforce
import ledger
import pricelist
import ratecard
from errors import RateCardRejected

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BUDGET_TABLE = os.environ.get("BUDGET_TABLE", "bedrock_budgets")
USAGE_TABLE = os.environ.get("USAGE_TABLE", "bedrock_usage")
NOTIFY_TOPIC_ARN = os.environ.get("NOTIFY_TOPIC_ARN")
# The Price List API lives in a handful of regions; the prices it returns are
# selected by RATE_REGION, which is the region the meter charges at.
PRICE_LIST_REGION = os.environ.get("PRICE_LIST_REGION", "us-east-1")
RATE_REGION = os.environ.get("RATE_REGION", "us-east-1")
MAX_DELTA_PCT = Decimal(os.environ.get("MAX_RATE_DELTA_PCT", "25"))

_clients: dict = {}


def clients():
    if not _clients:
        import boto3
        from botocore.config import Config

        config = Config(retries={"max_attempts": 5, "mode": "adaptive"})
        dynamodb = boto3.resource("dynamodb", config=config)
        _clients.update(
            budgets=dynamodb.Table(BUDGET_TABLE),
            usage=dynamodb.Table(USAGE_TABLE),
            # Region-pinned: the Price List API is not offered in every region.
            pricing=boto3.client("pricing", region_name=PRICE_LIST_REGION, config=config),
            sns=boto3.client("sns", config=config),
        )
    return _clients


def _stored_rates(item: dict | None) -> dict:
    """The current card's rates, or empty. A corrupt row must not block its own repair."""
    if not item:
        return {}
    try:
        return ratecard.rates_from_item(item)
    except Exception as exc:  # noqa: BLE001 - a refresh is exactly what repairs this
        logger.warning("ignoring unusable stored rate card: %r", exc)
        return {}


def handler(event, context):  # noqa: ARG001 - signature is fixed
    aws_clients = clients()
    table = aws_clients["budgets"]
    day = ledger.utc_day()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    current_item = ratecard.read_current(table)
    current_rates = _stored_rates(current_item)

    result = pricelist.fetch(aws_clients["pricing"], RATE_REGION)
    merged = ratecard.merge_over_current(result.rates, current_rates)

    # Everything above is a read. Nothing below the gate runs unless it passes.
    try:
        ratecard.validate(result.rates, current_rates, max_delta_pct=MAX_DELTA_PCT)
    except RateCardRejected as exc:
        _announce_rejected(aws_clients, day, exc, result, current_rates)
        raise

    rotated_to = _rotate(table, current_item, now)
    ratecard.put_card(
        table,
        ratecard.CURRENT_SCOPE,
        {
            **ratecard.item_from_rates(
                merged,
                fetched_at=now,
                source=f"{pricelist.SERVICE_CODE}:{RATE_REGION}",
                region=RATE_REGION,
            ),
            ratecard.PREVIOUS_SCOPE_ATTR: rotated_to,
        },
    )
    logger.warning(
        "rate card promoted: %d models from %d SKUs (%s); previous kept at %s",
        len(merged), result.skus_read, RATE_REGION, rotated_to,
    )

    if result.unmapped_names or result.defaulted:
        _announce_gaps(aws_clients, day, result)

    return {
        "promoted": True,
        "models": len(merged),
        "previous_scope": rotated_to,
        "skus_read": result.skus_read,
        "unmapped": list(result.unmapped_names),
        "defaulted": list(result.defaulted),
        "fetched_at": now,
    }


def _rotate(table, current_item: dict | None, now: str) -> str | None:
    """Keep the outgoing card under its own date, and drop the one before it.

    Returns the dated scope now holding the previous card, or None on a first run.
    """
    if not current_item:
        return None

    outgoing_day = ratecard.day_of(current_item.get("fetched_at")) or ratecard.day_of(now)
    dated_scope = ratecard.snapshot_scope(outgoing_day)

    # Delete first, so that even if the write below fails there are never three
    # rows. The deleted one is two cards old and nothing reads it.
    older = current_item.get(ratecard.PREVIOUS_SCOPE_ATTR)
    if older and older != dated_scope:
        ratecard.delete_card(table, older)

    body = {key: value for key, value in current_item.items()
            if key not in ("scope", ratecard.PREVIOUS_SCOPE_ATTR)}
    ratecard.put_card(table, dated_scope, body)
    return dated_scope


def _announce_rejected(aws_clients: dict, day: str, exc, result, current_rates) -> None:
    """Once a day, so the automatic retries do not re-page."""
    if not ledger.claim_notice(aws_clients["usage"], "_SYSTEM", day, "RATECARD_REJECTED"):
        return
    enforce.notify(
        aws_clients["sns"],
        NOTIFY_TOPIC_ARN,
        "[Bedrock] rate card REJECTED - prices not updated",
        {
            "day": day,
            "reasons": list(exc.reasons),
            "stored": _as_usd(current_rates),
            "fetched": _as_usd(result.rates),
            "unmapped_price_list_names": list(result.unmapped_names),
            "action": (
                "Nothing was written: the meter is still pricing with the card it "
                "had. Compare `stored` against `fetched` above. If the new prices "
                "are real, raise MAX_RATE_DELTA_PCT for one run; if they are not, "
                "the parser in src/pricelist.py needs the fix."
            ),
        },
    )


def _as_usd(rates: dict) -> dict:
    return {
        model_id: {name: str(getattr(row, name)) for name in ratecard.TOKEN_CLASSES}
        for model_id, row in sorted(rates.items())
    }


def _announce_gaps(aws_clients: dict, day: str, result) -> None:
    """Once a day, so a retry does not re-page."""
    if not ledger.claim_notice(aws_clients["usage"], "_SYSTEM", day, "RATECARD_GAPS"):
        return
    enforce.notify(
        aws_clients["sns"],
        NOTIFY_TOPIC_ARN,
        "[Bedrock] rate card refreshed with gaps",
        {
            "day": day,
            "models_priced": len(result.rates),
            "unmapped_price_list_names": list(result.unmapped_names),
            "cache_rates_defaulted_to_input": list(result.defaulted),
            "action": (
                "An unmapped name means that model is not on the card, so it "
                "prices at pricing.FALLBACK -- expensive, and it alerts daily. "
                "Add it to pricelist.MODEL_IDS to pick up its real rates."
            ),
        },
    )
