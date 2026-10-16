# Alerts and expiry — a reference

Two questions this answers:

1. **What email can arrive, when, and how often?**
2. **Which DynamoDB rows expire, after how long, and which never do?**

Every number here comes from the code. If you change a constant, change it here too.

---

## Part 1 — Alerts

### Where they go

Ten of the thirteen signals are **SNS messages** published by this code. Three
are **CloudWatch alarms**, which AWS raises on its own.

```
                       ┌─ the track's own topic, if its budget row names one
SNS messages ──────────┤
                       └─ else NOTIFY_TOPIC_ARN, the stack topic from AlertEmail

CloudWatch alarms ───── always the stack topic. They cannot read your budget
                        table, so they can never be routed per track.
```

> **Operating rule: do not set `notify_topic_arn` on any budget row yet.**
> Publishing to a topic with no subscribers *succeeds silently*, so a typo there
> loses alerts with no error anywhere. Leave every row without it and everything
> lands on the stack topic.

### The ten emails

| # | Subject | Sent by | Fires when | How often, and what stops the repeat |
|---|---|---|---|---|
| 1 | `<track> at <pct>% of daily budget` | meter | spend reaches an `alert_pcts` threshold | **once per threshold, per track, per day** — the `alerted` number set on the day row |
| 2 | `DISABLED <track> - runaway spend` | meter | past the ceiling **and** `enforce: true` **and** a key was actually switched off | **once**, until someone re-enables — the `DISABLED` row |
| 3 | `also DISABLED <user> on <track>` | meter | a second IAM user started spending on an already-disabled track, so its key was switched off too | **once per user per day** — `NOTICE#DISABLE_EXTENDED#<user>` |
| 4 | `RE-ENABLED <track>` | unblock | a human ran `make unblock` | once per release — only possible while a `DISABLED` row exists |
| 5 | `<track> has no budget row` | meter | neither `TRACK#<track>` nor `DEFAULT` exists, so the $10 floor applied | **once per track per day** — `NOTICE#NO_BUDGET` |
| 6 | `unpriced model used by <track>` | meter | the model is not on the rate card | **once per track per model per day** — `NOTICE#FALLBACK#<model id>` |
| 7 | `cannot stop <track> - no access key` | meter | past the ceiling, enforcing, but the principal is a role or has no active key | **once per track per day** — `NOTICE#UNENFORCEABLE` |
| 8 | `rate card has not refreshed in weeks` | meter | the card's `fetched_at` is over 30 days old | **once per day, for the whole account** — `TRACK#_SYSTEM` + `NOTICE#RATECARD_STALE` |
| 9 | `rate card REJECTED - prices not updated` | refresh | fetched prices failed validation. Nothing was written | **once per day** — `_SYSTEM` + `NOTICE#RATECARD_REJECTED` |
| 10 | `rate card refreshed with gaps` | refresh | prices saved, but a model name had no `MODEL_IDS` line, or a cache rate was assumed | **once per day** — `_SYSTEM` + `NOTICE#RATECARD_GAPS` |

### Why "once per day" is enforced in the database, not in code

A Lambda invoked by CloudWatch Logs or by a schedule is retried **twice**
automatically, and up to ten meter invocations run at the same time. A counter
in memory would not survive either. So before sending, the code writes a row:

```
pk = TRACK#camunda_to_temporal
sk = DAY#2026-10-15#NOTICE#NO_BUDGET
```

with `ConditionExpression: attribute_not_exists(pk)`. Exactly one caller wins;
everyone else gets a conditional-check failure and sends nothing. That is what
`ledger.claim_notice` does.

`_SYSTEM` is not a real track. It is the partition used for problems that belong
to the whole account rather than to one workload, so you get one email instead
of one per track.

> **A known rough edge in alert 7.** The stale-card alert is sent from the meter
> while it is processing one particular track, so it goes to *that track's*
> topic. Which track wins the daily claim is whichever one happens to be
> spending first, so the topic is not deterministic. It does not matter today,
> because no budget row should name its own topic — everything lands on the
> stack topic. It would matter if you ever started routing per track. Alerts 8
> and 9 come from the refresh Lambda, which has no track in hand, so those
> always use the stack topic.

