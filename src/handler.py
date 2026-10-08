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

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

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
        ddb = boto3.resource("dynamodb", config=config)
        _clients.update(
            usage=ddb.Table(USAGE_TABLE),
            budgets=ddb.Table(BUDGET_TABLE),
            iam=boto3.client("iam", config=config),
            sns=boto3.client("sns", config=config),
        )
    return _clients


def unpack(event: dict) -> list[str]:
    """base64(gzip(json)) -> the raw `message` strings, unparsed."""
    blob = event.get("awslogs", {}).get("data")
    if not blob:
        return []
    payload = json.loads(gzip.decompress(base64.b64decode(blob)))
    if payload.get("messageType") == "CONTROL_MESSAGE":
        return []  # subscription-filter health check, carries no invocations
    return [raw["message"] for raw in payload.get("logEvents", []) if "message" in raw]


def handler(event, context):  # noqa: ARG001 - context unused, signature is fixed
    stats = {"seen": 0, "counted": 0, "duplicate": 0, "failed": 0}
    failures: list[tuple[str, Exception]] = []

    c = clients()
    for raw in unpack(event):
        stats["seen"] += 1
        try:
            stats[process(raw, c)] += 1
        except Exception as exc:  # noqa: BLE001 - re-raised below, after the loop
            stats["failed"] += 1
            failures.append((_request_id_of(raw), exc))
            log.exception("failed on %s", _request_id_of(raw))

    log.info("batch %s", stats)
    if failures:
        raise errors.BatchIncomplete(stats, failures)
    return stats


def _request_id_of(raw: str) -> str:
    """Best effort, for the log line only. Never raises."""
    try:
        return str(json.loads(raw).get("requestId", "<no requestId>"))
    except Exception:  # noqa: BLE001 - this is the diagnostic path
        return "<unparseable>"


def process(raw: str, c: dict) -> str:
    """One invocation log line. Returns the stats key it belongs to."""
    try:
        entry = json.loads(raw)
    except ValueError as exc:
        raise errors.MalformedLogEntry(f"log event message is not JSON: {exc}") from exc
    if not isinstance(entry, dict):
        raise errors.MalformedLogEntry(f"log event message is not an object: {type(entry).__name__}")

    request_id = (entry.get("requestId") or "").strip()
    if not request_id:
        # No dedup key, so it cannot be counted exactly once.
        raise errors.MalformedLogEntry("log entry has no requestId, so it cannot be deduplicated")

    model_id = (entry.get("modelId") or "").strip()
    if not model_id:
        raise errors.MalformedLogEntry(f"log entry {request_id} has no modelId")

    arn = (entry.get("identity") or {}).get("arn", "")
    who = identity.resolve(arn, c["iam"], c["usage"])
    usage = pricing.usage_from_log(entry)
    priced = pricing.price(usage, model_id, entry.get("region"))
    day = ledger.utc_day()

    if ledger.claim_count(c["usage"], request_id):
        total = ledger.accumulate(
            c["usage"],
            track=who.track,
            day=day,
            model_id=model_id,
            principal_name=who.principal.name,
            usage=usage,
            micros=priced.micros,
        )
        outcome = "counted"
    else:
        # Already counted, by an earlier delivery or a concurrent invocation.
        # The money must not be added twice, but whoever counted it may have died
        # before alerting or disabling -- and every claim below is idempotent.
        total = ledger.current_total(c["usage"], who.track, day)
        outcome = "duplicate"

    react(who, day, total, c, model_id=model_id, fallback=priced.is_fallback)
    return outcome


