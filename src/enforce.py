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

logger = logging.getLogger(__name__)

__all__ = [
    "Decision",
    "evaluate",
    "notify",
    "deactivate_keys",
    "access_keys_for",
    "access_keys_for_user",
]

_SNS_SUBJECT_LIMIT = 100


class Decision(NamedTuple):
    crossed_pcts: tuple[int, ...]  # alert thresholds newly reached, ascending
    is_runaway: bool  # spend is past the deactivation ceiling
    pct_of_budget: Decimal  # how far through the daily budget, for the alert body


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
    pct_of_budget = Decimal(spent_micros) * 100 / Decimal(budget.daily_micros)
    return Decision(
        crossed_pcts=tuple(pct for pct in budget.alert_pcts if pct_of_budget >= pct),
        is_runaway=spent_micros >= budget.runaway_micros,
        pct_of_budget=pct_of_budget,
    )


def notify(sns, topic_arn: str | None, subject: str, body: dict) -> str:
    """Publish one alert. Raises on failure, including a missing topic."""
    if not topic_arn:
        raise AlertDeliveryFailed(
            f"no SNS topic configured, so this alert has nowhere to go: {subject!r}. "
            f"Set notify_topic_arn on the budget row or NOTIFY_TOPIC_ARN on the function."
        )
    try:
        response = sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:_SNS_SUBJECT_LIMIT],  # SNS hard limit
            Message=json.dumps(body, indent=2, default=str),
        )
    except Exception as exc:
        raise AlertDeliveryFailed(f"SNS publish failed for {subject!r}: {exc!r}") from exc
    return str(response.get("MessageId", ""))


def access_keys_for(iam, principal) -> set[str]:
    """Active access key ids. Empty set means none; a failed lookup raises."""
    if principal is None or principal.kind != "user":
        # Roles issue temporary credentials, so there is nothing to deactivate.
        # A property of the credential type, not a failure.
        return set()
    return access_keys_for_user(iam, principal.name)


def access_keys_for_user(iam, user_name: str) -> set[str]:
    """Active key ids for a name already known to be an IAM user."""
    # Taken by name because the disable path works from the track's recorded
    # users, where the kind was decided when the spend was accumulated.
    try:
        response = iam.list_access_keys(UserName=user_name)
    except Exception as exc:
        raise KeyLookupFailed(
            f"could not list access keys for {user_name!r}, so there is no "
            f"way to know what to deactivate: {exc!r}"
        ) from exc
    return {
        key["AccessKeyId"]
        for key in response.get("AccessKeyMetadata", [])
        if key.get("Status") == "Active"
    }


def deactivate_keys(iam, principal_keys: dict[str, set[str]]) -> set[str]:
    """Deactivate every key of every user given. Returns the ids now inactive."""
    # Status="Active" is never passed from here, and that is the only thing
    # stopping this from undoing a disable: the IAM grant covers both directions
    # and IAM has no condition key for a key's status. A real boundary needs
    # this call in its own function holding that permission alone.
    #
    # Every key of every user is attempted before anything raises, so one
    # throttled call cannot leave the rest live. A partial result still raises.
    deactivated, failures = set(), {}
    total = sum(len(key_ids) for key_ids in principal_keys.values())
    for user_name in sorted(principal_keys):
        for key_id in sorted(principal_keys[user_name]):
            try:
                iam.update_access_key(
                    UserName=user_name, AccessKeyId=key_id, Status="Inactive"
                )
                deactivated.add(key_id)
            except Exception as exc:
                logger.error(
                    "could not deactivate %s for %s", key_id, user_name, exc_info=True
                )
                failures[key_id] = repr(exc)
    if failures:
        raise DeactivationFailed(
            f"{len(failures)} of {total} access keys across "
            f"{sorted(principal_keys)} are still active: {failures}",
            deactivated=sorted(deactivated),
            still_active=sorted(failures),
        )
    return deactivated
