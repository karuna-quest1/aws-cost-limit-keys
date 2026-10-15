"""Typed failures. Nothing here is recovered from in-process.

Raise rather than carry on with wrong data: the meter is invoked asynchronously,
so a raise gets two retries then the DLQ. Two deliberate exceptions, both
alerted daily -- `pricing.FALLBACK` and `budgets.FLOOR_DAILY_USD`.
"""

from __future__ import annotations

__all__ = [
    "CostLimitError",
    "MalformedLogEntry",
    "AttributionUnavailable",
    "UntaggedPrincipal",
    "BudgetUnavailable",
    "InvalidBudget",
    "LedgerWriteFailed",
    "AlertDeliveryFailed",
    "KeyLookupFailed",
    "DeactivationFailed",
    "NotDisabled",
    "ReactivationFailed",
    "BatchIncomplete",
    "RateCardUnavailable",
    "RateCardRejected",
    "PriceListUnavailable",
    "aws_error_code",
    "is_conditional_check_failure",
    "is_validation_error",
]


class CostLimitError(Exception):
    """Base for every failure this project raises deliberately."""


class MalformedLogEntry(CostLimitError):
    """A log line is not a usable ModelInvocationLog."""


class AttributionUnavailable(CostLimitError):
    """Could not determine the track. Transient -- retry is the right response."""


class UntaggedPrincipal(CostLimitError):
    """No `Track` tag. Permanent until someone tags the key."""


class BudgetUnavailable(CostLimitError):
    """The budget table could not be read."""


class InvalidBudget(CostLimitError):
    """A configured budget value is not usable."""


class LedgerWriteFailed(CostLimitError):
    """A DynamoDB write the meter depends on did not happen."""

    def __init__(self, message: str, *, may_have_written: bool = False):
        super().__init__(message)
        # True when the write may have landed despite the failure. A dedup claim
        # must then be kept: releasing it would count the same call twice.
        self.may_have_written = may_have_written


class AlertDeliveryFailed(CostLimitError):
    """The alert did not reach SNS. Fatal: the threshold claim is already spent."""


class KeyLookupFailed(CostLimitError):
    """Could not list a principal's keys. Distinct from "has no keys" (empty set)."""


class DeactivationFailed(CostLimitError):
    """At least one key that should be inactive is still active."""

    def __init__(self, message: str, *, deactivated=(), still_active=()):
        super().__init__(message)
        self.deactivated = tuple(deactivated)
        self.still_active = tuple(still_active)


class NotDisabled(CostLimitError):
    """Asked to re-enable a track that is not disabled."""


class ReactivationFailed(CostLimitError):
    """At least one key could not be set back to Active."""

    def __init__(self, message: str, *, reactivated=(), still_inactive=()):
        super().__init__(message)
        self.reactivated = tuple(reactivated)
        self.still_inactive = tuple(still_inactive)


class BatchIncomplete(CostLimitError):
    """Some log lines failed. Raised after every line has been attempted."""

    def __init__(self, stats: dict, failures):
        self.stats = dict(stats)
        self.failures = list(failures)
        detail = "; ".join(f"{request_id}: {exc!r}" for request_id, exc in self.failures[:5])
        super().__init__(
            f"{len(self.failures)} of {stats.get('seen', 0)} log lines failed -- {detail}"
        )


class RateCardUnavailable(CostLimitError):
    """The refreshed rate card could not be read. The baseline is used instead."""


class RateCardRejected(CostLimitError):
    """A fetched rate card failed validation and was not promoted."""

    def __init__(self, message: str, *, reasons=()):
        super().__init__(message)
        self.reasons = tuple(reasons)


class PriceListUnavailable(CostLimitError):
    """The AWS Price List API could not be read or could not be understood."""


# botocore raises one class, ClientError, for everything; the discriminator is a
# string in the response body. Reading it here keeps botocore out of the modules
# that must import without it.


def aws_error_code(exc: BaseException) -> str:
    """Never raises -- it is called from inside `except` blocks."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("Code") or "")


def is_conditional_check_failure(exc: BaseException) -> bool:
    """A conditional write lost the race. Expected, and always an answer."""
    return aws_error_code(exc) == "ConditionalCheckFailedException"


def is_validation_error(exc: BaseException) -> bool:
    """The request itself was rejected -- a malformed or impossible expression."""
    return aws_error_code(exc) == "ValidationException"
