"""AWS Price List API -> a rate card. The only module that knows the SKU shape.

The price list does not carry the Bedrock `modelId` that appears in the
invocation log; it carries a model name and a usage type. MODEL_IDS maps one to
the other, so a new model needs one line here and its prices then track weekly.
Anything this cannot classify is reported, never guessed: see README.md.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

import pricing
from errors import PriceListUnavailable

logger = logging.getLogger(__name__)

__all__ = ["SERVICE_CODE", "MODEL_IDS", "FetchResult", "fetch", "normalize_name"]

SERVICE_CODE = "AmazonBedrock"

# Price-list model name -> the modelId the invocation log reports, normalized by
# `normalize_name`. A name missing here is listed in the refresh alert and its
# model keeps falling to pricing.FALLBACK, which over-charges and alerts daily.
# That is the safe direction: a wrong-but-cheap guess would under-count spend.
MODEL_IDS: dict[str, str] = {
    "claude sonnet 4 5": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude 4 5 sonnet": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude haiku 4 5": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude 4 5 haiku": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude opus 4 1": "anthropic.claude-opus-4-1-20250805-v1:0",
    "claude 4 1 opus": "anthropic.claude-opus-4-1-20250805-v1:0",
}

# Attributes that may carry the model name, in order of preference.
_NAME_ATTRIBUTES = ("model", "modelName", "titanModel", "usagetype")

# The four token classes are spelled in the usage type, not in an attribute of
# their own. Cache first: "CacheReadInputTokenCount" also contains "inputtoken".
_TOKEN_CLASS_MARKERS = (
    ("cache_read", ("cachereadinput", "cacheread")),
    ("cache_write", ("cachewriteinput", "cachewrite")),
    ("input", ("inputtoken",)),
    ("output", ("outputtoken",)),
)

# Our card is USD per MILLION tokens. The price list quotes per unit, and the
# unit varies. An unknown unit raises: guessing is a 1000x error in every budget.
_TOKENS_PER_UNIT = {
    "token": Decimal(1),
    "tokens": Decimal(1),
    "1k tokens": Decimal(1_000),
    "1000 tokens": Decimal(1_000),
    "1m tokens": Decimal(1_000_000),
    "1000000 tokens": Decimal(1_000_000),
    "1m token": Decimal(1_000_000),
}

_ON_DEMAND_MARKERS = ("ondemand", "on demand")
_MILLION = Decimal(1_000_000)


class FetchResult(NamedTuple):
    rates: dict[str, pricing.Rates]
    unmapped_names: tuple[str, ...]  # seen in the price list, absent from MODEL_IDS
    defaulted: tuple[str, ...]  # "<model_id>.cache_write" priced at the input rate
    skus_read: int


def normalize_name(value: str) -> str:
    """Lowercase, non-alphanumerics to spaces: 'Claude 4.5 Sonnet' -> 'claude 4 5 sonnet'."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _token_class(usage_type: str) -> str | None:
    flat = re.sub(r"[^a-z]+", "", str(usage_type or "").lower())
    for name, markers in _TOKEN_CLASS_MARKERS:
        if any(marker.replace(" ", "") in flat for marker in markers):
            return name
    return None

# Sample PriceList entry, for reference. The product attributes carry the model name and usage type, and the terms carry the price. The SKU is a unique
# identifier.
#{
#   "product": {                          ← WHAT it is
#     "sku": "7Y4PZ8KQNXR2EXAMPLE",
#     "attributes": {
#       "model": "Claude Sonnet 4.5",
#       "usagetype": "USE1-InputTokenCount",
#       "inferenceType": "On-demand",     ← _is_on_demand reads this
#     }
#   },
#   "terms": {                            ← WHAT IT COSTS
#     "OnDemand": {
#       "7Y4PZ8KQNXR2EXAMPLE.JRTCKXETXF": {
#         "priceDimensions": {
#           "7Y4PZ8KQNXR2EXAMPLE.JRTCKXETXF.6YS6EN2CT7": {
#             "beginRange": "0",
#             "endRange": "Inf",
#             "unit": "1K tokens",
#             "pricePerUnit": { "USD": "0.0030000000" }
#           }
#         }
#       }
#     }

def _is_on_demand(attributes: dict) -> bool:
    inference = normalize_name(attributes.get("inferenceType", ""))
    if not inference:
        # Absent on some records; the on-demand term lookup still gates those.
        return True
    flat = inference.replace(" ", "")
    return any(marker in flat or marker in inference for marker in _ON_DEMAND_MARKERS)


