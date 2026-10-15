"""identity.arn -> track, from IAM tags on the calling principal.

`Track` is the only tag read, and the whole attribution key, so this assumes
one key per track.
Three caches: in-process 15 min, DynamoDB 24 h, negative 60 s.
"""

from __future__ import annotations

import logging
import re
import time
from typing import NamedTuple

from errors import AttributionUnavailable, UntaggedPrincipal

logger = logging.getLogger(__name__)

__all__ = ["Principal", "Attribution", "parse_arn", "resolve", "reset_cache", "TRACK_TAG"]

TRACK_TAG = "Track"

_MEMO_TTL_SECONDS = 15 * 60
_ITEM_TTL_SECONDS = 24 * 60 * 60

# An untagged principal raises on every line it produces, so without this that
# is one ListUserTags per line, which throttles IAM for everyone else. Short,
# because the fix is someone adding a tag.
_NEGATIVE_TTL_SECONDS = 60

# arn:aws:iam::123456789012:user/BedrockDev-camunda-to-temporal
# arn:aws:sts::123456789012:assumed-role/ShinroRunner/i-0abc123def
# arn:aws:iam::123456789012:role/ShinroRunner
_ARN = re.compile(
    r"^arn:aws[a-z-]*:(?:iam|sts)::(?P<account>\d{12}):"
    r"(?P<kind>user|role|assumed-role)/(?P<path>.+)$"
)


class Principal(NamedTuple):
    kind: str  # "user" or "role"
    name: str
    arn: str


class Attribution(NamedTuple):
    track: str
    principal: Principal


# arn -> (expires_at_epoch, Attribution)
_memo_cache: dict[str, tuple[float, Attribution]] = {}
# arn -> expires_at_epoch, for principals known to be untagged
_untagged_cache: dict[str, float] = {}


def parse_arn(arn: str) -> Principal | None:
    """The IAM principal whose tags to read. None if the shape is unrecognised."""
    match = _ARN.match((arn or "").strip())
    if not match:
        return None
    kind, path = match.group("kind"), match.group("path")
    # The session name is per-session and meaningless for attribution.
    if kind == "assumed-role":
        return Principal("role", path.split("/", 1)[0], arn)
    return Principal(kind, path.rsplit("/", 1)[-1], arn)


def _tags_from_iam(iam, principal: Principal) -> dict[str, str]:
    if principal.kind == "user":
        response = iam.list_user_tags(UserName=principal.name)
    else:
        response = iam.list_role_tags(RoleName=principal.name)
    return {tag["Key"]: tag["Value"] for tag in response.get("Tags", [])}


def _from_tags(tags: dict[str, str], principal: Principal) -> Attribution:
    track = (tags.get(TRACK_TAG) or "").strip()
    if not track:
        raise UntaggedPrincipal(
            f"{principal.kind} {principal.name!r} has no {TRACK_TAG} tag. "
            f"Its Bedrock spend cannot be attributed or limited until it is "
            f"tagged. Tags present: {sorted(tags)}"
        )
    return Attribution(track=track, principal=principal)


def _read_item_cache(table, arn: str) -> Attribution | None:
    """The cross-invocation cache layer. Raises if the table cannot be read."""
    try:
        item = table.get_item(Key={"pk": f"ARN#{arn}", "sk": "TAGS"}).get("Item")
    except Exception as exc:
        raise AttributionUnavailable(f"tag cache read failed for {arn}: {exc!r}") from exc
    if not item:
        return None
    principal = parse_arn(arn)
    track = (item.get("track") or "").strip()
    if principal is None or not track:
        # Older schema or a partial write -- re-read IAM rather than trust half
        # a cache entry. This is also what ages out pre-`track` rows.
        logger.warning("discarding unusable tag cache row for %s", arn)
        return None
    return Attribution(track=track, principal=principal)


def _write_item_cache(table, arn: str, attribution: Attribution, now: float) -> None:
    item = {
        "pk": f"ARN#{arn}",
        "sk": "TAGS",
        "track": attribution.track,
        "ttl": int(now) + _ITEM_TTL_SECONDS,
    }
    try:
        table.put_item(Item=item)
    except Exception as exc:
        raise AttributionUnavailable(f"tag cache write failed for {arn}: {exc!r}") from exc


def resolve(arn: str, iam, table, now: float | None = None) -> Attribution:
    """Raises AttributionUnavailable (transient) or UntaggedPrincipal (permanent)."""
    now = time.time() if now is None else now

    memoized = _memo_cache.get(arn)
    if memoized and memoized[0] > now:
        return memoized[1]

    untagged_until = _untagged_cache.get(arn)
    if untagged_until and untagged_until > now:
        raise UntaggedPrincipal(
            f"{arn} has no {TRACK_TAG} tag (cached for up to "
            f"{_NEGATIVE_TTL_SECONDS}s to avoid throttling IAM)"
        )

    principal = parse_arn(arn)
    if principal is None:
        raise AttributionUnavailable(
            f"identity arn is not a shape this can read tags from: {arn!r}"
        )

    cached_attribution = _read_item_cache(table, arn)
    if cached_attribution is not None:
        _memo_cache[arn] = (now + _MEMO_TTL_SECONDS, cached_attribution)
        return cached_attribution

    try:
        tags = _tags_from_iam(iam, principal)
    except Exception as exc:
        raise AttributionUnavailable(
            f"IAM tag lookup failed for {principal.kind} {principal.name!r}: {exc!r}"
        ) from exc

    try:
        attribution = _from_tags(tags, principal)
    except UntaggedPrincipal:
        _untagged_cache[arn] = now + _NEGATIVE_TTL_SECONDS
        raise

    _memo_cache[arn] = (now + _MEMO_TTL_SECONDS, attribution)
    _write_item_cache(table, arn, attribution, now)
    return attribution


def reset_cache() -> None:
    """Drop the in-process caches. For tests, and for a forced re-read."""
    _memo_cache.clear()
    _untagged_cache.clear()
