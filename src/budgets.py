"""The per-track budget: TRACK# merged over DEFAULT over a floor, validated.

A merge, not a first match: a row setting one field would otherwise fall to the
$10 floor, which is the rollout happy path. See SETUP.md.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from errors import BudgetUnavailable, InvalidBudget

log = logging.getLogger(__name__)

__all__ = ["Budget", "load", "FLOOR_DAILY_USD", "FLOOR_SCOPE"]

# Only when neither a track row nor DEFAULT exists. A newly issued key must not
# be unlimited, and raising would mean no limit is evaluated at all. Alerted
# daily by handler.react.
FLOOR_DAILY_USD = Decimal("10")
FLOOR_SCOPE = "FLOOR"

_DEFAULT_ALERT_PCTS = (80, 100)
_DEFAULT_RUNAWAY_MULTIPLE = Decimal("1.5")

# Fields a partial TRACK# row inherits from DEFAULT rather than from the floor.
_INHERITABLE = ("daily_usd", "runaway_multiple", "alert_pcts", "notify_topic_arn", "enforce")

# Below 1 the key is disabled before the 100% alert, inverting the escalation.
_MIN_RUNAWAY_MULTIPLE = Decimal("1")
_MAX_ALERT_PCT = 1000


class Budget(NamedTuple):
    scope: str
    daily_micros: int
    alert_pcts: tuple[int, ...]
    runaway_micros: int
    notify_topic_arn: str | None
    enforce: bool

    @property
    def daily_usd(self) -> Decimal:
        return Decimal(self.daily_micros) / Decimal(1_000_000)

    @property
    def is_floor(self) -> bool:
        return self.scope == FLOOR_SCOPE


def _decimal(item: dict, field: str, default: Decimal, scope: str) -> Decimal:
    raw = item.get(field)
    if raw is None:
        return default
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidBudget(f"{scope}.{field} is not a number: {raw!r}") from exc


def _alert_pcts(item: dict, scope: str) -> tuple[int, ...]:
    raw = item.get("alert_pcts")
    if raw is None:
        return _DEFAULT_ALERT_PCTS
    try:
        pcts = tuple(sorted(int(p) for p in raw))
    except (TypeError, ValueError) as exc:
        raise InvalidBudget(f"{scope}.alert_pcts is not a set of numbers: {raw!r}") from exc
    if not pcts:
        raise InvalidBudget(
            f"{scope}.alert_pcts is empty. An empty set means no threshold is ever "
            f"announced; remove the attribute to take the default "
            f"{_DEFAULT_ALERT_PCTS} instead."
        )
    bad = [p for p in pcts if p <= 0 or p > _MAX_ALERT_PCT]
    if bad:
        raise InvalidBudget(f"{scope}.alert_pcts has values outside 1..{_MAX_ALERT_PCT}: {bad}")
    return pcts


def _coerce(item: dict, scope: str) -> Budget:
    daily_usd = _decimal(item, "daily_usd", FLOOR_DAILY_USD, scope)
    if daily_usd <= 0:
        raise InvalidBudget(
            f"{scope}.daily_usd must be greater than zero, got {daily_usd}. "
            f"To stop a track spending, deactivate its key; a zero budget "
            f"disables the limit rather than the track."
        )

    multiple = _decimal(item, "runaway_multiple", _DEFAULT_RUNAWAY_MULTIPLE, scope)
    if multiple < _MIN_RUNAWAY_MULTIPLE:
        raise InvalidBudget(
            f"{scope}.runaway_multiple must be at least {_MIN_RUNAWAY_MULTIPLE}, "
            f"got {multiple}. Below 1 the key is deactivated before the 100% "
            f"alert is sent."
        )

    enforce_raw = item.get("enforce", False)
    if not isinstance(enforce_raw, bool):
        raise InvalidBudget(
            f"{scope}.enforce must be a boolean, got {type(enforce_raw).__name__} "
            f"({enforce_raw!r}). A string 'false' is truthy and would start "
            f"deactivating keys."
        )

    topic = item.get("notify_topic_arn")
    if topic is not None and not str(topic).startswith("arn:"):
        raise InvalidBudget(f"{scope}.notify_topic_arn is not an ARN: {topic!r}")

    daily_micros = int(daily_usd * 1_000_000)
    return Budget(
        scope=scope,
        daily_micros=daily_micros,
        alert_pcts=_alert_pcts(item, scope),
        runaway_micros=int(Decimal(daily_micros) * multiple),
        notify_topic_arn=topic,
        # Absent means watch-only: enforcement is opt-in.
        enforce=enforce_raw,
    )


def _read(table, scope: str) -> dict | None:
    try:
        return table.get_item(Key={"scope": scope}).get("Item")
    except Exception as exc:
        # Swallowing this is indistinguishable from "row absent", which silently
        # applies a smaller ceiling than the one configured.
        raise BudgetUnavailable(f"budget read failed for scope {scope!r}: {exc!r}") from exc


def load(table, track: str) -> Budget:
    """One read when the track's row is complete, two when partial or absent."""
    scope = f"TRACK#{track}"
    row = _read(table, scope)

    if row is not None and all(field in row for field in _INHERITABLE):
        return _coerce(row, scope)

    default = _read(table, "DEFAULT")
    if row is None and default is None:
        return _coerce({}, FLOOR_SCOPE)

    merged = dict(default or {})
    merged.update({k: v for k, v in (row or {}).items() if v is not None})
    if row is not None and default is not None:
        inherited = sorted(set(_INHERITABLE) - set(row))
        if inherited:
            log.info("%s inherited %s from DEFAULT", scope, inherited)
    return _coerce(merged, scope if row is not None else "DEFAULT")
