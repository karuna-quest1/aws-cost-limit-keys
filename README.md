# Setup and operations

A step-by-step guide to getting this running, what you have to supply, what the
tables look like, and where each alert goes.


---

## Before you start

| You need | Check with | If missing |
| --- | --- | --- |
| AWS CLI v2, logged in | `aws sts get-caller-identity` | `brew install awscli`, then `aws configure` |
| SAM CLI | `sam --version` | `brew install aws-sam-cli` |
| Python 3.9+ | `python3 --version` | already on macOS |

You also need permission to create IAM roles, Lambda functions, DynamoDB tables,
SNS topics and SQS queues in the target account.

**Pick your region and stick to it.** Bedrock invocation logging is configured
*per region*, so a track calling Bedrock in `eu-west-1` is invisible to a stack
deployed in `us-east-1`. Start with the one region your tracks actually use.

---

## The nine steps, in order

The order matters in two places, called out as you reach them.

```
1. make test                     prove the code works, no AWS needed
2. sam deploy                    create everything
3. confirm the alert email       a human clicks a link
4. tag the IAM users             ← MUST be before step 6
5. create the log group + role
6. enable invocation logging     ← the spend starts being seen here
7. seed the budget table
8. watch for a week              enforce stays false
9. make enforce TRACK=...        one track at a time
```

---

### Step 1 — run the tests

```bash
make test
```

```
Ran 59 tests in 0.010s
OK
```

No AWS account, no credentials, no network, nothing to install. If this fails,
stop — nothing after this point will work either.

### Step 2 — deploy

```bash
make deploy          # = sam build && sam deploy --guided
```

You will be asked for these. The ones that matter are in bold.

| Prompt | Enter | Notes |
| --- | --- | --- |
| Stack Name | `bedrock-guardrail` | any name; it groups everything |
| AWS Region | `us-east-1` | **the region your tracks call Bedrock in** |
| **`BedrockLogGroupName`** | press Enter for `/aws/bedrock/modelinvocations` | you create this in step 5 |
| **`PrincipalNamePattern`** | e.g. `BedrockDev-*` | **the IAM users this may touch** — see below |
| **`AlertEmail`** | your email | optional, but then nothing alerts |
| Confirm changes before deploy | `y` | shows you the changeset first |
| Allow SAM CLI IAM role creation | `Y` | it needs to make the Lambda roles |
| Save arguments to configuration file | `Y` | so later deploys are just `sam deploy` |

> **`PrincipalNamePattern` is the blast radius.** It restricts which IAM users
> this system can read tags from and deactivate keys on. If you set it to `*`,
> a bug could deactivate *anyone's* access key, including yours. Use a prefix
> that only your Bedrock keys have.

This creates nine things:

```
bedrock_usage                       DynamoDB table (+ one index, + auto-expiry)
bedrock_budgets                     DynamoDB table
bedrock-guardrail-alerts            SNS topic
  └─ your email                     SNS subscription (if you gave one)
bedrock-guardrail-meter-dlq         SQS queue, for batches that fail
bedrock-guardrail-meter             Lambda — the meter
bedrock-guardrail-unblock           Lambda — the release
bedrock-guardrail-meter-errors      CloudWatch alarm
bedrock-guardrail-meter-dlq-not-empty   CloudWatch alarm
```

Note the values you will need later:

```bash
aws cloudformation describe-stacks --stack-name bedrock-guardrail \
  --query 'Stacks[0].Outputs' --output table
```

### Step 3 — confirm the email

AWS sent a *"Subscription Confirmation"* email. **Someone has to click the link
in it.** Until that happens the topic has zero subscribers and every alert this
system sends goes nowhere, silently.

Check:

```bash
TOPIC=$(aws cloudformation describe-stacks --stack-name bedrock-guardrail \
  --query 'Stacks[0].Outputs[?OutputKey==`AlertTopicArn`].OutputValue' --output text)

aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" \
  --query 'Subscriptions[].{endpoint:Endpoint,arn:SubscriptionArn}' --output table
```

A real ARN in the `arn` column means confirmed. The literal word
`PendingConfirmation` means not yet.

Adding more people later needs no redeploy:

```bash
aws sns subscribe --topic-arn "$TOPIC" --protocol email --endpoint teammate@company.com
```

