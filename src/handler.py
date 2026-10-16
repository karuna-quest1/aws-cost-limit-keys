"""Lambda entry point. One event carries a batch of log lines.

Per line, catch -- one bad line must not cost the others their retries.
Per batch, raise -- otherwise the invocation reports success while spend went
uncounted. Async invocation gives two retries then the DLQ.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os
from decimal import Decimal

import budgets
import enforce
import errors
import identity
import ledger
import pricing
import ratecard

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

USAGE_TABLE = os.environ.get("USAGE_TABLE", "bedrock_usage")
BUDGET_TABLE = os.environ.get("BUDGET_TABLE", "bedrock_budgets")
# Fallback when a budget row names no topic. A row that does not exist cannot
# name one, so without this the "no budget row" alert has nowhere to go.
NOTIFY_TOPIC_ARN = os.environ.get("NOTIFY_TOPIC_ARN") or None

_clients: dict = {}


def clients():
    """Lazy and cached across warm invocations."""
    # The import is inside the function so the pure modules stay importable,
    # and testable, with no boto3 and no credentials.
    if not _clients:
        import boto3
        from botocore.config import Config

        config = Config(retries={"max_attempts": 5, "mode": "adaptive"})
        dynamodb = boto3.resource("dynamodb", config=config)
        _clients.update(
            usage=dynamodb.Table(USAGE_TABLE),
            budgets=dynamodb.Table(BUDGET_TABLE),
            iam=boto3.client("iam", config=config),
            sns=boto3.client("sns", config=config),
        )
    return _clients


def unpack(event: dict) -> list[str]:
    """base64(gzip(json)) -> the raw `message` strings, unparsed."""
    encoded_payload = event.get("awslogs", {}).get("data")
    if not encoded_payload:
        return []
    payload = json.loads(gzip.decompress(base64.b64decode(encoded_payload)))
    if payload.get("messageType") == "CONTROL_MESSAGE":
        return []  # subscription-filter health check, carries no invocations
    return [
        log_event["message"]
        for log_event in payload.get("logEvents", [])
        if "message" in log_event
    ]


def handler(event, context):  # noqa: ARG001 - context unused, signature is fixed
    stats = {"seen": 0, "counted": 0, "duplicate": 0, "failed": 0}
    failures: list[tuple[str, Exception]] = []

    aws_clients = clients()
    for message in unpack(event):
        stats["seen"] += 1
        try:
            stats[process_log_entry(message, aws_clients)] += 1
        except Exception as exc:  # noqa: BLE001 - re-raised below, after the loop
            stats["failed"] += 1
            failures.append((_request_id_of(message), exc))
            logger.exception("failed on %s", _request_id_of(message))

    logger.info("batch %s", stats)
    if failures:
        raise errors.BatchIncomplete(stats, failures)
    return stats


def _request_id_of(message: str) -> str:
    """Best effort, for the log line only. Never raises."""
    try:
        return str(json.loads(message).get("requestId", "<no requestId>"))
    except Exception:  # noqa: BLE001 - this is the diagnostic path
        return "<unparseable>"


def process_log_entry(message: str, aws_clients: dict) -> str:
    """One invocation log line. Returns the stats key it belongs to."""
    try:
        entry = json.loads(message)
    except ValueError as exc:
        raise errors.MalformedLogEntry(f"log event message is not JSON: {exc}") from exc
    if not isinstance(entry, dict):
        raise errors.MalformedLogEntry(
            f"log event message is not an object: {type(entry).__name__}"
        )

    request_id = (entry.get("requestId") or "").strip()
    if not request_id:
        # No dedup key, so it cannot be counted exactly once.
        raise errors.MalformedLogEntry("log entry has no requestId, so it cannot be deduplicated")

    model_id = (entry.get("modelId") or "").strip()
    if not model_id:
        raise errors.MalformedLogEntry(f"log entry {request_id} has no modelId")

    arn = (entry.get("identity") or {}).get("arn", "")
    attribution = identity.resolve(arn, aws_clients["iam"], aws_clients["usage"])
    usage = pricing.usage_from_log(entry)
    # Memoised for 15 minutes, so this is one extra read per container, not per
    # line. No card, or no entry on it, means this model prices at FALLBACK.
    card = ratecard.load(aws_clients["budgets"])
    priced_usage = pricing.price(usage, model_id, card.rate_for(model_id) if card else None)
    day = ledger.utc_day()

    if ledger.claim_count(aws_clients["usage"], request_id):
        try:
            total = ledger.accumulate(
                aws_clients["usage"],
                track=attribution.track,
                day=day,
                model_id=model_id,
                principal_name=attribution.principal.name,
                principal_kind=attribution.principal.kind,
                usage=usage,
                micros=priced_usage.micros,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised below, after the release
            # The claim is a lie if the spend was not recorded, and it is
            # one-shot, so the retry would treat this line as a duplicate and
            # add nothing. Release it unless the write may actually have landed,
            # where releasing would count the same call twice instead.
            if not getattr(exc, "may_have_written", False):
                ledger.release_count(aws_clients["usage"], request_id)
            raise
        outcome = "counted"
    else:
        # Already counted, by an earlier delivery or a concurrent invocation.
        # The money must not be added twice, but whoever counted it may have died
        # before alerting or disabling -- and every claim below is idempotent.
        total = ledger.current_total(aws_clients["usage"], attribution.track, day)
        outcome = "duplicate"

    apply_budget_policy(
        attribution,
        day,
        total,
        aws_clients,
        model_id=model_id,
        fallback=priced_usage.is_fallback,
        stale_rates=bool(card and card.is_stale),
        rates_fetched_at=card.fetched_at if card else None,
    )
    return outcome


def apply_budget_policy(
    attribution,
    day: str,
    total: int,
    aws_clients: dict,
    *,
    model_id: str,
    fallback: bool,
    stale_rates: bool = False,
    rates_fetched_at: str | None = None,
) -> None:
    budget = budgets.load(aws_clients["budgets"], attribution.track)
    topic_arn = budget.notify_topic_arn or NOTIFY_TOPIC_ARN
    decision = enforce.evaluate(total, budget)

    if stale_rates:
        # Not per track: the card is one object, so this claims under _SYSTEM and
        # pages once a day however many tracks are spending.
        _send_notice_once(
            aws_clients,
            topic_arn,
            track="_SYSTEM",
            day=day,
            kind="RATECARD_STALE",
            subject="[Bedrock] rate card has not refreshed in weeks",
            body={
                "fetched_at": rates_fetched_at,
                "stale_after_days": ratecard.STALE_AFTER_DAYS,
                "action": (
                    "The weekly refresh has stopped landing, so every budget is "
                    "being evaluated against prices that may have moved. Check the "
                    "bedrock-cost-limits-refresh-rates function and its DLQ."
                ),
            },
        )

    if budget.is_floor:
        _send_notice_once(
            aws_clients,
            topic_arn,
            track=attribution.track,
            day=day,
            kind="NO_BUDGET",
            subject=f"[Bedrock] {attribution.track} has no budget row",
            body={
                "track": attribution.track,
                "applied_daily_usd": _format_usd(budget.daily_micros),
                "action": (
                    "A conservative floor is being applied so the track is not "
                    "unlimited. Seed a TRACK# row, or DEFAULT, to set a real budget."
                ),
            },
        )

    if fallback:
        _send_notice_once(
            aws_clients,
            topic_arn,
            track=attribution.track,
            day=day,
            kind=f"FALLBACK#{model_id}",
            subject=f"[Bedrock] unpriced model used by {attribution.track}",
            body={
                "track": attribution.track,
                "model_id": model_id,
                "action": (
                    "No RATECARD# row for this model, so it is charged at the "
                    "deliberately high fallback rate. Either the weekly refresh has "
                    "not run, or the model needs a line in pricelist.MODEL_IDS."
                ),
            },
        )

    for threshold_pct in decision.crossed_pcts:
        if not ledger.claim_threshold(
            aws_clients["usage"], attribution.track, day, threshold_pct
        ):
            continue  # another invocation already sent this one today
        enforce.notify(
            aws_clients["sns"],
            topic_arn,
            f"[Bedrock] {attribution.track} at {threshold_pct}% of daily budget",
            {
                "track": attribution.track,
                "day": day,
                "threshold_pct": threshold_pct,
                # The gap between the threshold and the real figure is the
                # signal: 81% is a nudge, 140% means the disable is next.
                "actual_pct": f"{decision.pct_of_budget:.1f}",
                "spent_usd": _format_usd(total),
                "daily_budget_usd": _format_usd(budget.daily_micros),
                "disable_at_usd": _format_usd(budget.runaway_micros),
                "budget_scope": budget.scope,
                "enforce": budget.enforce,
                "note": "Bedrock access is deactivated above the disable threshold.",
            },
        )

    if not decision.is_runaway:
        return
    if not budget.enforce:
        # The watch-only week in one line: what would have happened, with the
        # real number, without doing it.
        logger.warning(
            "%s past the disable ceiling at %s USD (ceiling %s), but enforce=false",
            attribution.track,
            _format_usd(total),
            _format_usd(budget.runaway_micros),
        )
        return
    disable_track(attribution, day, total, budget, topic_arn, aws_clients)


def disable_track(
    attribution,
    day: str,
    total: int,
    budget,
    topic_arn: str | None,
    aws_clients: dict,
) -> None:
    principal = attribution.principal
    # Every IAM user on the track, not just the one whose line crossed the
    # ceiling. A track may have several keys, and switching off one of them
    # leaves it spending through the rest while the ledger says it is stopped.
    # The current principal is unioned in because the day row may not list it
    # yet -- this line may be a duplicate whose spend was counted elsewhere.
    user_names = ledger.read_user_principals(aws_clients["usage"], attribution.track, day)
    if principal.kind == "user":
        user_names.add(principal.name)

    # Raises on failure: an empty set would be indistinguishable from "no keys".
    principal_keys = {}
    for user_name in sorted(user_names):
        active = enforce.access_keys_for_user(aws_clients["iam"], user_name)
        if active:
            principal_keys[user_name] = active

    if not principal_keys:
        # A role, or keys already inactive. Claiming here would record the track
        # as stopped while it kept spending, and the claim is one-shot.
        logger.error(
            "%s is past the disable ceiling at %s USD but no principal on it has an "
            "active access key to deactivate (tripped by %s, kind %s; users seen today %s)",
            attribution.track,
            _format_usd(total),
            principal.name,
            principal.kind,
            sorted(user_names),
        )
        _send_notice_once(
            aws_clients,
            topic_arn,
            track=attribution.track,
            day=day,
            kind="UNENFORCEABLE",
            subject=f"[Bedrock] cannot stop {attribution.track} - no access key",
            body={
                "track": attribution.track,
                "tripped_by": principal.name,
                "tripped_by_kind": principal.kind,
                "users_seen_today": sorted(user_names),
                "spent_usd": _format_usd(total),
                "ceiling_usd": _format_usd(budget.runaway_micros),
                "action": (
                    "Enforcement works by deactivating a long-lived access key. "
                    "A role-based caller has none. Stop the workload directly, or "
                    "attach a Bedrock deny policy to the principal."
                ),
            },
        )
        return

    # Claimed before the IAM call, so concurrent invocations cannot each
    # deactivate and each page.
    if not ledger.claim_disable(
        aws_clients["usage"],
        track=attribution.track,
        day=day,
        principal_keys=principal_keys,
        spent_micros=total,
    ):
        # Already disabled. A key that is still active now belongs to a user who
        # started spending afterwards, so it has to be switched off too -- the
        # claim is one-shot and would never fire again for it.
        _extend_disable(aws_clients, attribution, day, topic_arn, principal_keys, total)
        return

    try:
        deactivated_key_ids = enforce.deactivate_keys(aws_clients["iam"], principal_keys)
    except errors.DeactivationFailed as exc:
        if not exc.deactivated:
            # Nothing switched off, so the claim is a lie. A track over its
            # ceiling keeps producing lines, so the retry is seconds away.
            ledger.release_disable(aws_clients["usage"], attribution.track)
        else:
            # Partly done, so the claim is now an accurate record. Nothing
            # finishes the remaining keys automatically -- a human reads
            # still_active off the page.
            logger.error(
                "partial deactivation for %s: inactive=%s still active=%s",
                attribution.track,
                exc.deactivated,
                exc.still_active,
            )
        raise

    enforce.notify(
        aws_clients["sns"],
        topic_arn,
        f"[Bedrock] DISABLED {attribution.track} - runaway spend",
        {
            "track": attribution.track,
            "day": day,
            "spent_usd": _format_usd(total),
            "daily_budget_usd": _format_usd(budget.daily_micros),
            "ceiling_usd": _format_usd(budget.runaway_micros),
            "principals": sorted(principal_keys),
            "deactivated_access_keys": sorted(deactivated_key_ids),
            "release": (
                "Does not reactivate on its own. On-call rota re-enables via the "
                "bedrock-cost-limits-unblock function, after confirming what spent "
                "the money and that it has stopped."
            ),
        },
    )
    logger.error("DISABLED %s after %s USD", attribution.track, _format_usd(total))


def _extend_disable(
    aws_clients: dict,
    attribution,
    day: str,
    topic_arn: str | None,
    principal_keys: dict,
    total: int,
) -> None:
    """Switch off a key that appeared after the track was already disabled."""
    # No claim is taken first: the disabled record already exists, so there is
    # nothing to claim. Deactivating is idempotent and the ledger write uses set
    # ADD, so a concurrent repeat is harmless. The page is claimed per user
    # AFTER the act, which keeps the rule that a claim is releasable or late.
    if not principal_keys:
        return

    deactivated = enforce.deactivate_keys(aws_clients["iam"], principal_keys)
    ledger.extend_disable(aws_clients["usage"], attribution.track, principal_keys)
    logger.error(
        "extended the disable on %s to %s after %s USD",
        attribution.track,
        sorted(principal_keys),
        _format_usd(total),
    )

    for user_name in sorted(principal_keys):
        _send_notice_once(
            aws_clients,
            topic_arn,
            track=attribution.track,
            day=day,
            kind=f"DISABLE_EXTENDED#{user_name}",
            subject=f"[Bedrock] also DISABLED {user_name} on {attribution.track}",
            body={
                "track": attribution.track,
                "principal": user_name,
                "deactivated_access_keys": sorted(principal_keys[user_name]),
                "spent_usd": _format_usd(total),
                "action": (
                    "This track was already disabled when this principal started "
                    "spending on it, so its key has been deactivated too. Several "
                    "IAM users share this track's tag."
                ),
            },
        )


def _send_notice_once(
    aws_clients: dict,
    topic_arn: str | None,
    *,
    track: str,
    day: str,
    kind: str,
    subject,
    body,
):
    """At most once per track per day."""
    if not ledger.claim_notice(aws_clients["usage"], track, day, kind):
        return
    enforce.notify(aws_clients["sns"], topic_arn, subject, body)


def _format_usd(micros: int) -> str:
    return f"{Decimal(micros) / Decimal(1_000_000):.4f}"
