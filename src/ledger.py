"""Dedup, accumulate, and the once-per-X claims. Owns four item types.

Every write is conditional or an atomic ADD, because up to ten invocations run
concurrently. A failed conditional check is the answer, not an error -- it is
the only exception absorbed here. Money is micro-USD.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from errors import (
    LedgerWriteFailed,
    is_conditional_check_failure,
    is_validation_error,
)

log = logging.getLogger(__name__)

__all__ = [
    "utc_day",
    "claim_count",
    "accumulate",
    "current_total",
    "claim_threshold",
    "claim_notice",
    "claim_disable",
    "release_disable",
    "is_disabled",
    "read_disabled",
    "clear_disabled",
    "day_key",
]

_ROW_TTL_SECONDS = 60 * 24 * 60 * 60  # 60 days of history, then self-delete
_DEDUP_TTL_SECONDS = 48 * 60 * 60


def utc_day(ts: float | None = None) -> str:
    """The budget day, always UTC. In the sort key, which is the daily reset."""
    # UTC midnight is 05:30 IST, outside the working day, so the budget never
    # refills mid-afternoon. ts=0 is honoured; only None means "now".
    moment = (
        datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)
    )
    return moment.strftime("%Y-%m-%d")


def day_key(track: str, day: str) -> dict:
    return {"pk": f"TRACK#{track}", "sk": f"DAY#{day}"}


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def claim_count(table, request_id: str, now: float | None = None) -> bool:
    """True if this caller should add this invocation to the ledger."""
    # It does not read to check: that leaves a window where two invocations both
    # see nothing and both count. The write and the decision are one action.
    # Guards the money only -- a loser must still react, see handler.process.
    now = time.time() if now is None else now
    try:
        table.put_item(
            Item={
                "pk": f"REQ#{request_id}",
                "sk": "SEEN",
                "ttl": int(now) + _DEDUP_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return True
    except Exception as exc:
        if is_conditional_check_failure(exc):
            return False
        raise LedgerWriteFailed(f"dedup write failed for request {request_id}: {exc!r}") from exc


# ---------------------------------------------------------------------------
# The meter
# ---------------------------------------------------------------------------

_ACCUMULATE_EXPRESSION = (
    "ADD spent_micros :m, in_tokens :i, out_tokens :o, "
    "cache_read_tokens :cr, cache_write_tokens :cw, calls :one "
    "SET #day = :day, #ttl = :ttl, "
    "by_model.#model = if_not_exists(by_model.#model, :zero) + :m, "
    "by_principal.#princ = if_not_exists(by_principal.#princ, :zero) + :m"
)


def _ensure_maps(table, key: dict) -> None:
    """Create the day row with empty `by_model` / `by_principal` maps."""
    # DynamoDB rejects a nested path whose parent map is absent, and an
    # UpdateExpression is atomic, so the item is not created either -- the first
    # write of every day failed, so every write failed. `if_not_exists` creates
    # the item and leaves an existing map alone, so it is concurrency-safe.
    try:
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET by_model = if_not_exists(by_model, :empty), "
                "by_principal = if_not_exists(by_principal, :empty)"
            ),
            ExpressionAttributeValues={":empty": {}},
        )
    except Exception as exc:
        raise LedgerWriteFailed(f"could not initialise day row {key}: {exc!r}") from exc


def accumulate(
    table,
    *,
    track: str,
    day: str,
    model_id: str,
    principal_name: str,
    usage,
    micros: int,
    now: float | None = None,
) -> int:
    """Add one invocation. Returns the day's new total micros."""
    # ReturnValues so the total comes back in the round trip that records the
    # spend. The model id goes through ExpressionAttributeNames because it
    # contains dots, which an expression would read as a nested path.
    now = time.time() if now is None else now
    key = day_key(track, day)
    args = dict(
        Key=key,
        UpdateExpression=_ACCUMULATE_EXPRESSION,
        ExpressionAttributeNames={
            "#day": "day",
            "#ttl": "ttl",
            "#model": model_id,
            "#princ": principal_name,
        },
        ExpressionAttributeValues={
            ":m": micros,
            ":i": usage.input_tokens,
            ":o": usage.output_tokens,
            ":cr": usage.cache_read_tokens,
            ":cw": usage.cache_write_tokens,
            ":one": 1,
            ":zero": 0,
            ":day": day,
            ":ttl": int(now) + _ROW_TTL_SECONDS,
        },
        ReturnValues="UPDATED_NEW",
    )

    try:
        resp = table.update_item(**args)
    except Exception as exc:
        if not is_validation_error(exc):
            raise LedgerWriteFailed(f"accumulate failed for {key}: {exc!r}") from exc
        # First write of the day: the parent maps do not exist yet.
        _ensure_maps(table, key)
        try:
            resp = table.update_item(**args)
        except Exception as retry_exc:
            raise LedgerWriteFailed(
                f"accumulate failed for {key} even after initialising the day row: "
                f"{retry_exc!r}"
            ) from retry_exc

    attributes = resp.get("Attributes") or {}
    if "spent_micros" not in attributes:
        # UPDATED_NEW always returns an ADDed attribute. Guessing instead would
        # hand enforcement a number that is too small.
        raise LedgerWriteFailed(
            f"accumulate for {key} returned no spent_micros; cannot evaluate the budget"
        )
    return int(attributes["spent_micros"])


