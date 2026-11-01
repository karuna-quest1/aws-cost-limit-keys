# Going to production — the checklist

Copy-paste, verify, move on. `README.md` explains *why* each piece exists; this
file is the order of operations and the command for each one.

Nothing here deactivates a key until the very last step. `enforce` defaults to
`false`, so everything before step 10 only watches and emails.

---

## Before you start

| | |
| --- | --- |
| AWS CLI | configured, with permission to create IAM roles, Lambdas, DynamoDB tables, SNS topics |
| SAM CLI | `sam --version` |
| Python | 3.12 for the Lambdas; the tests need only the standard library |
| Region | pick ONE. Bedrock invocation logging is per region and this stack watches one log group |

```bash
aws sts get-caller-identity          # right account?
aws configure get region             # right region?
sam --version
```

---

## The ten steps

```
 1. make test                     prove it works, no AWS needed
 2. decide the blast radius        ⚠️ read this before deploying
 3. sam deploy                     create everything
 4. confirm the alert emails       a human clicks a link, per address
 5. tag the IAM users              ⚠️ MUST be before step 7
 6. create the log group + role
 7. enable invocation logging      spend starts being seen here
 8. seed the budget table
 9. make refresh-rates             ⚠️ not optional
10. watch a week, then enforce     one track at a time
```

---

### 1. Prove it works locally

```bash
make test
```

Expect `OK` and 126 tests. No AWS account, no credentials, no `pip install`.
If this fails, stop — nothing below will behave.

---

### 2. Decide the blast radius  ⚠️ before you deploy

Three decisions that are painful to change later.

**`PrincipalNamePattern`** — which IAM users this may read tags from and
**deactivate keys on**. Default `BedrockDev-*`. If you set it to `*`, a bug can
disable any identity in the account, including yours. Use a prefix only your
Bedrock keys have.

**`RateRegion`** — whose prices you are charged at. Must match the region you
deploy in, unless you know otherwise.

**Deletion protection.** The two DynamoDB tables hold your budgets and 60 days
of spend history, and the template does **not** retain them on stack deletion.
Add this to both tables in `template.yaml` before the first deploy:

```yaml
  UsageTable:
    Type: AWS::DynamoDB::Table
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
```

`UpdateReplacePolicy` matters as much as `DeletionPolicy`: changing `TableName`
later would otherwise destroy and recreate the table. See finding 3 in
`PRODUCTION_REVIEW.md`.

---

### 3. Deploy

```bash
make deploy          # = sam build && sam deploy --guided
```

Answers:

| Prompt | Enter |
| --- | --- |
| Stack Name | `bedrock-cost-limits` |
| AWS Region | the one region your tracks call Bedrock in |
| `BedrockLogGroupName` | Enter for `/aws/bedrock/modelinvocations` |
| `PrincipalNamePattern` | e.g. `BedrockDev-*` — from step 2 |
| `AlertEmail` | one address; more are added in step 4 |
| `RateRegion` | Enter for `us-east-1`, or your region |
| `MaxRateDeltaPct` | Enter for `25` |
| Confirm changes before deploy | `y` |
| Allow SAM CLI IAM role creation | `Y` |
| Save arguments to configuration file | `Y` |

**Verify** — eleven resources and six outputs:

```bash
aws cloudformation describe-stacks --stack-name bedrock-cost-limits \
  --query 'Stacks[0].StackStatus' --output text          # CREATE_COMPLETE

aws cloudformation describe-stacks --stack-name bedrock-cost-limits \
  --query 'Stacks[0].Outputs' --output table
```

Keep the topic ARN handy:

```bash
TOPIC=$(aws cloudformation describe-stacks --stack-name bedrock-cost-limits \
  --query 'Stacks[0].Outputs[?OutputKey==`AlertTopicArn`].OutputValue' --output text)
echo "$TOPIC"
```

---

### 4. Subscribe everyone who should see the alerts

Every subscriber receives **every** alert — the 80% and 100% budget warnings,
disables, re-enables, price-refresh notices and all three alarms. There is no
per-alert filtering.

```bash
aws sns subscribe --topic-arn "$TOPIC" --protocol email --endpoint alice@company.com
aws sns subscribe --topic-arn "$TOPIC" --protocol email --endpoint bob@company.com
```

A distribution list is usually better than several individuals — one
subscription, membership managed by your mail system, no re-confirming when
someone joins:

```bash
aws sns subscribe --topic-arn "$TOPIC" --protocol email --endpoint bedrock-alerts@company.com
```

**Every address must click the confirmation link.** Until then it receives
nothing.

**Verify** — no `PendingConfirmation` left:

```bash
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" \
  --query 'Subscriptions[].{endpoint:Endpoint,arn:SubscriptionArn}' --output table
```

A real ARN means confirmed. The literal string `PendingConfirmation` means
somebody still has an unread email.

**Send yourself a test** so you know the path works end to end:

```bash
aws sns publish --topic-arn "$TOPIC" \
  --subject "[Bedrock] test - ignore" --message "wiring check"
```

Add more addresses at any time. No redeploy, no restart.

