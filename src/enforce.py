"""What a spend total means, and doing something about it.

`evaluate` is pure, so the decision table is testable without IAM or SNS.
Nothing degrades: see errors.py.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import NamedTuple

from errors import (
    AlertDeliveryFailed,
    DeactivationFailed,
    InvalidBudget,
    KeyLookupFailed,
)

log = logging.getLogger(__name__)

__all__ = ["Decision", "evaluate", "notify", "deactivate_keys", "access_keys_for"]

_SNS_SUBJECT_LIMIT = 100


class Decision(NamedTuple):
    crossed: tuple[int, ...]  # alert thresholds newly reached, ascending
    runaway: bool  # spend is past the deactivation ceiling
    pct: Decimal  # how far through the daily budget, for the alert body


def evaluate(spent_micros: int, budget) -> Decision:
    """Pure. Every threshold reached (not just the highest) and whether to disable."""
    # Guarding the division instead is how zero -- the most restrictive-looking
    # value in the table -- became the only one that meant no limit at all.
    if budget.daily_micros <= 0:
        raise InvalidBudget(
            f"budget {budget.scope!r} has daily_micros={budget.daily_micros}; "
            f"no threshold can be evaluated against it"
        )
    # Every threshold, not just the highest: one large call can jump 70% -> 120%.
    # The caller claims each separately, so only unseen ones send.
    pct = Decimal(spent_micros) * 100 / Decimal(budget.daily_micros)
    return Decision(
        crossed=tuple(p for p in budget.alert_pcts if pct >= p),
        runaway=spent_micros >= budget.runaway_micros,
        pct=pct,
    )


def notify(sns, topic_arn: str | None, subject: str, body: dict) -> str:
    """Publish one alert. Raises on failure, including a missing topic."""
    if not topic_arn:
        raise AlertDeliveryFailed(
            f"no SNS topic configured, so this alert has nowhere to go: {subject!r}. "
            f"Set notify_topic_arn on the budget row or NOTIFY_TOPIC_ARN on the function."
        )
    try:
        resp = sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:_SNS_SUBJECT_LIMIT],  # SNS hard limit
            Message=json.dumps(body, indent=2, default=str),
        )
    except Exception as exc:
        raise AlertDeliveryFailed(f"SNS publish failed for {subject!r}: {exc!r}") from exc
    return str(resp.get("MessageId", ""))


def access_keys_for(iam, principal) -> set[str]:
    """Active access key ids. Empty set means none; a failed lookup raises."""
    if principal is None or principal.kind != "user":
        # Roles issue temporary credentials, so there is nothing to deactivate.
        # A property of the credential type, not a failure.
        return set()
    try:
        resp = iam.list_access_keys(UserName=principal.name)
    except Exception as exc:
        raise KeyLookupFailed(
            f"could not list access keys for {principal.name!r}, so there is no "
            f"way to know what to deactivate: {exc!r}"
        ) from exc
    return {
        k["AccessKeyId"]
        for k in resp.get("AccessKeyMetadata", [])
        if k.get("Status") == "Active"
    }


def deactivate_keys(iam, user_name: str, access_key_ids: set[str]) -> set[str]:
    """Deactivate, never delete. Returns the ids now inactive."""
    # Status="Active" is never passed from here, and that is the only thing
    # stopping this from undoing a disable: the IAM grant covers both directions
    # and IAM has no condition key for a key's status. A real boundary needs
    # this call in its own function holding that permission alone.
    #
    # Every key is attempted before anything raises, so one throttled call
    # cannot leave the rest live. A partial result still raises.
    done, failed = set(), {}
    for key_id in sorted(access_key_ids):
        try:
            iam.update_access_key(UserName=user_name, AccessKeyId=key_id, Status="Inactive")
            done.add(key_id)
        except Exception as exc:
            log.error("could not deactivate %s for %s", key_id, user_name, exc_info=True)
            failed[key_id] = repr(exc)
    if failed:
        raise DeactivationFailed(
            f"{len(failed)} of {len(access_key_ids)} access keys for {user_name!r} are "
            f"still active: {failed}",
            deactivated=sorted(done),
            still_active=sorted(failed),
        )
    return done
