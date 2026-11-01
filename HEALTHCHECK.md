# Health check — commands

Everything here is read-only. Nothing in this file changes state, so it is safe
to run any of it at any time, including during an incident.

`DEPLOY.md` is the install checklist; this is "is it working right now?".

---

## Set these once per shell

```bash
STACK=bedrock-cost-limits
REGION=$(aws configure get region)

TOPIC=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`AlertTopicArn`].OutputValue' --output text)
DLQ=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`MeterDeadLetterQueueUrl`].OutputValue' --output text)

echo "topic: $TOPIC"
echo "dlq  : $DLQ"
```

---

## The 60-second check

Five questions. If all five are fine, the system is healthy.

```bash
echo "=== 1. all three alarms OK? ==="
aws cloudwatch describe-alarms \
  --alarm-names bedrock-cost-limits-meter-errors \
                bedrock-cost-limits-meter-dlq-not-empty \
                bedrock-cost-limits-rate-refresh-errors \
  --query 'MetricAlarms[].{alarm:AlarmName,state:StateValue}' --output table

echo "=== 2. dead-letter queue empty? ==="
aws sqs get-queue-attributes --queue-url "$DLQ" \
  --attribute-names ApproximateNumberOfMessagesVisible \
  --query 'Attributes.ApproximateNumberOfMessagesVisible' --output text

echo "=== 3. meter actually metering? ==="
aws logs tail /aws/lambda/bedrock-cost-limits-meter --since 15m --format short \
  | grep batch | tail -5

echo "=== 4. prices present and fresh? ==="
aws dynamodb get-item --table-name bedrock_budgets \
  --key '{"scope":{"S":"RATECARD#CURRENT"}}' \
  --query 'Item.fetched_at.S' --output text

echo "=== 5. anything disabled right now? ==="
aws dynamodb scan --table-name bedrock_usage \
  --filter-expression 'sk = :s' \
  --expression-attribute-values '{":s":{"S":"DISABLED"}}' \
  --query 'Items[].{track:pk.S,at:disabled_at.S}' --output table
```

**What good looks like**

| | Healthy | Act if |
| --- | --- | --- |
| 1 | all three `OK` | any `ALARM` — see the sections below |
| 2 | `0` | anything above 0. **Spend is missing from the ledger** |
| 3 | `batch {'seen': N, 'counted': N, ..., 'failed': 0}` | `failed` above 0, or no lines at all during working hours |
| 4 | a timestamp within the last 7 days | empty, or older than ~10 days |
| 5 | empty table | a row — that track's keys are off, someone needs to act |

---

## 1. Is the meter running?

```bash
# live
aws logs tail /aws/lambda/bedrock-cost-limits-meter --follow | grep batch

# last hour
aws logs tail /aws/lambda/bedrock-cost-limits-meter --since 1h | grep batch
```

One line per invocation:

```
batch {'seen': 12, 'counted': 11, 'duplicate': 1, 'failed': 0}
```

| Counter | Zero means |
| --- | --- |
| `seen` | **nothing is arriving** — the trigger or invocation logging is broken |
| `counted` | **running and recording nothing** — read the error above it |
| `duplicate` | normal (occasional redelivery is expected, not required) |
| `failed` | normal, and what you want |

`seen` always equals `counted + duplicate + failed`.

### Errors, with the reason

```bash
aws logs tail /aws/lambda/bedrock-cost-limits-meter --since 1h \
  --filter-pattern "ERROR"

# just the typed failures
aws logs tail /aws/lambda/bedrock-cost-limits-meter --since 1h \
  | grep -E "UntaggedPrincipal|MalformedLogEntry|LedgerWriteFailed|AlertDeliveryFailed|KeyLookupFailed|RateCard"
```

### Is it being throttled? (no alarm covers this)

The meter is capped at 10 concurrent executions. Throttling is **not** an error,
so no alarm fires — metering just falls behind and some events are eventually
dropped.