def current_total(table, track: str, day: str) -> int:
    """Today's spend, without writing. For the duplicate path."""
    key = day_key(track, day)
    try:
        item = table.get_item(Key=key).get("Item")
    except Exception as exc:
        raise LedgerWriteFailed(f"could not read the day row {key}: {exc!r}") from exc
    if not item:
        return 0
    return int(item.get("spent_micros", 0))


# ---------------------------------------------------------------------------
# Claims: the "exactly one of you does this" primitives
# ---------------------------------------------------------------------------


def claim_threshold(table, track: str, day: str, pct: int) -> bool:
    """True if this caller crossed `pct` today. Exactly one winner per day."""
    try:
        table.update_item(
            Key=day_key(track, day),
            UpdateExpression="ADD alerted :p",
            ConditionExpression="attribute_not_exists(alerted) OR NOT contains(alerted, :pv)",
            ExpressionAttributeValues={":p": {pct}, ":pv": pct},
        )
        return True
    except Exception as exc:
        if is_conditional_check_failure(exc):
            return False
        raise LedgerWriteFailed(
            f"threshold claim failed for {track} at {pct}%: {exc!r}"
        ) from exc


def claim_notice(table, track: str, day: str, kind: str, now: float | None = None) -> bool:
    """True if this caller should send the `kind` notice today."""
    now = time.time() if now is None else now
    try:
        table.put_item(
            Item={
                "pk": f"TRACK#{track}",
                "sk": f"DAY#{day}#NOTICE#{kind}",
                "ttl": int(now) + _ROW_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return True
    except Exception as exc:
        if is_conditional_check_failure(exc):
            return False
        raise LedgerWriteFailed(
            f"notice claim failed for {track} {kind}: {exc!r}"
        ) from exc


def claim_disable(
    table,
    *,
    track: str,
    day: str,
    principal_keys: dict[str, set[str]],
    spent_micros: int,
    now: float | None = None,
) -> bool:
    """True if this caller should deactivate the keys. Exactly one winner."""
    now = time.time() if now is None else now
    # Pre-paired: two independent sets lose which key belongs to which user, and
    # the cross product calls UpdateAccessKey with mismatched owners.
    pairs = {f"{user}/{key_id}" for user, keys in principal_keys.items() for key_id in keys}
    item = {
        "pk": f"TRACK#{track}",
        "sk": "DISABLED",
        "disabled_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(
            timespec="seconds"
        ),
        "spend_at_disable": spent_micros,
        # `disabled_day`, not `day`: this never expires, so under `day` it would
        # sit in the sparse GSI1 forever. And no ttl -- expiry would leave a dead
        # key with nothing to explain it.
        "disabled_day": day,
    }
    if principal_keys:
        item["principals"] = set(principal_keys)
    if pairs:
        item["keys"] = pairs
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return True
    except Exception as exc:
        if is_conditional_check_failure(exc):
            return False
        raise LedgerWriteFailed(f"disable claim failed for {track}: {exc!r}") from exc


def release_disable(table, track: str) -> None:
    """Undo a claim whose deactivation did nothing."""
    # Keeping it is the worst state reachable: the ledger says stopped, the key
    # still works, and the claim is one-shot so nothing retries.
    clear_disabled(table, track)


def is_disabled(table, track: str) -> bool:
    try:
        return bool(table.get_item(Key={"pk": f"TRACK#{track}", "sk": "DISABLED"}).get("Item"))
    except Exception as exc:
        raise LedgerWriteFailed(f"disabled check failed for {track}: {exc!r}") from exc


def read_disabled(table, track: str) -> dict | None:
    try:
        return table.get_item(Key={"pk": f"TRACK#{track}", "sk": "DISABLED"}).get("Item")
    except Exception as exc:
        raise LedgerWriteFailed(f"disabled read failed for {track}: {exc!r}") from exc


def clear_disabled(table, track: str) -> None:
    """Delete the disabled record. Deliberately not wrapped."""
    table.delete_item(Key={"pk": f"TRACK#{track}", "sk": "DISABLED"})