def _usd_per_million(dimension: dict, sku: str) -> Decimal:
    raw = (dimension.get("pricePerUnit") or {}).get("USD")
    if raw is None:
        raise PriceListUnavailable(f"sku {sku} has no USD price dimension")
    try:
        per_unit = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise PriceListUnavailable(f"sku {sku} has a non-numeric USD price {raw!r}") from exc

    unit = normalize_name(dimension.get("unit", ""))
    tokens_per_unit = _TOKENS_PER_UNIT.get(unit)
    if tokens_per_unit is None:
        raise PriceListUnavailable(
            f"sku {sku} is priced per {dimension.get('unit')!r}, which this does not know how "
            f"to convert to USD per million tokens. Add it to pricelist._TOKENS_PER_UNIT "
            f"after confirming the unit against the price list."
        )
    return per_unit * (_MILLION / tokens_per_unit)


def _on_demand_dimensions(product: dict, sku: str) -> list[dict]:
    """The base on-demand tier. Tiered SKUs are skipped, not averaged."""
    dimensions = []
    for term in (product.get("terms") or {}).get("OnDemand", {}).values():
        for dimension in (term.get("priceDimensions") or {}).values():
            if str(dimension.get("beginRange", "0")) in ("0", "0.0"):
                dimensions.append(dimension)
    if len(dimensions) > 1:
        raise PriceListUnavailable(
            f"sku {sku} has {len(dimensions)} base on-demand price dimensions; "
            f"this cannot tell which one bills a token"
        )
    return dimensions


def _model_name(attributes: dict) -> str:
    for key in _NAME_ATTRIBUTES:
        value = attributes.get(key)
        if value:
            return str(value)
    return ""


def fetch(pricing_client, region_code: str) -> FetchResult:
    """Every on-demand Bedrock token rate for one region, as USD per million."""
    collected: dict[str, dict[str, Decimal]] = {}
    unmapped: dict[str, None] = {}
    skus_read = 0

    try:
        pages = pricing_client.get_paginator("get_products").paginate(
            ServiceCode=SERVICE_CODE,
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region_code},
            ],
        )
        raw_pages = [page for page in pages]
    except Exception as exc:
        raise PriceListUnavailable(
            f"could not read the {SERVICE_CODE} price list for {region_code}: {exc!r}"
        ) from exc

    for page in raw_pages:
        # PriceList entries arrive as JSON strings, not objects.
        for entry in page.get("PriceList", []):
            skus_read += 1
            product = entry if isinstance(entry, dict) else json.loads(entry)
            attributes = (product.get("product") or {}).get("attributes") or {}
            sku = (product.get("product") or {}).get("sku", "<no sku>")

            token_class = _token_class(attributes.get("usagetype", ""))
            if token_class is None or not _is_on_demand(attributes):
                continue  # provisioned throughput, batch, images, embeddings

            name = _model_name(attributes)
            model_id = MODEL_IDS.get(normalize_name(name))
            if model_id is None:
                unmapped[name or sku] = None
                continue

            dimensions = _on_demand_dimensions(product, sku)
            if not dimensions:
                continue
            rate = _usd_per_million(dimensions[0], sku)
            if rate <= 0:
                continue  # free and placeholder SKUs exist; a zero rate is no limit
            collected.setdefault(model_id, {})[token_class] = rate

    if not collected:
        raise PriceListUnavailable(
            f"read {skus_read} {SERVICE_CODE} SKUs for {region_code} and recognised no token "
            f"rate. Either the filters match nothing or the usage-type spelling changed; "
            f"confirm with `aws pricing get-products --service-code {SERVICE_CODE} "
            f"--region us-east-1 --max-results 1`."
        )

    rates: dict[str, pricing.Rates] = {}
    defaulted: list[str] = []
    for model_id, classes in sorted(collected.items()):
        if "input" not in classes or "output" not in classes:
            # Input and output are always billed. Missing one means the parse is
            # wrong for this model, and half a card is worse than none.
            raise PriceListUnavailable(
                f"{model_id} came back with {sorted(classes)} and no input/output pair"
            )
        for cache_class in ("cache_read", "cache_write"):
            if cache_class not in classes:
                # Not every model supports caching, and its cached token counts
                # are then zero, so the rate never multiplies anything. Default
                # to the input rate: no discount assumed, which errs expensive.
                classes[cache_class] = classes["input"]
                defaulted.append(f"{model_id}.{cache_class}")
        rates[model_id] = pricing.Rates(
            input=classes["input"],
            output=classes["output"],
            cache_read=classes["cache_read"],
            cache_write=classes["cache_write"],
        )

    return FetchResult(
        rates=rates,
        unmapped_names=tuple(sorted(unmapped)),
        defaulted=tuple(defaulted),
        skus_read=skus_read,
    )