```bash
# macOS
START=$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)
# Linux:  START=$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)

aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Throttles --statistics Sum --period 300 \
  --dimensions Name=FunctionName,Value=bedrock-cost-limits-meter \
  --start-time "$START" --end-time "$END" \
  --query 'sort_by(Datapoints,&Timestamp)[].{t:Timestamp,throttles:Sum}' --output table
```

Anything above zero means invocations were refused. Raise
`ReservedConcurrentExecutions` or accept delayed enforcement.

---

## 2. Is the dead-letter queue empty?

```bash
aws sqs get-queue-attributes --queue-url "$DLQ" \
  --attribute-names ApproximateNumberOfMessagesVisible \
                    ApproximateNumberOfMessagesNotVisible \
                    ApproximateAgeOfOldestMessage \
  --output table
```

**A non-empty DLQ is the most urgent signal in the system.** Each message is a
batch of Bedrock invocations whose spend is **not in the ledger**, so every
affected track's budget and ceiling is reading low.

```bash
# look at one, without removing it
aws sqs receive-message --queue-url "$DLQ" --max-number-of-messages 1 \
  --visibility-timeout 0 --query 'Messages[].Body' --output text | head -c 400
```

The body is the original gzipped CloudWatch event, so it can be replayed once
the cause is fixed.

> **Replay within about four days.** The dedup rows that stop a line being
> counted twice expire after 48 hours, plus up to 48 more of lazy deletion.
> Later than that and a replay double-counts. See `ALERTS_AND_TTL.md`.

---

## 3. Are the prices current?

```bash
make rates
```

or directly:

```bash
aws dynamodb get-item --table-name bedrock_budgets \
  --key '{"scope":{"S":"RATECARD#CURRENT"}}' \
  --query 'Item.{fetched_at:fetched_at.S,region:region.S,previous:previous_scope.S}' \
  --output table
```

**An empty result means there are no prices at all** and every model is being
charged at the fallback rate — about 6.7× Sonnet. Fix with `make refresh-rates`.

```bash
# the actual numbers in use
aws dynamodb get-item --table-name bedrock_budgets \
  --key '{"scope":{"S":"RATECARD#CURRENT"}}' --query 'Item.models.M' --output json

# how many rate card rows exist -- should be exactly 2, or 1 before the second refresh
aws dynamodb scan --table-name bedrock_budgets \
  --filter-expression 'begins_with(#s, :p)' \
  --expression-attribute-names '{"#s":"scope"}' \
  --expression-attribute-values '{":p":{"S":"RATECARD#"}}' \
  --query 'Items[].scope.S' --output table
```

More than two rate card rows means the rotation is not deleting the old one —
check that `RefreshRatesFunction` has `dynamodb:DeleteItem`.

```bash
# did the weekly job run, and what did it say?
aws logs tail /aws/lambda/bedrock-cost-limits-refresh-rates --since 8d
```

---

## 4. What is spending?

```bash
make status                      # today, every track
make status DAY=2026-11-01       # a past day
```

**`spent_micros` is millionths of a dollar.** `309750` is `$0.3098`.

```bash
# with the dollars worked out
aws dynamodb query --table-name bedrock_usage --index-name GSI1 \
  --key-condition-expression '#d = :day' \
  --expression-attribute-names '{"#d":"day"}' \
  --expression-attribute-values "{\":day\":{\"S\":\"$(date -u +%F)\"}}" \
  --query 'Items[].[pk.S,spent_micros.N,calls.N]' --output text \
  | awk '{printf "%-45s $%8.4f  %s calls\n", $1, $2/1000000, $3}'
```

### One track, one day — the diagnosis view

```bash
TRACK=camunda_to_temporal
aws dynamodb get-item --table-name bedrock_usage \
  --key "{\"pk\":{\"S\":\"TRACK#$TRACK\"},\"sk\":{\"S\":\"DAY#$(date -u +%F)\"}}" \
  --query 'Item.{spent:spent_micros.N,calls:calls.N,in:in_tokens.N,out:out_tokens.N,by_model:by_model.M,by_principal:by_principal.M,users:user_principals.SS}' \
  --output json
```