---

### 5. Tag the IAM users  ⚠️ before step 7

`Track` is the only tag read. It is the budget boundary, the ledger key, and
the thing a key gets deactivated for. **An untagged user raises on every log
line it produces**, which fires the error alarm continuously and fills the DLQ
with batches that can never succeed.

```bash
aws iam tag-user --user-name BedrockDev-camunda-to-temporal \
  --tags Key=Track,Value=camunda_to_temporal
```

Give each of these users **one access key, used only for Bedrock**. Enforcement
works at the granularity of the IAM *user*, not the key — the invocation log
carries an ARN and no key id — so every active key that user holds is switched
off together. A key also doing S3 work stops with it.

**Verify** — nothing untagged:

```bash
aws iam list-users --query 'Users[?starts_with(UserName, `BedrockDev-`)].UserName' \
  --output text | tr '\t' '\n' | while read -r u; do
    track=$(aws iam list-user-tags --user-name "$u" \
      --query 'Tags[?Key==`Track`].Value' --output text)
    printf '%-45s %s\n' "$u" "${track:-*** NO TRACK TAG ***}"
done
```

Fix every `NO TRACK TAG` before step 7.

---

### 6. Create the log group and the logging role

Neither is in the template: enabling invocation logging is not a CloudFormation
resource.

```bash
# the log group, with retention so it does not grow forever
aws logs create-log-group --log-group-name /aws/bedrock/modelinvocations
aws logs put-retention-policy --log-group-name /aws/bedrock/modelinvocations \
  --retention-in-days 30

# a role Bedrock can assume to write to it
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

**⚠️ Then check who else can write to that log group.** It is the trust boundary
for the whole control: anything that can write to it can claim any identity and
any figure, and the meter cannot tell a forged line from a real one. A broad
`logs:*` on `Resource: "*"` — common in CI roles and admin policies — is write
access here. See the threat model in `README.md`; the reliable control is an SCP
denying `logs:PutLogEvents` on this log group for everything except
`bedrock.amazonaws.com`.

---

### 7. Enable invocation logging — spend starts being seen

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

aws bedrock put-model-invocation-logging-configuration \
  --logging-config "{
    \"cloudWatchConfig\": {
      \"logGroupName\": \"/aws/bedrock/modelinvocations\",
      \"roleArn\": \"arn:aws:iam::${ACCOUNT}:role/BedrockInvocationLoggingRole\"
    },
    \"textDataDeliveryEnabled\": false,
    \"imageDataDeliveryEnabled\": false,
    \"embeddingDataDeliveryEnabled\": false
  }"
```

**Every data-delivery flag must be `false`.** That records token counts without
prompt or response text. Nothing in this system needs the content, and logging
it would put customer data in CloudWatch.

**Verify:**

```bash
aws bedrock get-model-invocation-logging-configuration

# make one Bedrock call with a tagged key, then:
aws logs tail /aws/bedrock/modelinvocations --since 5m
aws logs tail /aws/lambda/bedrock-cost-limits-meter --since 5m | grep batch
```

A healthy meter logs one line per batch:

```
batch {'seen': 1, 'counted': 1, 'duplicate': 0, 'failed': 0}
```

`counted: 0` with `seen` above zero means it is running and recording nothing —
stop and read the error above it.

---

### 8. Seed the budget table

```bash
# the DEFAULT row. Every TRACK# row inherits any field it does not set.
make seed DAILY_USD=25

# one row per track
make seed-track TRACK=camunda_to_temporal DAILY_USD=50
make seed-track TRACK=memcached_to_elasticache DAILY_USD=30
```

Both write `enforce: false`. Without any row a track falls to a hardcoded $10
floor and emails you daily about it.

**Verify:**

```bash
aws dynamodb scan --table-name bedrock_budgets \
  --query 'Items[].{scope:scope.S,daily:daily_usd.N,enforce:enforce.BOOL}' --output table
```

---

### 9. Fetch the prices  ⚠️ not optional

```bash
make refresh-rates
make rates
```

**There are no prices in the code.** Until this runs, every model prices at the
fallback rate — about 6.7× Sonnet — so budgets trip early and you get a daily
"unpriced model" email. Nothing is ever under-counted, but the numbers are
wrong.

After this a Lambda refreshes them every 7 days on its own. The schedule's first
firing is seven days after deploy, which is why this step exists.

**Verify** — `make rates` must print a `models` map. An empty result means no
card at all and everything is at the fallback rate.

If you get a "rate card refreshed with gaps" email, a model name needs one line
in `pricelist.MODEL_IDS`. Allow 15 minutes for the meter's in-memory cache to
pick up the new card.

---

### 10. Watch a week, then enforce one track at a time

Leave `enforce: false` for a full week. Compare what the meter recorded against
Cost Explorer for the same period:

```bash
make status                      # today, every track
make status DAY=2026-11-01       # a past day
```

Then look at the day row for a track that was busy, to sanity-check the
breakdown:

```bash
aws dynamodb get-item --table-name bedrock_usage \
  --key '{"pk":{"S":"TRACK#camunda_to_temporal"},"sk":{"S":"DAY#2026-11-01"}}' \
  --query 'Item.{spent:spent_micros.N,calls:calls.N,by_model:by_model.M,by_principal:by_principal.M}'
```

`spent_micros` is millionths of a dollar: `309750` is `$0.3098`.

**Before arming anything**, confirm these are true:

- [ ] every `BedrockDev-*` user has a `Track` tag (step 5)
- [ ] the metered totals match Cost Explorer to your satisfaction
- [ ] `make rates` returns a real card, not empty
- [ ] all alert subscriptions are confirmed, and you have received a real alert
- [ ] a regional guardrail is in place, or you accept that calls in other
      regions are invisible (finding 1, `PRODUCTION_REVIEW.md`)
- [ ] `logs:PutLogEvents` on the invocation log group has been audited (step 6)

Then arm **one** track:

```bash
make enforce TRACK=camunda_to_temporal
```

And to disarm at any time:

```bash
make watch TRACK=camunda_to_temporal
```

Watch that one track for a few days before arming the next.

---

## When a track gets disabled

Three steps, in order. `unblock` **refuses a reason under 10 characters**, so the
diagnosis is enforced rather than suggested.

```bash
# 1. what is disabled
aws dynamodb scan --table-name bedrock_usage \
  --filter-expression 'sk = :s' \
  --expression-attribute-values '{":s":{"S":"DISABLED"}}' \
  --query 'Items[].{track:pk.S,at:disabled_at.S,spend:spend_at_disable.N}' --output table

# 2. what spent the money
aws dynamodb get-item --table-name bedrock_usage \
  --key '{"pk":{"S":"TRACK#camunda_to_temporal"},"sk":{"S":"DAY#2026-11-01"}}' \
  --query 'Item.{by_model:by_model.M,by_principal:by_principal.M,calls:calls.N}'

# 3. fix the cause, then re-enable
make unblock TRACK=camunda_to_temporal REASON='wave-7f3a21 loop fixed, retry cap added'
```

One model holding nearly all the spend with `calls` in the thousands is a loop.
That is your reason.

**Re-enabling does not reset the day's spend.** The track is still over its
ceiling, so if the cause has not stopped the next call switches the keys
straight back off. For genuine headroom, raise the budget instead:
`make seed-track TRACK=camunda_to_temporal DAILY_USD=100`.

---

## Rolling back

Each step is reversible on its own.

| Undo | Command |
| --- | --- |
| disarm one track | `make watch TRACK=<name>` |
| stop all metering, keep the data | `aws bedrock delete-model-invocation-logging-configuration` |
| re-enable a disabled track | `make unblock TRACK=<name> REASON='...'` |
| remove the stack | `aws cloudformation delete-stack --stack-name bedrock-cost-limits` |

**Before deleting the stack, re-enable every disabled track.** Otherwise its
keys stay inactive and the record explaining why goes with the table. And unless
you added `DeletionPolicy: Retain` in step 2, deleting the stack **destroys your
budgets and 60 days of spend history**.

---

## Known gaps to accept or close first

From `PRODUCTION_REVIEW.md` and `review.md`. None of these block metering; two
of them should be closed before you rely on enforcement.

| | Gap | Status |
| --- | --- | --- |
| 1 | **Bedrock calls in another region are unmetered.** No alert, indistinguishable from idle | close with a regional deny policy |
| 2 | **A forged log entry can get another team's keys disabled.** The meter cannot verify entries | close by auditing `logs:PutLogEvents` (step 6) |
| 3 | Stack deletion destroys the tables | close in step 2 |
| 4 | Spend is dated when the meter ran, not when the call happened | matters mainly on a DLQ replay |
| 5 | A non-ASCII or over-long `Track` name breaks that track's alerting permanently | keep names `[A-Za-z0-9._-]` |
| 6 | A throttled meter has no alarm | watch the `Throttles` metric manually |
| 7 | A mislabelled batch SKU could reach the rate card | accepted; `make rates` after the first refresh |

---

## Day to day

```bash
make status                                   # today's spend, every track
make rates                                    # the prices in use
make refresh-rates                            # fetch prices now
make seed-track TRACK=<name> DAILY_USD=<n>     # add or change a budget
make enforce TRACK=<name>                      # arm one track
make watch   TRACK=<name>                      # disarm one track
make unblock TRACK=<name> REASON='...'         # release a disabled track
make test                                      # after any code change
```

Health check:

```bash
aws logs tail /aws/lambda/bedrock-cost-limits-meter --follow | grep batch

DLQ=$(aws cloudformation describe-stacks --stack-name bedrock-cost-limits \
  --query 'Stacks[0].Outputs[?OutputKey==`MeterDeadLetterQueueUrl`].OutputValue' --output text)
aws sqs get-queue-attributes --queue-url "$DLQ" \
  --attribute-names ApproximateNumberOfMessagesVisible
```

A non-empty DLQ is the one to act on: that spend is **not in the ledger**, so
every affected budget is reading low until the batch is replayed. Replay within
about four days — after that the dedup rows have expired and a replay
double-counts.