### Step 4 — tag the IAM users  ⚠️ before step 6

**This is the one step people skip, and skipping it floods you with errors.**

Every IAM user that calls Bedrock must carry a `Track` tag. That tag *is* the
attribution — it decides which budget applies, which row the spend lands on, and
which key gets deactivated. An untagged user raises an error on **every log line
it produces**, which fires the error alarm continuously and fills the
dead-letter queue with batches that can never succeed.

**One key per track** is the rule this assumes. Six tracks means six IAM users,
six keys, six tags.

```bash
aws iam tag-user --user-name BedrockDev-camunda-to-temporal \
  --tags Key=Track,Value=camunda_to_temporal \
         Key=Owner,Value=platform-team \
         Key=Env,Value=dev
```

| Tag | Required | Used for |
| --- | --- | --- |
| `Track` | **yes** | the budget boundary and the ledger key |
| `Owner` | no | appears in alerts, so you know who to call |
| `Env` | no | appears in alerts, so you know if it is prod |

Then check nothing is missed:

```bash
for u in $(aws iam list-users \
    --query 'Users[?starts_with(UserName, `BedrockDev-`)].UserName' --output text); do
  t=$(aws iam list-user-tags --user-name "$u" \
        --query 'Tags[?Key==`Track`].Value' --output text)
  printf '%-45s %s\n' "$u" "${t:-MISSING}"
done
```

Anything printing `MISSING` must be tagged, or taken out of
`PrincipalNamePattern`, before you continue.

> Roles work too (`aws iam tag-role`), and they will be metered and alerted
> normally — but roles have no long-lived access keys, so **enforcement cannot
> switch them off.** A role past its ceiling raises a distinct "cannot stop this
> track — no access key" alert instead.

### Step 5 — create the log group and the logging role

Bedrock needs somewhere to write and permission to write there. Neither is in
the template.

```bash
# 1. the log group
aws logs create-log-group --log-group-name /aws/bedrock/modelinvocations
aws logs put-retention-policy --log-group-name /aws/bedrock/modelinvocations \
  --retention-in-days 30

# 2. a role Bedrock can assume to write to it
cat > /tmp/trust.json <<'JSON'
{ "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow",
                  "Principal": { "Service": "bedrock.amazonaws.com" },
                  "Action": "sts:AssumeRole" }] }
JSON

aws iam create-role --role-name BedrockInvocationLoggingRole \
  --assume-role-policy-document file:///tmp/trust.json

cat > /tmp/perms.json <<'JSON'
{ "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow",
                  "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                  "Resource": "arn:aws:logs:*:*:log-group:/aws/bedrock/modelinvocations:*" }] }
JSON

aws iam put-role-policy --role-name BedrockInvocationLoggingRole \
  --policy-name WriteInvocationLogs --policy-document file:///tmp/perms.json
```

### Step 6 — enable invocation logging

**Bedrock invocation logging is off by default.** Until you turn it on, nothing
is logged, so nothing is metered, and this entire system sits idle doing nothing.

It is also **not a CloudFormation resource**, which is why `sam deploy` could not
do it for you. One-time, per region.

**The important part: leave every data-delivery option OFF.**

Token counts are *metadata* and are recorded regardless. The data-delivery
options control whether the actual **prompt and response text** is written to
the log group. You do not need it and you do not want a second copy of customer
prompts in a log group with different retention and different access controls.

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

aws bedrock put-model-invocation-logging-configuration --logging-config "{
  \"cloudWatchConfig\": {
    \"logGroupName\": \"/aws/bedrock/modelinvocations\",
    \"roleArn\": \"arn:aws:iam::${ACCOUNT}:role/BedrockInvocationLoggingRole\"
  },
  \"textDataDeliveryEnabled\": false,
  \"imageDataDeliveryEnabled\": false,
  \"embeddingDataDeliveryEnabled\": false
}"

# confirm
aws bedrock get-model-invocation-logging-configuration
```

> The exact set of `*DataDeliveryEnabled` flags has grown over time (video was
> added later). If the CLI rejects one, the console route is more forgiving:
> **Bedrock → Settings → Model invocation logging** — pick the log group and the
> role, and leave **every checkbox unchecked**.

Spend starts being metered from this moment.

### Step 7 — seed the budget table

Nothing is enforced yet, but without a budget row every track falls to a
hardcoded $10 floor and sends you a daily "no budget row" warning.

```bash
# the DEFAULT row — every track inherits whatever it does not set itself
make seed DAILY=25