| What you see | What it means |
| --- | --- |
| one principal holding nearly all the spend, `calls` in the thousands | a retry loop in that workload |
| spend spread evenly across principals | the budget is genuinely too low |
| one model holding nearly all of it, and it is Opus | somebody switched model without checking the price |
| `calls` small but spend huge | one enormous prompt |

### The budgets in force

```bash
aws dynamodb scan --table-name bedrock_budgets \
  --filter-expression 'begins_with(#s, :t) OR #s = :d' \
  --expression-attribute-names '{"#s":"scope"}' \
  --expression-attribute-values '{":t":{"S":"TRACK#"},":d":{"S":"DEFAULT"}}' \
  --query 'Items[].{scope:scope.S,daily_usd:daily_usd.N,multiple:runaway_multiple.N,enforce:enforce.BOOL}' \
  --output table
```

`enforce: True` on any row is the only thing that lets a key be deactivated.

---

## 5. Is anything disabled?

```bash
aws dynamodb scan --table-name bedrock_usage \
  --filter-expression 'sk = :s' \
  --expression-attribute-values '{":s":{"S":"DISABLED"}}' \
  --query 'Items[].{track:pk.S,at:disabled_at.S,day:disabled_day.S,spend:spend_at_disable.N,principals:principals.SS,keys:keys.SS}' \
  --output json
```

Confirm against IAM — the ledger and reality should agree:

```bash
aws iam list-access-keys --user-name BedrockDev-camunda-to-temporal \
  --query 'AccessKeyMetadata[].{id:AccessKeyId,status:Status}' --output table
```

| Ledger | IAM | Meaning |
| --- | --- | --- |
| `DISABLED` row | keys `Inactive` | consistent, as designed |
| `DISABLED` row | a key still `Active` | **investigate** — a user may share the tag and have appeared late, or a deactivation partly failed |
| no row | keys `Inactive` | something other than this control switched them off |

Release with `make unblock TRACK=<name> REASON='...'` (10 characters minimum).

---

## 6. Will alerts actually arrive?

```bash
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" \
  --query 'Subscriptions[].{endpoint:Endpoint,protocol:Protocol,arn:SubscriptionArn}' \
  --output table
```

A real ARN means **confirmed**. The literal string `PendingConfirmation` means
that address receives nothing — someone has an unread confirmation email.

```bash
# prove the whole path, end to end
aws sns publish --topic-arn "$TOPIC" \
  --subject "[Bedrock] health check - ignore" \
  --message "wiring check $(date -u +%FT%TZ)"
```

If that does not arrive, no real alert will either.

### What has already been sent today

Alerts are claimed once per day, so the claim rows are a record of what went out:

```bash
TRACK=camunda_to_temporal
aws dynamodb query --table-name bedrock_usage \
  --key-condition-expression 'pk = :p AND begins_with(sk, :s)' \
  --expression-attribute-values \
    "{\":p\":{\"S\":\"TRACK#$TRACK\"},\":s\":{\"S\":\"DAY#$(date -u +%F)#NOTICE#\"}}" \
  --query 'Items[].sk.S' --output table

# account-wide notices (stale rate card, refresh gaps)
aws dynamodb query --table-name bedrock_usage \
  --key-condition-expression 'pk = :p' \
  --expression-attribute-values '{":p":{"S":"TRACK#_SYSTEM"}}' \
  --query 'Items[].sk.S' --output table
```

A threshold claim lives on the day row instead:

```bash
aws dynamodb get-item --table-name bedrock_usage \
  --key "{\"pk\":{\"S\":\"TRACK#$TRACK\"},\"sk\":{\"S\":\"DAY#$(date -u +%F)\"}}" \
  --query 'Item.alerted.NS' --output text
```

`80` means the 80% email has gone out today and will not repeat.

---

## 7. Is attribution working?

