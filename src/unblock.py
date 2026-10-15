"""Re-enable a track disabled by the runaway ceiling. Human-invoked only.

The rota is granted `lambda:InvokeFunction` on this, not `iam:UpdateAccessKey`.
Refusals raise; returning {"ok": false} would exit 0 and read as success.
Invoke it with `make unblock TRACK=<name> REASON='...'` -- see README.md.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

import budgets
import enforce
import ledger
from errors import MalformedLogEntry, NotDisabled, ReactivationFailed

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

USAGE_TABLE = os.environ.get("USAGE_TABLE", "bedrock_usage")
BUDGET_TABLE = os.environ.get("BUDGET_TABLE", "bedrock_budgets")
# Fallback only; the budget row's own topic wins. See `_topic_for_track`.
NOTIFY_TOPIC_ARN = os.environ.get("NOTIFY_TOPIC_ARN")
MIN_REASON_CHARS = 10

_clients: dict = {}


def clients():
    if not _clients:
        import boto3
        from botocore.config import Config

        config = Config(retries={"max_attempts": 5, "mode": "adaptive"})
        dynamodb = boto3.resource("dynamodb", config=config)
        _clients.update(
            usage=dynamodb.Table(USAGE_TABLE),
            # Read-only, and only to find out where this track's alerts go.
            budgets=dynamodb.Table(BUDGET_TABLE),
            iam=boto3.client("iam", config=config),
            sns=boto3.client("sns", config=config),
        )
    return _clients


def _topic_for_track(aws_clients: dict, track: str) -> str | None:
    """The track's own topic, else the default. Resolved before any mutation."""
    return budgets.load(aws_clients["budgets"], track).notify_topic_arn or NOTIFY_TOPIC_ARN


def _pairs_from(item: dict) -> list[tuple[str, str]]:
    """The (user, key id) pairs recorded at disable time."""
    key_pairs = []
    for record in sorted(item.get("keys") or []):
        user, _, key_id = str(record).partition("/")
        if not user or not key_id:
            raise ReactivationFailed(f"unusable key record {record!r} on the disabled item")
        key_pairs.append((user, key_id))
    return key_pairs


def handler(event, context):
    track = str((event or {}).get("track", "")).strip()
    reason = str((event or {}).get("reason", "")).strip()

    if not track:
        raise MalformedLogEntry(
            "track is required. Payload shape: "
            '{"track": "<name>", "reason": "<what you found>"}'
        )
    # So re-enabling follows a diagnosis rather than becoming a switch people
    # flip without reading. Enforced here, not in the runbook.
    if len(reason) < MIN_REASON_CHARS:
        raise MalformedLogEntry(
            f"reason is required and must say what was found (at least "
            f"{MIN_REASON_CHARS} characters, got {len(reason)}). Identify what "
            f"spent the money, confirm it has stopped, then re-enable."
        )

    aws_clients = clients()
    item = ledger.read_disabled(aws_clients["usage"], track)
    if not item:
        raise NotDisabled(
            f"{track} is not disabled, so there is nothing to re-enable. "
            f"If its key is inactive, it was not this control that deactivated it."
        )

    # Before the first IAM call, so a budget problem refuses rather than
    # half-performs the release.
    topic_arn = _topic_for_track(aws_clients, track)

    key_pairs = _pairs_from(item)
    reactivated, failures = [], {}
    for user_name, key_id in key_pairs:
        try:
            aws_clients["iam"].update_access_key(
                UserName=user_name, AccessKeyId=key_id, Status="Active"
            )
            reactivated.append(key_id)
        except Exception as exc:  # noqa: BLE001 - every key is attempted, then reported
            logger.error("reactivate failed %s/%s", user_name, key_id, exc_info=True)
            failures[key_id] = repr(exc)

    if failures:
        # The record stays, so the next attempt finds it. Clearing it would leave
        # a free disable claim on a key that is already inactive.
        raise ReactivationFailed(
            f"{len(failures)} of {len(key_pairs)} access keys for {track} are still inactive: "
            f"{failures}. The disabled record has been left in place; fix the cause and "
            f"invoke this again.",
            reactivated=sorted(reactivated),
            still_inactive=sorted(failures),
        )

    # Not wrapped: a failed delete must not be reported as a release, or the
    # track sits listed as disabled with live keys and the next runaway no-ops.
    ledger.clear_disabled(aws_clients["usage"], track)

    caller = _caller(context)
    enforce.notify(
        aws_clients["sns"],
        topic_arn,
        f"[Bedrock] RE-ENABLED {track}",
        {
            "track": track,
            "reactivated_access_keys": sorted(reactivated),
            "spend_at_disable_usd": _format_usd(item.get("spend_at_disable", 0)),
            "disabled_at": item.get("disabled_at"),
            "disabled_day": item.get("disabled_day"),
            "reason": reason,
            "by": caller,
        },
    )
    logger.warning("RE-ENABLED %s by %s: %s", track, caller, reason)
    return {
        "ok": True,
        "track": track,
        "reactivated": sorted(reactivated),
        "by": caller,
    }


def _caller(context) -> str:
    """Who invoked this, for the audit trail."""
    lambda_identity = getattr(context, "identity", None)
    cognito = getattr(lambda_identity, "cognito_identity_id", None)
    request_id = getattr(context, "aws_request_id", None) or "unknown"
    return cognito or f"see CloudTrail for lambda request {request_id}"


def _format_usd(micros) -> str:
    return f"{Decimal(str(micros)) / Decimal(1_000_000):.4f}"