# one row per track
make seed-track TRACK=camunda_to_temporal DAILY=50
make seed-track TRACK=memcached_to_elasticache DAILY=30
```

Both write `enforce: false`. See [the budget table](#bedrock_budgets) below for
what each field does.

### Step 8 — watch for a week

Do not skip this. The price list in `src/pricing.py` was typed by hand from
AWS's pricing page and **has not been checked against a real bill**. If a rate is
wrong, every number this system produces is wrong — and being wrong in the
direction of "deactivate the key" means halting a team's work over arithmetic
nobody verified.

Each day:

```bash
make status                    # today's spend per track
make status DAY=2026-10-07     # a past day
```

Three things to watch:

| Check | Means |
| --- | --- |
| `make status` totals ≈ Cost Explorer for the same day | the price list is right |
| the error alarm is quiet | nothing untagged or misconfigured |
| the dead-letter queue is empty | no spend is being lost |

```bash
# is the DLQ empty?
DLQ=$(aws cloudformation describe-stacks --stack-name bedrock-guardrail \
  --query 'Stacks[0].Outputs[?OutputKey==`MeterDeadLetterQueueUrl`].OutputValue' --output text)
aws sqs get-queue-attributes --queue-url "$DLQ" \
  --attribute-names ApproximateNumberOfMessagesVisible
```

During this week the system still meters everything and still emails you at 80%
and 100%. The only thing it will not do is deactivate a key. When a track passes
its ceiling you get a log line instead:

```
camunda_to_temporal past the disable ceiling at 76.4012 USD (ceiling 75.0000), but enforce=false
```

That line is the point of the week — exactly what *would* have happened, with
the real number, without doing it.

### Step 9 — turn on enforcement

One track at a time.

```bash
make enforce TRACK=camunda_to_temporal

# and to turn it back off
make watch TRACK=camunda_to_temporal
```

From now on, that track passing `daily_usd × runaway_multiple` deactivates its
access keys and pages you. It will not come back on its own.

---

## The DynamoDB tables

### `bedrock_budgets`

Configuration. You edit this; the system only reads it. Tiny, never expires.

**Partition key: `scope`.** No sort key.

| Field | Type | Example | Rules |
| --- | --- | --- | --- |
| `scope` | string | `TRACK#camunda_to_temporal` | or the literal `DEFAULT` |
| `daily_usd` | number | `50` | must be **> 0** |
| `runaway_multiple` | number | `1.5` | must be **≥ 1**; deactivate at `daily_usd × this` |
| `alert_pcts` | number set | `{80, 100}` | non-empty, each 1–1000 |
| `notify_topic_arn` | string | `arn:aws:sns:...` | optional — **leave it out**, see the warning below |
| `enforce` | boolean | `false` | must be a real boolean, not `"false"` |
| `updated_at` | string | `2026-10-08T09:12:00Z` | audit trail only |

**Sample contents**

```
scope                            daily_usd  runaway_multiple  alert_pcts  enforce
───────────────────────────────────────────────────────────────────────────────────
DEFAULT                                 25               1.5   {80, 100}   false
TRACK#camunda_to_temporal               50               1.5   {80, 100}    true
TRACK#memcached_to_elasticache          80                 -           -       -
TRACK#gap_to_clickhouse                  -                 -           -    true
```

**How a lookup resolves.** It is a *merge*, not first-match — a row only has to
set what differs from `DEFAULT`:

| Asked for | Uses | Daily | Ceiling | Enforcing | Reads |
| --- | --- | --- | --- | --- | --- |
| `camunda_to_temporal` | its own complete row | $50 | $75 | yes | 1 |
| `memcached_to_elasticache` | its `daily_usd`, rest from `DEFAULT` | $80 | $120 | no | 2 |
| `gap_to_clickhouse` | its `enforce`, rest from `DEFAULT` | $25 | $37.50 | **yes** | 2 |
| `sqlserver_to_postgres` (no row) | `DEFAULT` entirely | $25 | $37.50 | no | 2 |
| anything, with an **empty table** | the hardcoded floor | **$10** | $15 | no | 2 |

