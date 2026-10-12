.PHONY: test deploy seed seed-track enforce watch status unblock

BUDGET_TABLE     ?= bedrock_budgets
USAGE_TABLE      ?= bedrock_usage
DAILY_USD        ?= 25
RUNAWAY_MULTIPLE ?= 1.5
DAY              ?= $(shell date -u +%F)

test:
	python3 -m unittest discover -s tests -t tests -v

deploy:
	sam build && sam deploy --guided

# The DEFAULT row. Every TRACK# row inherits any field it does not set from
# this one, so this is the only place a number has to be kept current.
#
# enforce stays false until the rate card has been reconciled against Cost
# Explorer -- see README.md, the watch-only week.
seed:
	aws dynamodb put-item --table-name $(BUDGET_TABLE) --item '{ \
	  "scope":{"S":"DEFAULT"}, "daily_usd":{"N":"$(DAILY_USD)"}, \
	  "runaway_multiple":{"N":"$(RUNAWAY_MULTIPLE)"}, "alert_pcts":{"NS":["80","100"]}, \
	  "enforce":{"BOOL":false} }'

# One track, in watch-only mode:  make seed-track TRACK=camunda_to_temporal DAILY_USD=50
#
# This target exists because hand-writing the row is how the budget used to get
# silently reduced: a row carrying only `daily_usd` inherits everything else from
# DEFAULT now, but it is still easier to not hand-write it at all.
seed-track:
	@test -n "$(TRACK)" || (echo "TRACK is required: make seed-track TRACK=<name> [DAILY_USD=50]"; exit 1)
	aws dynamodb put-item --table-name $(BUDGET_TABLE) --item '{ \
	  "scope":{"S":"TRACK#$(TRACK)"}, "daily_usd":{"N":"$(DAILY_USD)"}, \
	  "runaway_multiple":{"N":"$(RUNAWAY_MULTIPLE)"}, "alert_pcts":{"NS":["80","100"]}, \
	  "enforce":{"BOOL":false}, \
	  "updated_at":{"S":"$(shell date -u +%FT%TZ)"} }'

# Turn enforcement on for one track. The only step that lets this control
# deactivate anything, and deliberately a separate, explicit command.
enforce:
	@test -n "$(TRACK)" || (echo "TRACK is required: make enforce TRACK=<name>"; exit 1)
	aws dynamodb update-item --table-name $(BUDGET_TABLE) \
	  --key '{"scope":{"S":"TRACK#$(TRACK)"}}' \
	  --update-expression 'SET #enforce = :on, updated_at = :now' \
	  --condition-expression 'attribute_exists(scope)' \
	  --expression-attribute-names '{"#enforce":"enforce"}' \
	  --expression-attribute-values '{":on":{"BOOL":true},":now":{"S":"$(shell date -u +%FT%TZ)"}}'

watch:
	@test -n "$(TRACK)" || (echo "TRACK is required: make watch TRACK=<name>"; exit 1)
	aws dynamodb update-item --table-name $(BUDGET_TABLE) \
	  --key '{"scope":{"S":"TRACK#$(TRACK)"}}' \
	  --update-expression 'SET #enforce = :off, updated_at = :now' \
	  --expression-attribute-names '{"#enforce":"enforce"}' \
	  --expression-attribute-values '{":off":{"BOOL":false},":now":{"S":"$(shell date -u +%FT%TZ)"}}'

# Today's spend for every track, from the sparse index. One query, no scan.
status:
	aws dynamodb query --table-name $(USAGE_TABLE) --index-name GSI1 \
	  --key-condition-expression '#day = :day' \
	  --expression-attribute-names '{"#day":"day"}' \
	  --expression-attribute-values '{":day":{"S":"$(DAY)"}}' \
	  --query 'Items[].{track:pk.S,spent_micros:spent_micros.N,calls:calls.N}' --output table

unblock:
	@test -n "$(TRACK)" || (echo "TRACK is required: make unblock TRACK=<name> REASON='...'"; exit 1)
	@test -n "$(REASON)" || (echo "REASON is required and must say what you found"; exit 1)
	aws lambda invoke --function-name bedrock-cost-limits-unblock \
	  --payload '{"track":"$(TRACK)","reason":"$(REASON)"}' --cli-binary-format raw-in-base64-out \
	  /dev/stdout