### The three alarms

| Alarm | Raised when | What it means |
|---|---|---|
| `bedrock-cost-limits-meter-errors` | the meter returned an error in a 5-minute window | something failed; AWS is retrying. Usually fine on its own |
| `bedrock-cost-limits-meter-dlq-not-empty` | a batch landed in the dead-letter queue | a batch **gave up**. That spend is **not in the ledger**, so every affected budget is reading low until it is replayed |
| `bedrock-cost-limits-rate-refresh-errors` | the weekly price refresh returned an error | prices did not update. Nothing stops — the meter keeps using the card it has — but budgets drift from real AWS prices until it is fixed |

The second one is the serious one. The first means "retrying"; the second means
"stopped trying".

### Alert bodies

Every message body is JSON. The common fields:

```json
{
  "track": "camunda_to_temporal",
  "day": "2026-10-15",
  "threshold_pct": 80,
  "actual_pct": "92.9",
  "spent_usd": "0.1858",
  "daily_budget_usd": "0.2000",
  "disable_at_usd": "0.3000",
  "budget_scope": "TRACK#camunda_to_temporal",
  "enforce": true
}
```

`threshold_pct` is the line that was crossed; `actual_pct` is where spend
actually is. The gap between them is the signal — 81% is a nudge, 140% means the
disable is next.

There is deliberately **no `owner` or `env`** field. Only the `Track` tag is read
from IAM; nothing else is cached or carried.

### What is NOT alerted

| Situation | Why not |
|---|---|
| a clean weekly price refresh | a log line, not an email |
| prices unchanged week to week | nothing happened |
| a duplicate log line | normal; it is deduplicated and the reaction still runs |
| a single failed log line | the batch raises, the meter retries, the alarm covers it |
| spend below the lowest threshold | nothing to say |

---

## Part 2 — Expiry (TTL)

### How DynamoDB TTL actually works

Three things worth knowing before the tables:

1. **TTL is configured per table, on one named attribute.** Here that attribute
   is `ttl`, holding a Unix timestamp in seconds.
2. **Deletion is lazy.** AWS deletes expired items "typically within 48 hours"
   of expiry, not at the instant. An expired item can still be read until then,
   so nothing may depend on an item being gone the moment it expires.
3. **TTL deletes are free** — they do not consume write capacity.

### `bedrock_usage` — TTL **enabled** on `ttl`

The meter's own records. Keys are `pk` + `sk`.

| `pk` | `sk` | TTL set? | Lifetime | Why that number |
|---|---|---|---|---|
| `REQ#<requestId>` | `SEEN` | yes | **48 hours** | The dedup lock. It only has to outlive the retry window — CloudWatch retries twice and the DLQ holds 14 days, but a replay older than two days is a deliberate human act, not an accidental redelivery. Keeping these forever would be the largest thing in the table by far. |
| `TRACK#<track>` | `DAY#<date>` | yes | **60 days** | The day's running total, the per-model and per-principal breakdowns, and `user_principals` — the set of IAM users on this track, which the disable path works from. Two months of history is enough to answer "what happened last month" without the table growing without limit. |
| `TRACK#<track>` | `DAY#<date>#NOTICE#<kind>` | yes | **60 days** | The "already emailed this" markers. Matched to the day row so history is one consistent window. Only the first day matters functionally; the rest is audit. |
| `TRACK#<track>` | `DISABLED` | **NO** | **forever** | Deliberate. This row is the only record that a key was switched off and why. If it expired, you would be left with an inactive access key and nothing explaining it — and the next runaway would find no claim and page again. It is removed explicitly by `make unblock`, never by time. |
| `ARN#<arn>` | `TAGS` | yes | **24 hours** | The `Track` tag cache, so IAM is not asked on every log line. 24 hours is the upper bound on how long a re-tagged key keeps its old attribution. |

> **The one real trap here: replaying a DLQ batch after two days double-counts.**
> The dead-letter queue holds a failed batch for **14 days**, but the dedup rows
> that stop a line being counted twice expire after **48 hours** (plus up to 48
> more of lazy deletion). So replaying a batch that is more than about four days
> old will add that spend to the ledger a second time, because nothing remembers
> it was already counted. Replay promptly, or accept that the day's total for
> the affected tracks will be overstated. Those day rows live 60 days, so the
> correction has to be made by hand.