A complete row costs one read; `DEFAULT` is only fetched when something is
missing. The $10 floor is a last resort and sends you a warning every day it is
used.

Anything unusable **raises an error** rather than being quietly replaced:

```
TRACK#x.daily_usd must be greater than zero, got 0. To stop a track spending,
deactivate its key; a zero budget disables the limit rather than the track.
```

> ⚠️ **Do not set `notify_topic_arn` yet.** The override works, but nothing
> checks that the topic you name has a confirmed subscriber — and publishing to a
> topic with no subscribers *succeeds silently*. See `TODO.md` item 22.

### `bedrock_usage`

The ledger. The system writes this constantly; you only read it.

**Partition key `pk`, sort key `sk`.** Five different kinds of record share this
one table, told apart by the prefix on `pk`:

```
TRACK#<track>  ┬  DAY#<date>                   the money — one per track per day
               ├  DAY#<date>#NOTICE#<kind>     "already sent that warning today"
               └  DISABLED                      "this track's keys are off"
ARN#<arn>      ─  TAGS                          cached tag lookup
REQ#<id>       ─  SEEN                          "already counted this request"
```

This is normal for DynamoDB and is called single-table design. Only the key
fields are declared anywhere; every record can carry completely different
fields.

**1. The money**

```
pk = TRACK#camunda_to_temporal
sk = DAY#2026-10-08
{
  "spent_micros":       61950,                   ← $0.06195
  "calls":                  1,
  "in_tokens":          12400,
  "out_tokens":           850,
  "cache_read_tokens":  40000,
  "cache_write_tokens":     0,
  "by_model":     { "us.anthropic.claude-sonnet-4-5-20250929-v1:0": 61950 },
  "by_principal": { "BedrockDev-camunda-to-temporal": 61950 },
  "alerted":      [80, 100],                     ← thresholds already emailed
  "day":  "2026-10-08",                          ← the reporting index key
  "ttl":  1796539045                             ← auto-deletes after 60 days
}
```

Money is stored as **integer micro-dollars** — divide by 1,000,000. `61950` is
$0.06195. Integers because DynamoDB's atomic increment on an integer is exact
and floats drift.

All four token counts are kept, not just the dollar total, so that if a price
turns out to be wrong you can recompute history. `by_model` and `by_principal`
answer *"what spent it?"* without a second table.

**The date is in the sort key**, and that is the whole daily reset: tomorrow is
`DAY#2026-10-09`, a different record that does not exist yet, so it starts at
zero on its own. There is no nightly job.

**2. A once-a-day warning marker**

```
pk = TRACK#camunda_to_temporal
sk = DAY#2026-10-08#NOTICE#NO_BUDGET
{ "ttl": 1796550649 }
```

Holds nothing. Its *existence* is the message: "someone already sent this
warning today." Without it, a track with no budget row would email you once per
Bedrock call. Three kinds: `NO_BUDGET`, `FALLBACK#<modelId>`, `UNENFORCEABLE`.

**3. The disabled record**

```
pk = TRACK#camunda_to_temporal
sk = DISABLED
{
  "disabled_at":      "2026-10-08T10:29:50+00:00",
  "disabled_day":     "2026-10-08",
  "spend_at_disable": 309750,
  "principals":       ["BedrockDev-camunda-to-temporal"],
  "keys":             ["BedrockDev-camunda-to-temporal/AKIAEXAMPLE1",
                       "BedrockDev-camunda-to-temporal/AKIAEXAMPLE2"]
}
```

**No expiry** — the only record here without one. If it expired you would have a
dead access key and nothing explaining what killed it. Deleted only by a human
re-enabling.

`keys` stores `user/keyid` pairs so the release path knows which key belongs to
which user.

**4. The tag cache**

```
pk = ARN#arn:aws:iam::123456789012:user/BedrockDev-camunda-to-temporal
sk = TAGS
{ "track": "camunda_to_temporal", "owner": "platform-team", "env": "dev",
  "ttl": 1791441445 }                            ← 24 hours
```

So IAM is not asked on every log line. Rebuilds itself automatically.

**5. The duplicate marker**

```
pk = REQ#req-0001
sk = SEEN
{ "ttl": 1791527845 }                            ← 48 hours
```

Also holds nothing — existence means "already counted". CloudWatch can deliver
the same log line twice, and double-charging a track is the bug that would
destroy trust in the whole thing.

