"""Synthetic CloudWatch Logs events, in the exact wire shape Lambda receives."""

from __future__ import annotations

import base64
import gzip
import json

SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
TRACK = "camunda_to_temporal"
ARN_USER = "arn:aws:iam::123456789012:user/BedrockDev-camunda-to-temporal"
ARN_ASSUMED = "arn:aws:sts::123456789012:assumed-role/ShinroRunner/i-0abc123def"
USER_NAME = "BedrockDev-camunda-to-temporal"


def log_entry(
    request_id="abcd1234-5678-efgh-ijkl",
    arn=ARN_USER,
    model_id=SONNET,
    in_tokens=12_400,
    out_tokens=850,
    cache_read=40_000,
    cache_write=0,
):
    return {
        "schemaType": "ModelInvocationLog",
        "schemaVersion": "1.0",
        "timestamp": "2026-10-06T14:14:00Z",
        "accountId": "123456789012",
        "region": "us-east-1",
        "requestId": request_id,
        "operation": "Converse",
        "modelId": model_id,
        # Documented on ModelInvocationLog and ignored by this code, but present
        # on every real line. Carried here so the fixture cannot drift into being
        # a shape AWS never sends.
        "inputContentType": "application/json",
        "outputContentType": "application/json",
        "identity": {"arn": arn},
        "input": {
            "inputTokenCount": in_tokens,
            "cacheReadInputTokenCount": cache_read,
            "cacheWriteInputTokenCount": cache_write,
        },
        "output": {"outputTokenCount": out_tokens},
    }


def cloudwatch_event(entries):
    """`entries` may be dicts, or raw strings for the malformed-line tests."""
    payload = {
        "messageType": "DATA_MESSAGE",
        "logGroup": "/aws/bedrock/modelinvocations",
        "logStream": "aws/bedrock/modelinvocations",
        "logEvents": [
            {
                "id": str(i),
                "timestamp": 1_760_000_000_000 + i,
                "message": e if isinstance(e, str) else json.dumps(e),
            }
            for i, e in enumerate(entries)
        ],
    }
    blob = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(blob).decode()}}