```bash
# every BedrockDev-* user and its Track tag
aws iam list-users --query 'Users[?starts_with(UserName, `BedrockDev-`)].UserName' \
  --output text | tr '\t' '\n' | while read -r u; do
    track=$(aws iam list-user-tags --user-name "$u" \
      --query 'Tags[?Key==`Track`].Value' --output text)
    printf '%-45s %s\n' "$u" "${track:-*** NO TRACK TAG ***}"
done
```

An untagged user raises on **every log line it produces**, which fires the error
alarm continuously and fills the DLQ with batches that can never succeed.

```bash
# which tracks have more than one key (fine, but worth knowing)
aws iam list-users --query 'Users[?starts_with(UserName, `BedrockDev-`)].UserName' \
  --output text | tr '\t' '\n' | while read -r u; do
    track=$(aws iam list-user-tags --user-name "$u" \
      --query 'Tags[?Key==`Track`].Value' --output text)
    printf '%s\t%s\n' "$track" "$u"
done | sort | awk '{print $1}' | uniq -d

# the cached attribution (24h TTL)
aws dynamodb scan --table-name bedrock_usage \
  --filter-expression 'begins_with(pk, :p)' \
  --expression-attribute-values '{":p":{"S":"ARN#"}}' \
  --query 'Items[].{arn:pk.S,track:track.S}' --output table
```

Re-tagging a user takes up to 24 hours to take effect. To force it, delete that
`ARN#...` row — the in-process memo still holds for up to 15 more minutes.

---

## 8. Is logging still on?

```bash
aws bedrock get-model-invocation-logging-configuration
```

All three data-delivery flags must be `false` — token counts without prompt or
response text.

```bash
# is anything arriving at all?
aws logs tail /aws/bedrock/modelinvocations --since 15m | head -3

# retention set? (otherwise this group grows forever)
aws logs describe-log-groups --log-group-name-prefix /aws/bedrock/modelinvocations \
  --query 'logGroups[].{name:logGroupName,retentionDays:retentionInDays,bytes:storedBytes}' \
  --output table
```

`retentionDays: null` means never expires.

---

## 9. Is the stack still as deployed?

```bash
aws cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].{status:StackStatus,updated:LastUpdatedTime}' --output table

# has anyone changed a resource outside CloudFormation?
aws cloudformation detect-stack-drift --stack-name "$STACK" \
  --query 'StackDriftDetectionId' --output text
# then, with that id:
aws cloudformation describe-stack-drift-detection-status \
  --stack-drift-detection-id <id> \
  --query '{status:DetectionStatus,drifted:StackDriftStatus,count:DriftedStackResourceCount}'

aws lambda get-function-configuration --function-name bedrock-cost-limits-meter \
  --query '{timeout:Timeout,memory:MemorySize,concurrency:ReservedConcurrentExecutions,env:Environment.Variables}' \
  --output json
```

---

## Symptom → command

| Symptom | Start here |
| --- | --- |
| `meter-errors` alarm | section 1 — the typed failure in the logs says which |
| `meter-dlq-not-empty` alarm | section 2. **Urgent** — spend is missing from the ledger |
| `rate-refresh-errors` alarm | section 3 — the refresher's own logs |
| no alerts ever, for anything | section 6 — probably an unconfirmed subscription |
| "unpriced model" email daily | section 3. Empty card → `make refresh-rates`. Otherwise the model needs a `MODEL_IDS` line |
| "has no budget row" email daily | section 4 — `make seed-track TRACK=... DAILY_USD=...` |
| "rate card has not refreshed in weeks" | section 3 — the weekly job has stopped being triggered |
| "cannot stop <track>" email | that track runs under a role; no long-lived key to deactivate |
| every track's spend looks ~6× too high | section 3 — no card, everything at the fallback rate |
| spend recorded but nothing enforced | section 4 — `enforce` is still `false`, which is the default |
| a track is disabled and nobody knows why | section 5, then the `by_model`/`by_principal` view in section 4 |
| `make status` shows nothing | section 8 — logging off, or the wrong region |
| keys inactive but no `DISABLED` row | section 5 — something else switched them off |