**The reporting index (`GSI1`)**

The main table is organised by track, so it cannot answer *"all tracks on one
day"*. A second index flips the keys to answer exactly that, and because only
the money records carry a `day` field, the index contains nothing else. That is
what `make status` queries — one lookup, no table scan.

---

## What you have to supply, and where

| What | Where you put it | When |
| --- | --- | --- |
| region | `sam deploy` prompt | step 2 |
| `PrincipalNamePattern` | `sam deploy` prompt | step 2 |
| one alert email | `sam deploy` prompt | step 2 |
| more alert emails | `aws sns subscribe` | any time, no redeploy |
| `Track` tag per IAM user | `aws iam tag-user` | step 4 |
| log group name | `sam deploy` prompt + step 5 must match | steps 2 and 5 |
| the logging role | `aws iam create-role` | step 5 |
| `DEFAULT` budget | `make seed DAILY=25` | step 7 |
| a track's budget | `make seed-track TRACK=... DAILY=...` | step 7 |
| enforcement on/off | `make enforce` / `make watch` | step 9 |

Everything except the tags and the budget rows is set once at deploy.

---

## Alerts: what is sent, and where

Everything goes to **one SNS topic**, `bedrock-guardrail-alerts`, and from there
to whoever is subscribed to it. One email address subscribed means one inbox
receives all of this.

| Alert | Sent when | How often | Topic |
| --- | --- | --- | --- |
| `at 80% of daily budget` | spend ≥ 80% of `daily_usd` | once per day | track's, else default |
| `at 100% of daily budget` | spend ≥ 100% | once per day | track's, else default |
| `DISABLED <track> — runaway spend` | past the ceiling **and** `enforce: true` | once | track's, else default |
| `RE-ENABLED <track>` | someone ran `make unblock` | once | track's, else default |
| `<track> has no budget row` | the $10 floor was applied | once per day | track's, else default |
| `unpriced model used by <track>` | that model is not in the price list | once per day **per model** | track's, else default |
| `cannot stop <track> — no access key` | past the ceiling, nothing to deactivate | once per day | track's, else default |
| **`bedrock-guardrail-meter-errors`** | the meter itself failed | per 5-minute window | **always the default** |
| **`bedrock-guardrail-meter-dlq-not-empty`** | a batch gave up after retries | per 5-minute window | **always the default** |

"track's, else default" means: the track's budget row can name its own topic, and
if it does not, the stack's topic is used. **Right now no row should name one**
(see `TODO.md` item 22), so in practice everything lands on the stack topic.

The last two are AWS watching AWS — CloudWatch alarms do not read your budget
table, so they can never be routed per track. That is correct: "the meter is
broken" is a platform problem, not one track's problem.

### The two alarms mean different things

| Alarm | Severity |
| --- | --- |
| `meter-errors` | something failed; AWS is retrying automatically, probably fine |
| `meter-dlq-not-empty` | something **gave up**. That spend is **not in the ledger**, so every budget and ceiling for the affected tracks is reading low |

### What a budget alert looks like

```json
{
  "track": "camunda_to_temporal",
  "owner": "platform-team",
  "env": "dev",
  "day": "2026-10-08",
  "threshold_pct": 80,
  "actual_pct": "92.9",
  "spent_usd": "0.1858",
  "daily_budget_usd": "0.2000",
  "disable_at_usd": "0.3000",
  "budget_scope": "TRACK#camunda_to_temporal",
  "enforce": true,
  "note": "Bedrock access is deactivated above the disable threshold."
}
```

Three fields worth knowing:

- **`threshold_pct` vs `actual_pct`** — 80 vs 92.9. One big call jumped clean
  over the threshold. The gap is the signal.
- **`disable_at_usd`** — how much room is left before the key is switched off.
- **`enforce`** — whether anything will actually happen. During the watch week
  this reads `false`, so the same email is informational rather than urgent.

### What a disable page looks like

```json
{
  "track": "camunda_to_temporal",
  "spent_usd": "0.3098",
  "ceiling_usd": "0.3000",
  "principal": "BedrockDev-camunda-to-temporal",
  "deactivated_access_keys": ["AKIAEXAMPLE1", "AKIAEXAMPLE2"],
  "release": "Does not reactivate on its own. On-call rota re-enables via the
              bedrock-guardrail-unblock function, after confirming what spent
              the money and that it has stopped."
}
```