A detail on the `DISABLED` row: it stores its date as `disabled_day`, **not** as
`day`. The `day` attribute is the partition key of the sparse index `GSI1`, and
since this row never expires, storing the date under `day` would leave it in
that index forever and pollute every "today's spend" query.

### `bedrock_budgets` — TTL **not enabled at all**

Configuration and prices. Key is `scope`, no sort key.

| `scope` | TTL? | Removed by | Why |
|---|---|---|---|
| `DEFAULT` | no | you | configuration must not evaporate |
| `TRACK#<track>` | no | you | same. A budget that expired would silently drop the track to the $10 floor |
| `RATECARD#CURRENT` | no | overwritten weekly | if it expired, every model would price at the fallback — about 6.7× — for no reason |
| `RATECARD#<date>` | no | **deleted explicitly** by the next refresh | the rotation keeps exactly two rows, so TTL is not needed and would be less predictable |

Because this table has **no TTL specification at all**, adding a `ttl` attribute
to a row here would do nothing. That is intentional: there is no row in this
table that should ever disappear by itself.

### In-process caches — not DynamoDB, easy to confuse

These live in Lambda memory and vanish when the container is recycled. They are
not rows and cannot be inspected.

| Cache | Lifetime | What it holds | Where |
|---|---|---|---|
| tag memo | **15 minutes** | ARN → track | `identity._memo_cache` |
| untagged memo | **60 seconds** | ARNs known to have no `Track` tag | `identity._untagged_cache` |
| rate card memo | **15 minutes** | the whole price card | `ratecard._memo_cache` |

The 60-second one is short on purpose: an untagged principal raises on every log
line it produces, and without this cache that would be one `ListUserTags` call
per line, which throttles IAM for everything else in the account. It is short
because the fix is someone adding a tag, and you want that picked up quickly.

The two 15-minute ones answer a different question — how stale may this be?
Tags change rarely; prices change weekly. Fifteen minutes is far fresher than
either, and it takes a per-call database read down to roughly one per container.

### All the numbers in one place

| Constant | Value | File |
|---|---|---|
| `_DEDUP_TTL_SECONDS` | 48 hours | `ledger.py` |
| `_ROW_TTL_SECONDS` | 60 days | `ledger.py` |
| `_ITEM_TTL_SECONDS` | 24 hours | `identity.py` |
| `_MEMO_TTL_SECONDS` | 15 minutes | `identity.py` |
| `_NEGATIVE_TTL_SECONDS` | 60 seconds | `identity.py` |
| `_MEMO_TTL_SECONDS` | 15 minutes | `ratecard.py` |
| `STALE_AFTER_DAYS` | 30 days | `ratecard.py` |
| DLQ `MessageRetentionPeriod` | 14 days | `template.yaml` |
| refresh schedule | `rate(7 days)` | `template.yaml` |

---

## Quick reference: an email arrived, what do I do?

| Email | First thing to check |
|---|---|
| `at 80% of daily budget` | expected. Is the budget right for this track? |
| `at 100% of daily budget` | the ceiling is next. Is something looping? |
| `DISABLED <track>` | `by_model` and `by_principal` on the day row — one model with thousands of calls means a loop |
| `has no budget row` | `make seed-track TRACK=<name> DAILY_USD=<n>` |
| `unpriced model used by` | `make rates`. Empty → run `make refresh-rates`. Not empty → add a `MODEL_IDS` line |
| `cannot stop <track>` | that track runs under a role. Enforcement cannot bite; stop the workload directly |
| `rate card has not refreshed in weeks` | the refresh Lambda's logs — it has stopped being triggered or keeps failing |
| `rate card REJECTED` | the `stored` vs `fetched` numbers in the email body |
| `rate card refreshed with gaps` | add the named model to `pricelist.MODEL_IDS` |
| `also DISABLED <user> on <track>` | expected if several IAM users share that track's tag. Nothing to do unless it is a surprise |
| `meter-dlq-not-empty` alarm | **urgent.** Spend is missing from the ledger until the batch is replayed |