def react(who, day: str, total: int, c: dict, *, model_id: str, fallback: bool) -> None:
    budget = budgets.load(c["budgets"], who.track)
    topic = budget.notify_topic_arn or NOTIFY_TOPIC_ARN
    decision = enforce.evaluate(total, budget)

    if budget.is_floor:
        _notice_once(
            c,
            topic,
            track=who.track,
            day=day,
            kind="NO_BUDGET",
            subject=f"[Bedrock] {who.track} has no budget row",
            body={
                "track": who.track,
                "owner": who.owner,
                "applied_daily_usd": _usd(budget.daily_micros),
                "action": (
                    "A conservative floor is being applied so the track is not "
                    "unlimited. Seed a TRACK# row, or DEFAULT, to set a real budget."
                ),
            },
        )

    if fallback:
        _notice_once(
            c,
            topic,
            track=who.track,
            day=day,
            kind=f"FALLBACK#{model_id}",
            subject=f"[Bedrock] unpriced model used by {who.track}",
            body={
                "track": who.track,
                "model_id": model_id,
                "action": "charged at the fallback rate; add it to the rate card",
            },
        )

    for pct in decision.crossed:
        if not ledger.claim_threshold(c["usage"], who.track, day, pct):
            continue  # another invocation already sent this one today
        enforce.notify(
            c["sns"],
            topic,
            f"[Bedrock] {who.track} at {pct}% of daily budget",
            {
                "track": who.track,
                "owner": who.owner,
                "env": who.env,
                "day": day,
                "threshold_pct": pct,
                # The gap between the threshold and the real figure is the
                # signal: 81% is a nudge, 140% means the disable is next.
                "actual_pct": f"{decision.pct:.1f}",
                "spent_usd": _usd(total),
                "daily_budget_usd": _usd(budget.daily_micros),
                "disable_at_usd": _usd(budget.runaway_micros),
                "budget_scope": budget.scope,
                "enforce": budget.enforce,
                "note": "Bedrock access is deactivated above the disable threshold.",
            },
        )

    if not decision.runaway:
        return
    if not budget.enforce:
        # The watch-only week in one line: what would have happened, with the
        # real number, without doing it.
        log.warning(
            "%s past the disable ceiling at %s USD (ceiling %s), but enforce=false",
            who.track,
            _usd(total),
            _usd(budget.runaway_micros),
        )
        return
    disable(who, day, total, budget, topic, c)


def disable(who, day: str, total: int, budget, topic: str | None, c: dict) -> None:
    principal = who.principal
    # Raises on failure: an empty set would be indistinguishable from "no keys".
    key_ids = enforce.access_keys_for(c["iam"], principal)

    if not key_ids:
        # A role, or keys already inactive. Claiming here would record the track
        # as stopped while it kept spending, and the claim is one-shot.
        log.error(
            "%s is past the disable ceiling at %s USD but has no active access key to "
            "deactivate (principal %s, kind %s)",
            who.track,
            _usd(total),
            principal.name,
            principal.kind,
        )
        _notice_once(
            c,
            topic,
            track=who.track,
            day=day,
            kind="UNENFORCEABLE",
            subject=f"[Bedrock] cannot stop {who.track} - no access key",
            body={
                "track": who.track,
                "owner": who.owner,
                "principal": principal.name,
                "principal_kind": principal.kind,
                "spent_usd": _usd(total),
                "ceiling_usd": _usd(budget.runaway_micros),
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
        c["usage"],
        track=who.track,
        day=day,
        principal_keys={principal.name: key_ids},
        spent_micros=total,
    ):
        return

    try:
        done = enforce.deactivate_keys(c["iam"], principal.name, key_ids)
    except errors.DeactivationFailed as exc:
        if not exc.deactivated:
            # Nothing switched off, so the claim is a lie. A track over its
            # ceiling keeps producing lines, so the retry is seconds away.
            ledger.release_disable(c["usage"], who.track)
        else:
            # Partly done, so the claim is now an accurate record. Nothing
            # finishes the remaining keys automatically -- a human reads
            # still_active off the page.
            log.error(
                "partial deactivation for %s: inactive=%s still active=%s",
                who.track,
                exc.deactivated,
                exc.still_active,
            )
        raise

    enforce.notify(
        c["sns"],
        topic,
        f"[Bedrock] DISABLED {who.track} - runaway spend",
        {
            "track": who.track,
            "owner": who.owner,
            "env": who.env,
            "day": day,
            "spent_usd": _usd(total),
            "daily_budget_usd": _usd(budget.daily_micros),
            "ceiling_usd": _usd(budget.runaway_micros),
            "principal": principal.name,
            "deactivated_access_keys": sorted(done),
            "release": (
                "Does not reactivate on its own. On-call rota re-enables via the "
                "bedrock-guardrail-unblock function, after confirming what spent "
                "the money and that it has stopped."
            ),
        },
    )
    log.error("DISABLED %s after %s USD", who.track, _usd(total))


def _notice_once(c: dict, topic: str | None, *, track: str, day: str, kind: str, subject, body):
    """At most once per track per day."""
    if not ledger.claim_notice(c["usage"], track, day, kind):
        return
    enforce.notify(c["sns"], topic, subject, body)


def _usd(micros: int) -> str:
    return f"{Decimal(micros) / Decimal(1_000_000):.4f}"