The runbook is in the alert, so nobody has to go looking for it at 9pm.

---

## Day-to-day commands

```bash
make status                                   # today's spend, every track
make status DAY=2026-10-07                    # a past day

make seed-track TRACK=<name> DAILY=<n>        # add or reset a track's budget
make enforce TRACK=<name>                     # arm enforcement for one track
make watch   TRACK=<name>                     # disarm it

make unblock TRACK=<name> REASON='...'        # re-enable a disabled track
make test                                     # after any code change
```

### Re-enabling a disabled track

Three steps, in order. The function **refuses a reason under 10 characters**, so
the diagnosis is enforced rather than suggested.

**1. What is disabled**

```bash
aws dynamodb scan --table-name bedrock_usage \
  --filter-expression 'sk = :s' \
  --expression-attribute-values '{":s":{"S":"DISABLED"}}' \
  --query 'Items[].{track:pk.S,at:disabled_at.S,spend:spend_at_disable.N}' --output table
```

**2. What spent the money** — this is what `by_model` and `by_principal` are for

```bash
aws dynamodb get-item --table-name bedrock_usage \
  --key '{"pk":{"S":"TRACK#camunda_to_temporal"},"sk":{"S":"DAY#2026-10-08"}}' \
  --query 'Item.{spent:spent_micros.N,calls:calls.N,by_model:by_model.M}'
```

One model holding nearly all the spend with `calls` in the thousands means a
loop. That is your reason.

**3. Fix the cause, then re-enable**

```bash
make unblock TRACK=camunda_to_temporal REASON='wave-7f3a21 loop fixed, retry cap added'
```

> **Re-enabling does not reset the day's spend.** The track is still above its
> ceiling, so if the cause has not actually stopped, the next Bedrock call
> switches the keys straight back off. If you genuinely need headroom for the
> rest of the day, raise the budget instead:
> `make seed-track TRACK=camunda_to_temporal DAILY=100`

---

## When something looks wrong

| Symptom | Likely cause |
| --- | --- |
| No records in `bedrock_usage` at all | invocation logging not enabled (step 6), or enabled in a different region |
| `make status` shows nothing | no spend today, or the log group name in the stack does not match the one Bedrock writes to |
| Error alarm firing constantly | an IAM user is missing its `Track` tag (step 4) |
| Daily "has no budget row" email | that track needs `make seed-track`, or a `DEFAULT` row |
| Daily "unpriced model" email | that model is not in `src/pricing.py` — see `TODO.md` item 4 |
| No emails at all, ever | the subscription was never confirmed (step 3) |
| Spend recorded but nothing enforced | `enforce` is still `false` — that is the default, by design |
| "cannot stop — no access key" | that track runs under a role, which has no key to deactivate |
| DLQ alarm firing | a batch failed three times; that spend is missing from the ledger |

### Is the meter actually running?

```bash
aws logs tail /aws/lambda/bedrock-guardrail-meter --follow
```

A healthy batch logs one line:

```
batch {'seen': 12, 'counted': 11, 'duplicate': 1, 'failed': 0}
```

| Counter | Zero means |
| --- | --- |
| `seen` | nothing is arriving — the trigger or the logging is broken |
| `counted` | **the meter is running and recording nothing** |
| `duplicate` | normal |
| `failed` | normal |

---

## Two things this cannot do

**It cannot prevent spending.** Bedrock never asks permission — the call is
answered and billed before the Lambda sees the log line. A runaway loop starting
at 3am is caught within a minute or so, but that minute is already charged. It
contains damage; it does not prevent it.

**It can only stop IAM users with access keys.** A track running under a role
gets metered and alerted normally, but there is no long-lived key to deactivate,
so enforcement cannot bite. You get a distinct alert saying exactly that.

---

## Removing it

```bash
aws cloudformation delete-stack --stack-name bedrock-guardrail
```

Deletes everything the stack made. **Not** deleted, because the stack did not
create them: the Bedrock logging configuration (turn it off separately), the log
group, the logging role, and the `Track` tags on your IAM users.

If any track is currently disabled, **re-enable it before deleting the stack** —
otherwise its access keys stay inactive and the record explaining why is gone
with the table.
