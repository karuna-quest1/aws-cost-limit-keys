"""The three things a review will ask to see run, plus the edges around them.

    python3 -m unittest discover -s tests -t . -v

Two groups of tests matter more than the happy paths:

  * `TestLedgerAgainstRealDynamoSemantics` -- the first write of a day used to
    fail in production and pass here. These assert against what DynamoDB does.
  * `TestFailuresRaise` -- every AWS call that used to be swallowed. Each case
    is a defect that previously produced wrong behaviour with no error raised.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import budgets  # noqa: E402
import enforce  # noqa: E402
import errors  # noqa: E402
import handler  # noqa: E402
import identity  # noqa: E402
import ledger  # noqa: E402
import pricelist  # noqa: E402
import pricing  # noqa: E402
import ratecard  # noqa: E402
import refresh_rates  # noqa: E402
import unblock  # noqa: E402
from events import (  # noqa: E402
    ARN_ASSUMED,
    ARN_USER,
    SONNET,
    TRACK,
    USER_NAME,
    cloudwatch_event,
    log_entry,
    price_list_product,
    sonnet_price_list,
)
from fakes import FakeIAM, FakePricing, FakeSNS, FakeTable  # noqa: E402

TOPIC = "arn:aws:sns:us-east-1:123456789012:bedrock-alerts"

SONNET_ID = "anthropic.claude-sonnet-4-5-20250929-v1:0"
FRESH = "2026-10-14T03:00:00+00:00"


def rates(input_="3", output="15", cache_read="0.3", cache_write="3.75"):
    return pricing.Rates(
        input=Decimal(input_),
        output=Decimal(output),
        cache_read=Decimal(cache_read),
        cache_write=Decimal(cache_write),
    )


def rate_card_item(models=None, fetched_at=FRESH, **extra):
    """The RATECARD#CURRENT row. Sonnet at the published prices unless told otherwise."""
    return {
        "scope": ratecard.CURRENT_SCOPE,
        **ratecard.item_from_rates(
            {SONNET_ID: rates()} if models is None else models,
            fetched_at=fetched_at,
            source="AmazonBedrock:us-east-1",
            region="us-east-1",
        ),
        **extra,
    }


def budget_table(daily_usd="50", enforce_flag=False, multiple="1.5", scope=f"TRACK#{TRACK}",
                 card=True):
    items = {
        (scope,): {
            "scope": scope,
            "daily_usd": Decimal(daily_usd),
            "runaway_multiple": Decimal(multiple),
            "alert_pcts": {80, 100},
            "notify_topic_arn": TOPIC,
            "enforce": enforce_flag,
        }
    }
    # There is no committed card any more, so the meter has to read one from the
    # table or everything prices at FALLBACK.
    if card:
        items[(ratecard.CURRENT_SCOPE,)] = rate_card_item()
    return FakeTable(items=items, key_names=("scope",))


def wiring(daily_usd="50", enforce_flag=False, tags=None, **iam_kwargs):
    identity.reset_cache()
    ratecard.reset_cache()
    return {
        "usage": FakeTable(),
        "budgets": budget_table(daily_usd, enforce_flag),
        "iam": FakeIAM(
            tags={USER_NAME: tags if tags is not None else {"Track": TRACK}},
            access_keys={USER_NAME: ["AKIAEXAMPLE1", "AKIAEXAMPLE2"]},
            **iam_kwargs,
        ),
        "sns": FakeSNS(),
    }


def day_row(aws_clients, track=TRACK):
    return aws_clients["usage"].items[(f"TRACK#{track}", f"DAY#{ledger.utc_day()}")]


class WiredTestCase(unittest.TestCase):
    """Replaces handler.clients() with the fakes for the duration of a test."""

    def setUp(self):
        self._aws_clients = None
        self._real_clients = handler.clients
        handler._clients.clear()
        handler.clients = lambda: self._aws_clients
        # The deployed function always carries a topic, from the template. It is
        # what makes the no-budget alert deliverable at all: a budget row that
        # does not exist cannot name a topic, so without this default the alert
        # announcing the missing budget has nowhere to go.
        self._real_topic = handler.NOTIFY_TOPIC_ARN
        handler.NOTIFY_TOPIC_ARN = TOPIC
        identity.reset_cache()
        ratecard.reset_cache()

    def tearDown(self):
        handler.clients = self._real_clients
        handler.NOTIFY_TOPIC_ARN = self._real_topic
        handler._clients.clear()
        ratecard.reset_cache()


# --------------------------------------------------------------------------
# 1. A fake log line goes in, the right dollar amount comes out.
# --------------------------------------------------------------------------
class TestPricing(unittest.TestCase):
    def test_worked_example_from_the_readme(self):
        usage = pricing.usage_from_log(log_entry())
        self.assertEqual(usage, pricing.Usage(12_400, 850, 40_000, 0))
        # 12400*3 + 850*15 + 40000*0.30 = 37200 + 12750 + 12000 micro-USD
        self.assertEqual(pricing.price(usage, SONNET, rates()).micros, 61_950)

    def test_cache_reads_are_not_billed_as_input(self):
        """The whole reason the card carries four rates and not two."""
        as_input = pricing.price(pricing.Usage(40_000, 0, 0, 0), SONNET, rates()).micros
        as_cache = pricing.price(pricing.Usage(0, 0, 40_000, 0), SONNET, rates()).micros
        self.assertEqual(as_input, 120_000)
        self.assertEqual(as_cache, 12_000)  # 10x cheaper; collapsing them overstates badly

    def test_geography_prefix_resolves_to_the_same_card_entry(self):
        """`us.` and the bare id must find the same row, or one of them is FALLBACK."""
        card = ratecard.RateCard({SONNET_ID: rates()}, FRESH, False)
        self.assertEqual(card.rate_for(SONNET), card.rate_for(SONNET_ID))
        self.assertIsNotNone(card.rate_for(SONNET))

    def test_a_model_with_no_card_entry_is_expensive_and_flagged_never_free(self):
        priced_usage = pricing.price(pricing.Usage(1000, 0, 0, 0), "meta.llama-not-on-card")
        self.assertTrue(priced_usage.is_fallback)
        self.assertGreater(priced_usage.micros, 0)  # free would mean an unlimited budget

    def test_missing_cache_fields_are_zero_not_an_error(self):
        entry = {"input": {"inputTokenCount": 10}, "output": {"outputTokenCount": 2}}
        self.assertEqual(pricing.usage_from_log(entry), pricing.Usage(10, 2, 0, 0))


# --------------------------------------------------------------------------
# 2. The ledger, against what DynamoDB actually does.
# --------------------------------------------------------------------------
class TestLedgerAgainstRealDynamoSemantics(unittest.TestCase):
    """The nested-map defect: `accumulate` could not write its first row.

    `by_model.#model = ...` is a ValidationException while `by_model` is absent,
    and an UpdateExpression is atomic, so the item was never created and the next
    call failed identically. Forever. The old fake created the parent map for
    the caller and hid it.
    """

    def setUp(self):
        self.table = FakeTable()
        self.usage = pricing.Usage(12_400, 850, 40_000, 0)

    def _accumulate(self, micros=61_950, model_id=SONNET, principal=USER_NAME):
        return ledger.accumulate(
            self.table,
            track=TRACK,
            day="2026-10-06",
            model_id=model_id,
            principal_name=principal,
            usage=self.usage,
            micros=micros,
        )

    def test_the_first_write_of_the_day_succeeds(self):
        self.assertEqual(self._accumulate(), 61_950)
        row = self.table.items[(f"TRACK#{TRACK}", "DAY#2026-10-06")]
        self.assertEqual(int(row["spent_micros"]), 61_950)
        self.assertEqual(row["by_model"], {SONNET: 61_950})
        self.assertEqual(row["by_principal"], {USER_NAME: 61_950})

    def test_the_first_write_costs_one_extra_round_trip_and_no_more(self):
        self._accumulate()
        first = len([call for call in self.table.calls if call[0] == "update_item"])
        self.table.calls.clear()
        self._accumulate()
        second = len([call for call in self.table.calls if call[0] == "update_item"])
        self.assertEqual(first, 3)  # attempt, initialise, retry
        self.assertEqual(second, 1)  # steady state

    def test_running_total_comes_back_from_the_same_write(self):
        self.assertEqual(self._accumulate(100), 100)
        self.assertEqual(self._accumulate(250), 350)
        self.assertEqual(self._accumulate(1), 351)

    def test_breakdowns_accumulate_per_model_and_per_principal(self):
        self._accumulate(100, model_id="a")
        self._accumulate(250, model_id="b")
        self._accumulate(5, model_id="a")
        row = self.table.items[(f"TRACK#{TRACK}", "DAY#2026-10-06")]
        self.assertEqual(row["by_model"], {"a": 105, "b": 250})

    def test_a_row_created_by_a_threshold_claim_is_still_accumulable(self):
        """claim_threshold can create the row with nothing but `alerted` on it."""
        ledger.claim_threshold(self.table, TRACK, "2026-10-06", 80)
        self.assertEqual(self._accumulate(42), 42)

    def test_current_total_reads_without_writing(self):
        self.assertEqual(ledger.current_total(self.table, TRACK, "2026-10-06"), 0)
        self._accumulate(777)
        self.assertEqual(ledger.current_total(self.table, TRACK, "2026-10-06"), 777)

    def test_disabled_record_stays_out_of_the_sparse_index(self):
        ledger.claim_disable(
            self.table,
            track=TRACK,
            day="2026-10-06",
            principal_keys={USER_NAME: {"AKIA1"}},
            spent_micros=1,
        )
        record = self.table.items[(f"TRACK#{TRACK}", "DISABLED")]
        self.assertNotIn("day", record)  # GSI1 is sparse on `day`; this never expires
        self.assertEqual(record["disabled_day"], "2026-10-06")
        self.assertNotIn("ttl", record)
        self.assertEqual(record["keys"], {f"{USER_NAME}/AKIA1"})

    def test_utc_day_honours_epoch_zero(self):
        self.assertEqual(ledger.utc_day(0), "1970-01-01")


# --------------------------------------------------------------------------
# 3. The same log line twice only counts once.
# --------------------------------------------------------------------------
class TestIdempotency(WiredTestCase):
    def test_redelivery_is_not_double_charged(self):
        aws_clients = self._aws_clients = wiring()
        event = cloudwatch_event([log_entry()])

        first = handler.handler(event, None)
        second = handler.handler(event, None)

        self.assertEqual(first["counted"], 1)
        self.assertEqual(second["counted"], 0)
        self.assertEqual(second["duplicate"], 1)

        row = day_row(aws_clients)
        self.assertEqual(int(row["spent_micros"]), 61_950)
        self.assertEqual(int(row["calls"]), 1)

    def test_distinct_requests_accumulate(self):
        aws_clients = self._aws_clients = wiring()
        handler.handler(
            cloudwatch_event([log_entry(request_id="r1"), log_entry(request_id="r2")]), None
        )
        row = day_row(aws_clients)
        self.assertEqual(int(row["calls"]), 2)
        self.assertEqual(int(row["spent_micros"]), 123_900)

    def test_a_duplicate_still_reacts_to_the_current_total(self):
        """Dedup protects the money, not the reaction.

        If the invocation that counted the spend died before disabling, the
        redelivery has to finish the job. Simulated by counting the line with
        enforcement off, then redelivering it with enforcement on.
        """
        aws_clients = self._aws_clients = wiring(daily_usd="0.01", enforce_flag=False)
        handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertEqual(aws_clients["iam"].deactivated, [])

        aws_clients["budgets"] = budget_table("0.01", enforce_flag=True)
        stats = handler.handler(cloudwatch_event([log_entry()]), None)

        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(len(aws_clients["iam"].deactivated), 2)
        self.assertEqual(int(day_row(aws_clients)["calls"]), 1)  # and it did not re-count


# --------------------------------------------------------------------------
# 4. The runaway path fires at the right threshold, and enforce=false
#    means nothing is actually disabled.
# --------------------------------------------------------------------------
class TestEnforcement(WiredTestCase):
    def test_threshold_arithmetic(self):
        budget = budgets.load(budget_table(), TRACK)
        self.assertEqual(budget.daily_micros, 50_000_000)
        self.assertEqual(budget.runaway_micros, 75_000_000)  # 1.5x
        self.assertEqual(enforce.evaluate(39_000_000, budget).crossed_pcts, ())
        self.assertEqual(enforce.evaluate(40_000_000, budget).crossed_pcts, (80,))
        self.assertEqual(enforce.evaluate(50_000_000, budget).crossed_pcts, (80, 100))
        self.assertFalse(enforce.evaluate(74_999_999, budget).is_runaway)
        self.assertTrue(enforce.evaluate(75_000_000, budget).is_runaway)

    def test_absent_budget_row_is_never_unlimited(self):
        budget = budgets.load(FakeTable(key_names=("scope",)), "brand-new")
        self.assertEqual(budget.daily_micros, int(budgets.FLOOR_DAILY_USD * 1_000_000))
        self.assertTrue(budget.is_floor)
        self.assertFalse(budget.enforce)  # and watch-only until someone opts in

    def test_watch_only_alerts_but_disables_nothing(self):
        aws_clients = self._run_until_runaway(enforce_flag=False)
        self.assertEqual(aws_clients["iam"].deactivated, [])
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), aws_clients["usage"].items)
        self.assertTrue(
            any("100%" in alert["Subject"] for alert in aws_clients["sns"].published)
        )

    def test_enforcing_deactivates_keys_and_records_why(self):
        aws_clients = self._run_until_runaway(enforce_flag=True)
        self.assertEqual(
            sorted(aws_clients["iam"].deactivated),
            [(USER_NAME, "AKIAEXAMPLE1"), (USER_NAME, "AKIAEXAMPLE2")],
        )
        record = aws_clients["usage"].items[(f"TRACK#{TRACK}", "DISABLED")]
        self.assertEqual(record["principals"], {USER_NAME})
        self.assertEqual(
            record["keys"], {f"{USER_NAME}/AKIAEXAMPLE1", f"{USER_NAME}/AKIAEXAMPLE2"}
        )
        self.assertNotIn("ttl", record)  # must not expire and strand a dead key
        self.assertTrue(
            any("DISABLED" in alert["Subject"] for alert in aws_clients["sns"].published)
        )

    def test_each_threshold_emails_once_per_day(self):
        # $0.06195 a call against a $0.20 day: crosses 80% on call 3, 100% on call 4
        aws_clients = self._aws_clients = wiring(daily_usd="0.2")
        for call_number in range(6):
            handler.handler(cloudwatch_event([log_entry(request_id=f"r{call_number}")]), None)
        subjects = [alert["Subject"] for alert in aws_clients["sns"].published]
        self.assertEqual(sum("80%" in subject for subject in subjects), 1)
        self.assertEqual(sum("100%" in subject for subject in subjects), 1)

    def test_disable_happens_once_not_on_every_later_line(self):
        aws_clients = self._run_until_runaway(enforce_flag=True)
        before = len(aws_clients["iam"].deactivated)
        handler.handler(cloudwatch_event([log_entry(request_id="later")]), None)
        self.assertEqual(len(aws_clients["iam"].deactivated), before)

    def test_unpriced_model_alerts_once_not_once_per_call(self):
        aws_clients = self._aws_clients = wiring()
        for call_number in range(4):
            handler.handler(
                cloudwatch_event(
                    [log_entry(request_id=f"r{call_number}", model_id="meta.llama-x")]
                ),
                None,
            )
        subjects = [alert["Subject"] for alert in aws_clients["sns"].published]
        self.assertEqual(sum("unpriced model" in subject for subject in subjects), 1)

    def test_no_budget_row_alerts_once_and_applies_the_floor(self):
        aws_clients = self._aws_clients = wiring()
        aws_clients["budgets"] = FakeTable(key_names=("scope",))
        for call_number in range(3):
            handler.handler(cloudwatch_event([log_entry(request_id=f"r{call_number}")]), None)
        subjects = [alert["Subject"] for alert in aws_clients["sns"].published]
        self.assertEqual(sum("no budget row" in subject for subject in subjects), 1)

    def test_a_role_past_the_ceiling_says_so_instead_of_faking_a_disable(self):
        """A role has no access key, so enforcement cannot bite. It must not pretend."""
        identity.reset_cache()
        aws_clients = self._aws_clients = {
            "usage": FakeTable(),
            "budgets": budget_table("0.01", enforce_flag=True),
            "iam": FakeIAM(tags={"ShinroRunner": {"Track": TRACK}}),
            "sns": FakeSNS(),
        }
        handler.handler(cloudwatch_event([log_entry(arn=ARN_ASSUMED)]), None)
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), aws_clients["usage"].items)
        self.assertTrue(
            any("no access key" in alert["Subject"] for alert in aws_clients["sns"].published)
        )

    def _run_until_runaway(self, enforce_flag):
        # $0.06195 a call, $0.01 budget -> the first call is already past 1.5x
        aws_clients = wiring(daily_usd="0.01", enforce_flag=enforce_flag)
        self._aws_clients = aws_clients
        handler.handler(cloudwatch_event([log_entry()]), None)
        return aws_clients


# --------------------------------------------------------------------------
# 5. Every AWS failure that used to be swallowed.
# --------------------------------------------------------------------------
class TestFailuresRaise(WiredTestCase):
    def test_a_failed_batch_fails_the_invocation(self):
        """Otherwise the invocation reports success while spend goes uncounted."""
        self._aws_clients = wiring()
        with self.assertRaises(errors.BatchIncomplete) as caught:
            handler.handler(cloudwatch_event(["not json at all"]), None)
        self.assertEqual(caught.exception.stats["failed"], 1)

    def test_one_bad_line_does_not_stop_the_good_ones(self):
        aws_clients = self._aws_clients = wiring()
        with self.assertRaises(errors.BatchIncomplete) as caught:
            handler.handler(
                cloudwatch_event([log_entry(request_id="good"), "{{{ broken"]), None
            )
        self.assertEqual(
            caught.exception.stats,
            {"seen": 2, "counted": 1, "duplicate": 0, "failed": 1},
        )
        self.assertEqual(int(day_row(aws_clients)["calls"]), 1)

    def test_untagged_principal_raises_instead_of_charging_a_bucket(self):
        self._aws_clients = wiring(tags={})
        with self.assertRaises(errors.BatchIncomplete) as caught:
            handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertIsInstance(caught.exception.failures[0][1], errors.UntaggedPrincipal)

    def test_untagged_principal_is_not_looked_up_once_per_line(self):
        """It raises every time, but IAM is asked once -- otherwise it throttles."""
        iam = FakeIAM(tags={USER_NAME: {}})
        table = FakeTable()
        for _ in range(5):
            with self.assertRaises(errors.UntaggedPrincipal):
                identity.resolve(ARN_USER, iam, table)
        self.assertEqual(iam.tag_calls, 1)

    def test_failed_iam_lookup_raises_rather_than_misattributing(self):
        with self.assertRaises(errors.AttributionUnavailable):
            identity.resolve(ARN_USER, FakeIAM(fail_tags=True), FakeTable())

    def test_failed_budget_read_raises_rather_than_shrinking_the_budget(self):
        """It used to fall through to DEFAULT, quietly applying a smaller ceiling."""
        table = budget_table()
        table.fail_on.add("get_item")
        with self.assertRaises(errors.BudgetUnavailable):
            budgets.load(table, TRACK)

    def test_failed_key_lookup_does_not_record_a_disable(self):
        """The worst state reachable: ledger says stopped, key still spending."""
        aws_clients = self._aws_clients = wiring(
            daily_usd="0.01", enforce_flag=True, fail_list_keys=True
        )
        with self.assertRaises(errors.BatchIncomplete) as caught:
            handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertIsInstance(caught.exception.failures[0][1], errors.KeyLookupFailed)
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), aws_clients["usage"].items)

    def test_a_total_deactivation_failure_releases_the_claim_for_the_retry(self):
        aws_clients = self._aws_clients = wiring(
            daily_usd="0.01", enforce_flag=True, fail_update=["AKIAEXAMPLE1", "AKIAEXAMPLE2"]
        )
        with self.assertRaises(errors.BatchIncomplete):
            handler.handler(cloudwatch_event([log_entry()]), None)
        # Claim released, so the next log line tries again rather than finding it taken.
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), aws_clients["usage"].items)

        aws_clients["iam"].fail_update.clear()
        handler.handler(cloudwatch_event([log_entry(request_id="next")]), None)
        self.assertEqual(len(aws_clients["iam"].deactivated), 2)

    def test_a_partial_deactivation_keeps_the_claim_and_still_raises(self):
        aws_clients = self._aws_clients = wiring(
            daily_usd="0.01", enforce_flag=True, fail_update=["AKIAEXAMPLE2"]
        )
        with self.assertRaises(errors.BatchIncomplete):
            handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertEqual(aws_clients["iam"].deactivated, [(USER_NAME, "AKIAEXAMPLE1")])
        # Kept: it is an accurate record of what was switched off.
        self.assertIn((f"TRACK#{TRACK}", "DISABLED"), aws_clients["usage"].items)

    def test_a_failed_alert_raises_because_the_threshold_is_already_spent(self):
        aws_clients = self._aws_clients = wiring(daily_usd="0.01")
        aws_clients["sns"].fail = True
        with self.assertRaises(errors.BatchIncomplete) as caught:
            handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertIsInstance(caught.exception.failures[0][1], errors.AlertDeliveryFailed)

    def test_no_topic_configured_is_an_error_not_a_log_line(self):
        with self.assertRaises(errors.AlertDeliveryFailed):
            enforce.notify(FakeSNS(), None, "subject", {})

    def test_failed_dedup_write_raises_rather_than_counting_anyway(self):
        table = FakeTable(fail_on=["put_item"])
        with self.assertRaises(errors.LedgerWriteFailed):
            ledger.claim_count(table, "r1")


# --------------------------------------------------------------------------
# 6. Budget validation. Every one of these used to be accepted.
# --------------------------------------------------------------------------
class TestBudgetValidation(unittest.TestCase):
    def _load(self, **fields):
        row = {"scope": f"TRACK#{TRACK}", **fields}
        return budgets.load(FakeTable(items={(row["scope"],): row}, key_names=("scope",)), TRACK)

    def test_zero_budget_is_rejected_not_treated_as_unlimited(self):
        with self.assertRaises(errors.InvalidBudget):
            self._load(daily_usd=Decimal("0"))

    def test_negative_budget_is_rejected(self):
        with self.assertRaises(errors.InvalidBudget):
            self._load(daily_usd=Decimal("-5"))

    def test_a_runaway_multiple_below_one_is_rejected(self):
        """Below 1 the key is deactivated before the 100% alert is sent."""
        with self.assertRaises(errors.InvalidBudget):
            self._load(daily_usd=Decimal("10"), runaway_multiple=Decimal("0.5"))

    def test_a_string_enforce_flag_is_rejected(self):
        """'false' is truthy, and would have started deactivating keys."""
        with self.assertRaises(errors.InvalidBudget):
            self._load(daily_usd=Decimal("10"), enforce="false")

    def test_an_empty_alert_set_is_rejected(self):
        with self.assertRaises(errors.InvalidBudget):
            self._load(daily_usd=Decimal("10"), alert_pcts=set())

    def test_a_partial_row_inherits_default_rather_than_falling_to_the_floor(self):
        """Hand-writing one field used to replace a $25 budget with a $10 one."""
        table = FakeTable(
            items={
                ("DEFAULT",): {
                    "scope": "DEFAULT",
                    "daily_usd": Decimal("25"),
                    "runaway_multiple": Decimal("1.5"),
                    "alert_pcts": {80, 100},
                    "notify_topic_arn": TOPIC,
                    "enforce": False,
                },
                (f"TRACK#{TRACK}",): {"scope": f"TRACK#{TRACK}", "enforce": True},
            },
            key_names=("scope",),
        )
        budget = budgets.load(table, TRACK)
        self.assertEqual(budget.daily_usd, Decimal("25"))  # not the $10 floor
        self.assertTrue(budget.enforce)  # its own value wins
        self.assertEqual(budget.notify_topic_arn, TOPIC)

    def test_a_complete_row_costs_one_read(self):
        table = budget_table()
        budgets.load(table, TRACK)
        self.assertEqual(len(table.calls), 1)

    def test_evaluate_refuses_a_zero_budget_as_a_second_line_of_defence(self):
        zero_budget = budgets.Budget("TRACK#x", 0, (80,), 0, None, True)
        with self.assertRaises(errors.InvalidBudget):
            enforce.evaluate(1_000_000, zero_budget)


# --------------------------------------------------------------------------
# 7. Attribution
# --------------------------------------------------------------------------
class TestIdentity(unittest.TestCase):
    def setUp(self):
        identity.reset_cache()

    def test_arn_shapes(self):
        self.assertEqual(identity.parse_arn(ARN_USER), ("user", USER_NAME, ARN_USER))
        # the per-session suffix is meaningless for attribution and is dropped
        self.assertEqual(identity.parse_arn(ARN_ASSUMED), ("role", "ShinroRunner", ARN_ASSUMED))
        self.assertEqual(identity.parse_arn("arn:aws:iam::123456789012:role/R").name, "R")
        self.assertIsNone(identity.parse_arn("not-an-arn"))

    def test_the_track_tag_is_the_whole_attribution(self):
        """Other tags are read from IAM and deliberately not carried or cached."""
        iam = FakeIAM(tags={USER_NAME: {"Track": TRACK, "Owner": "platform-team"}})
        table = FakeTable()
        attribution = identity.resolve(ARN_USER, iam, table)
        self.assertEqual(attribution, identity.Attribution(TRACK, attribution.principal))
        self.assertEqual(attribution.principal.name, USER_NAME)
        cached = table.items[(f"ARN#{ARN_USER}", "TAGS")]
        self.assertEqual(sorted(cached), ["pk", "sk", "track", "ttl"])

    def test_an_unparseable_arn_raises(self):
        with self.assertRaises(errors.AttributionUnavailable):
            identity.resolve("nonsense", FakeIAM(), FakeTable())

    def test_second_lookup_does_not_hit_iam(self):
        iam = FakeIAM(tags={USER_NAME: {"Track": TRACK}})
        table = FakeTable()
        identity.resolve(ARN_USER, iam, table)
        identity.resolve(ARN_USER, iam, table)
        self.assertEqual(iam.tag_calls, 1)

    def test_the_item_cache_survives_a_cold_start(self):
        iam = FakeIAM(tags={USER_NAME: {"Track": TRACK}})
        table = FakeTable()
        identity.resolve(ARN_USER, iam, table)
        identity.reset_cache()  # new container, same table
        attribution = identity.resolve(ARN_USER, iam, table)
        self.assertEqual(attribution.track, TRACK)
        self.assertEqual(iam.tag_calls, 1)


# --------------------------------------------------------------------------
# 8. Event unpacking
# --------------------------------------------------------------------------
class TestUnpack(unittest.TestCase):
    def test_one_event_carries_many_log_lines(self):
        messages = handler.unpack(
            cloudwatch_event([log_entry(request_id="a"), log_entry(request_id="b")])
        )
        import json

        self.assertEqual([json.loads(message)["requestId"] for message in messages], ["a", "b"])

    def test_control_message_carries_no_invocations(self):
        import base64, gzip, json

        compressed = gzip.compress(json.dumps({"messageType": "CONTROL_MESSAGE"}).encode())
        self.assertEqual(
            handler.unpack({"awslogs": {"data": base64.b64encode(compressed).decode()}}), []
        )

    def test_empty_event(self):
        self.assertEqual(handler.unpack({}), [])


# --------------------------------------------------------------------------
# 9. The release path, and where it announces
# --------------------------------------------------------------------------
class TestUnblock(WiredTestCase):
    """The disable page and the release announcement must land in one place.

    `unblock` used to publish to NOTIFY_TOPIC_ARN unconditionally, so a track
    whose budget row named its own topic had the meter page one audience and the
    release announced to another -- the rota woken up to fix a disabled track
    never saw the record of it being released.
    """

    PROD_TOPIC = "arn:aws:sns:us-east-1:123456789012:prod-pager"

    def setUp(self):
        super().setUp()
        self._real_unblock_clients = unblock.clients
        self._real_unblock_topic = unblock.NOTIFY_TOPIC_ARN
        unblock._clients.clear()
        unblock.clients = lambda: self._aws_clients
        unblock.NOTIFY_TOPIC_ARN = TOPIC

    def tearDown(self):
        unblock.clients = self._real_unblock_clients
        unblock.NOTIFY_TOPIC_ARN = self._real_unblock_topic
        unblock._clients.clear()
        super().tearDown()

    def _disable_track(self, row_topic=None):
        """Run a track past its ceiling so there is something to release."""
        aws_clients = self._aws_clients = wiring(daily_usd="0.01", enforce_flag=True)
        if row_topic is not None:
            aws_clients["budgets"].items[(f"TRACK#{TRACK}",)]["notify_topic_arn"] = row_topic
        handler.handler(cloudwatch_event([log_entry()]), None)
        return aws_clients

    def _subjects_to(self, aws_clients, topic):
        return [
            alert["Subject"]
            for alert in aws_clients["sns"].published
            if alert["TopicArn"] == topic
        ]

    def test_release_is_announced_on_the_tracks_own_topic(self):
        aws_clients = self._disable_track(row_topic=self.PROD_TOPIC)
        self.assertTrue(
            any(
                "DISABLED" in subject
                for subject in self._subjects_to(aws_clients, self.PROD_TOPIC)
            )
        )

        unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)

        on_prod = self._subjects_to(aws_clients, self.PROD_TOPIC)
        self.assertTrue(any("RE-ENABLED" in subject for subject in on_prod))
        # and nothing leaked to the stack default
        self.assertEqual(self._subjects_to(aws_clients, TOPIC), [])

    def test_release_falls_back_to_the_default_topic(self):
        aws_clients = self._disable_track()  # budget row names no topic
        unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)
        self.assertTrue(
            any("RE-ENABLED" in subject for subject in self._subjects_to(aws_clients, TOPIC))
        )

    def test_the_release_actually_reactivates_and_clears(self):
        aws_clients = self._disable_track()
        result = unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)
        self.assertTrue(result["ok"])
        self.assertEqual(
            sorted(key_id for _, key_id in aws_clients["iam"].activated),
            ["AKIAEXAMPLE1", "AKIAEXAMPLE2"],
        )
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), aws_clients["usage"].items)

    def test_a_failed_budget_read_refuses_before_touching_anything(self):
        """Resolved first, so the keys are not reactivated and then left unannounced."""
        aws_clients = self._disable_track()
        aws_clients["budgets"].fail_on.add("get_item")
        with self.assertRaises(errors.BudgetUnavailable):
            unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)
        self.assertEqual(aws_clients["iam"].activated, [])
        self.assertIn((f"TRACK#{TRACK}", "DISABLED"), aws_clients["usage"].items)

    def test_re_enabling_something_not_disabled_is_refused(self):
        self._aws_clients = wiring()
        with self.assertRaises(errors.NotDisabled):
            unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)

    def test_a_short_reason_is_refused(self):
        self._aws_clients = self._disable_track()
        with self.assertRaises(errors.MalformedLogEntry):
            unblock.handler({"track": TRACK, "reason": "oops"}, None)



# --------------------------------------------------------------------------
# 10. The weekly rate card: reading it, and refusing a bad one.
# --------------------------------------------------------------------------
class TestRateCardLoad(unittest.TestCase):
    def setUp(self):
        ratecard.reset_cache()

    def tearDown(self):
        ratecard.reset_cache()

    def test_no_card_at_all_means_every_model_is_the_fallback(self):
        """There is no committed card any more, so this is the only safe answer."""
        self.assertIsNone(ratecard.load(FakeTable(key_names=("scope",))))
        priced = pricing.price(pricing.Usage(1000, 0, 0, 0), SONNET, None)
        self.assertTrue(priced.is_fallback)
        self.assertEqual(priced.micros, 20_000)  # FALLBACK input, not Sonnet's 3_000

    def test_a_stored_card_prices_the_models_on_it(self):
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        card = ratecard.load(table, now=_epoch("2026-10-15T00:00:00+00:00"))
        self.assertFalse(card.is_stale)
        priced = pricing.price(pricing.Usage(1000, 0, 0, 0), SONNET, card.rate_for(SONNET))
        self.assertEqual(priced.micros, 3_000)
        self.assertFalse(priced.is_fallback)

    def test_a_model_missing_from_the_card_is_the_fallback(self):
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        card = ratecard.load(table)
        self.assertIsNone(card.rate_for("meta.llama-not-on-card"))

    def test_a_failed_read_raises_rather_than_charging_the_fallback(self):
        """A blip must not bake a 6x price into the ledger; the retry is cheap."""
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        table.fail_on.add("get_item")
        with self.assertRaises(errors.RateCardUnavailable):
            ratecard.load(table)

    def test_a_half_written_card_is_discarded_not_half_used(self):
        """A missing token class would price those tokens at zero -- no limit at all."""
        broken = rate_card_item()
        del broken["models"][SONNET_ID]["cache_read"]
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): broken}, key_names=("scope",))
        self.assertIsNone(ratecard.load(table))

    def test_a_card_nobody_has_refreshed_in_a_month_is_stale(self):
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        self.assertFalse(ratecard.load(table, now=_epoch("2026-10-15T00:00:00+00:00")).is_stale)
        ratecard.reset_cache()
        self.assertTrue(ratecard.load(table, now=_epoch("2026-12-01T00:00:00+00:00")).is_stale)

    def test_the_card_is_read_once_per_container_not_once_per_line(self):
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        for _ in range(5):
            ratecard.load(table)
        reads = [call for call in table.calls if call[0] == "get_item"]
        self.assertEqual(len(reads), 1)


class TestRateCardValidation(unittest.TestCase):
    """Every rejection here is a card that would have mis-metered every track."""

    def test_a_clean_card_passes(self):
        ratecard.validate({SONNET_ID: rates()}, {SONNET_ID: rates()})

    def test_an_empty_card_is_rejected(self):
        with self.assertRaises(errors.RateCardRejected):
            ratecard.validate({}, {SONNET_ID: rates()})

    def test_a_zero_rate_is_rejected_because_it_means_no_limit(self):
        with self.assertRaises(errors.RateCardRejected):
            ratecard.validate({SONNET_ID: rates(output="0")}, {})

    def test_cache_reads_costing_more_than_input_is_rejected(self):
        """The token-class mix-up. Cache reads bill at about a tenth of input."""
        with self.assertRaises(errors.RateCardRejected) as caught:
            ratecard.validate({SONNET_ID: rates(cache_read="9")}, {})
        self.assertTrue(any("cache_read" in reason for reason in caught.exception.reasons))

    def test_a_thousand_fold_move_is_rejected(self):
        """What a per-1K price parsed as per-million looks like."""
        with self.assertRaises(errors.RateCardRejected) as caught:
            ratecard.validate({SONNET_ID: rates(input_="3000")}, {SONNET_ID: rates()})
        self.assertTrue(any("moved" in reason for reason in caught.exception.reasons))

    def test_a_small_real_price_change_is_accepted(self):
        ratecard.validate({SONNET_ID: rates(input_="3.30")}, {SONNET_ID: rates()})

    def test_a_brand_new_model_has_nothing_to_drift_from(self):
        ratecard.validate({"new.model-v1:0": rates(input_="99")}, {SONNET_ID: rates()})

    def test_a_dropped_model_keeps_its_last_known_rate(self):
        """Otherwise it has no row and prices at FALLBACK for no reason of its own."""
        merged = ratecard.merge_over_current({"new.model-v1:0": rates()}, {SONNET_ID: rates()})
        self.assertEqual(sorted(merged), sorted([SONNET_ID, "new.model-v1:0"]))

    def test_a_round_trip_through_dynamodb_keeps_the_cents(self):
        item = ratecard.item_from_rates(
            {SONNET_ID: rates()}, fetched_at=FRESH, source="test", region="us-east-1",
        )
        self.assertEqual(ratecard.rates_from_item(item)[SONNET_ID], rates())


class TestPriceListParsing(unittest.TestCase):
    def test_four_skus_become_one_model_entry_in_usd_per_million(self):
        result = pricelist.fetch(FakePricing([sonnet_price_list()]), "us-east-1")
        self.assertEqual(
            result.rates[SONNET_ID],
            pricing.Rates(
                input=Decimal("3.000"),
                output=Decimal("15.000"),
                cache_read=Decimal("0.300"),
                cache_write=Decimal("3.750"),
            ),
        )
        self.assertEqual(result.skus_read, 4)
        self.assertEqual(result.unmapped_names, ())

    def test_the_price_list_is_filtered_by_region(self):
        fake = FakePricing([sonnet_price_list()])
        pricelist.fetch(fake, "eu-west-1")
        self.assertIn(
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "eu-west-1"},
            fake.paginate_calls[0]["Filters"],
        )

    def test_a_cache_sku_is_not_mistaken_for_input(self):
        """`CacheReadInputTokenCount` contains `InputToken`; order decides this."""
        self.assertEqual(pricelist._token_class("USE1-CacheReadInputTokenCount"), "cache_read")
        self.assertEqual(pricelist._token_class("USE1-CacheWriteInputTokenCount"), "cache_write")
        self.assertEqual(pricelist._token_class("USE1-InputTokenCount"), "input")

    def test_a_price_quoted_per_million_is_not_scaled_again(self):
        skus = sonnet_price_list()
        for sku in skus:
            sku["terms"]["OnDemand"][f"{sku['product']['sku']}.JRTCKXETXF"]["priceDimensions"][
                f"{sku['product']['sku']}.JRTCKXETXF.6YS6EN2CT7"
            ].update({"unit": "1M tokens", "pricePerUnit": {"USD": "3.00"}})
        result = pricelist.fetch(FakePricing([skus]), "us-east-1")
        self.assertEqual(result.rates[SONNET_ID].input, Decimal("3.00"))

    def test_an_unknown_unit_raises_rather_than_guessing(self):
        """Guessing between per-1K and per-million is a 1000x error in every budget."""
        skus = sonnet_price_list()
        sku = skus[0]
        sku["terms"]["OnDemand"]["SKU-IN.JRTCKXETXF"]["priceDimensions"][
            "SKU-IN.JRTCKXETXF.6YS6EN2CT7"
        ]["unit"] = "Furlongs"
        with self.assertRaises(errors.PriceListUnavailable):
            pricelist.fetch(FakePricing([skus]), "us-east-1")

    def test_a_model_name_nobody_mapped_is_reported_not_guessed(self):
        skus = sonnet_price_list(model_name="Claude Nebula 9")
        with self.assertRaises(errors.PriceListUnavailable):
            # nothing else was recognisable, so there is no card to promote
            pricelist.fetch(FakePricing([skus]), "us-east-1")

    def test_an_unmapped_model_alongside_a_mapped_one_is_listed(self):
        pages = [sonnet_price_list() + sonnet_price_list(model_name="Claude Nebula 9")]
        result = pricelist.fetch(FakePricing(pages), "us-east-1")
        self.assertIn(SONNET_ID, result.rates)
        self.assertEqual(result.unmapped_names, ("Claude Nebula 9",))

    def test_provisioned_and_batch_skus_are_ignored(self):
        pages = [
            sonnet_price_list()
            + [price_list_product(usage_type="USE1-InputTokenCount", inference_type="Provisioned",
                                  usd_per_unit="99", sku="SKU-PT")]
        ]
        result = pricelist.fetch(FakePricing(pages), "us-east-1")
        self.assertEqual(result.rates[SONNET_ID].input, Decimal("3.000"))

    def test_a_model_without_caching_defaults_to_the_input_rate(self):
        """Its cached token counts are zero, so this never multiplies anything."""
        skus = sonnet_price_list()[:2]  # input and output only
        result = pricelist.fetch(FakePricing([skus]), "us-east-1")
        self.assertEqual(result.rates[SONNET_ID].cache_read, Decimal("3.000"))
        self.assertEqual(
            sorted(result.defaulted),
            [f"{SONNET_ID}.cache_read", f"{SONNET_ID}.cache_write"],
        )

    def test_a_model_missing_its_output_rate_raises(self):
        """Half a card is worse than none: it would under-count every reply."""
        skus = [sonnet_price_list()[0]]  # input only
        with self.assertRaises(errors.PriceListUnavailable):
            pricelist.fetch(FakePricing([skus]), "us-east-1")

    def test_a_failed_price_list_call_raises(self):
        with self.assertRaises(errors.PriceListUnavailable):
            pricelist.fetch(FakePricing([], fail=True), "us-east-1")

    def test_recognising_nothing_raises_rather_than_promoting_an_empty_card(self):
        with self.assertRaises(errors.PriceListUnavailable):
            pricelist.fetch(FakePricing([[]]), "us-east-1")


class TestRefreshRates(unittest.TestCase):
    """The weekly job: validate everything, then rotate exactly two rows."""

    def setUp(self):
        ratecard.reset_cache()
        self._real_clients = refresh_rates.clients
        self._real_topic = refresh_rates.NOTIFY_TOPIC_ARN
        refresh_rates._clients.clear()
        refresh_rates.clients = lambda: self._aws_clients
        refresh_rates.NOTIFY_TOPIC_ARN = TOPIC

    def tearDown(self):
        refresh_rates.clients = self._real_clients
        refresh_rates.NOTIFY_TOPIC_ARN = self._real_topic
        refresh_rates._clients.clear()
        ratecard.reset_cache()

    def _wire(self, pages, budgets_table=None):
        self._aws_clients = {
            "budgets": budgets_table
            if budgets_table is not None
            else FakeTable(key_names=("scope",)),
            "usage": FakeTable(),
            "pricing": FakePricing(pages),
            "sns": FakeSNS(),
        }
        return self._aws_clients

    @staticmethod
    def _scopes(aws_clients):
        return sorted(key[0] for key in aws_clients["budgets"].items)

    def test_a_first_run_writes_only_the_current_row(self):
        aws_clients = self._wire([sonnet_price_list()])
        result = refresh_rates.handler({}, None)

        self.assertTrue(result["promoted"])
        self.assertEqual(self._scopes(aws_clients), [ratecard.CURRENT_SCOPE])
        stored = aws_clients["budgets"].items[(ratecard.CURRENT_SCOPE,)]
        self.assertEqual(stored["models"][SONNET_ID]["input"], Decimal("3.000"))
        self.assertIsNone(stored[ratecard.PREVIOUS_SCOPE_ATTR])

    def test_the_second_run_keeps_the_outgoing_card_under_its_own_date(self):
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        aws_clients = self._wire([sonnet_price_list(input_usd="0.0033")], table)
        refresh_rates.handler({}, None)

        self.assertEqual(
            self._scopes(aws_clients), ["RATECARD#2026-10-14", ratecard.CURRENT_SCOPE]
        )
        # the dated row is the card that was live, not the one just fetched
        self.assertEqual(
            aws_clients["budgets"].items[("RATECARD#2026-10-14",)]["models"][SONNET_ID]["input"],
            Decimal("3"),
        )
        current = aws_clients["budgets"].items[(ratecard.CURRENT_SCOPE,)]
        self.assertEqual(current["models"][SONNET_ID]["input"], Decimal("3.300"))
        self.assertEqual(current[ratecard.PREVIOUS_SCOPE_ATTR], "RATECARD#2026-10-14")

    def test_there_are_never_more_than_two_rows(self):
        table = FakeTable(
            items={
                (ratecard.CURRENT_SCOPE,): rate_card_item(
                    **{ratecard.PREVIOUS_SCOPE_ATTR: "RATECARD#2026-10-07"}
                ),
                ("RATECARD#2026-10-07",): rate_card_item(fetched_at="2026-10-07T03:00:00+00:00"),
            },
            key_names=("scope",),
        )
        aws_clients = self._wire([sonnet_price_list(input_usd="0.0033")], table)
        refresh_rates.handler({}, None)

        scopes = self._scopes(aws_clients)
        self.assertEqual(scopes, ["RATECARD#2026-10-14", ratecard.CURRENT_SCOPE])
        self.assertNotIn("RATECARD#2026-10-07", scopes)  # two cards old, deleted

    def test_a_rejected_card_writes_absolutely_nothing(self):
        """Validation runs before any write, so DynamoDB is left exactly as it was."""
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        aws_clients = self._wire([sonnet_price_list(input_usd="3.00")], table)  # -> $3000/M
        with self.assertRaises(errors.RateCardRejected):
            refresh_rates.handler({}, None)

        self.assertEqual(self._scopes(aws_clients), [ratecard.CURRENT_SCOPE])
        self.assertEqual(
            aws_clients["budgets"].items[(ratecard.CURRENT_SCOPE,)]["models"][SONNET_ID]["input"],
            Decimal("3"),
        )
        self.assertEqual(
            [
                call
                for call in aws_clients["budgets"].calls
                if call[0] in ("put_item", "delete_item")
            ],
            [],
        )

    def test_a_rejection_emails_the_old_and_new_numbers(self):
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        aws_clients = self._wire([sonnet_price_list(input_usd="3.00")], table)
        with self.assertRaises(errors.RateCardRejected):
            refresh_rates.handler({}, None)

        alert = aws_clients["sns"].published[0]
        self.assertIn("REJECTED", alert["Subject"])
        self.assertIn('"stored"', alert["Message"])
        self.assertIn('"fetched"', alert["Message"])

    def test_the_meter_then_prices_with_the_refreshed_card(self):
        aws_clients = self._wire([sonnet_price_list(input_usd="0.006")])
        refresh_rates.handler({}, None)
        ratecard.reset_cache()
        card = ratecard.load(aws_clients["budgets"])
        self.assertEqual(
            pricing.price(
                pricing.Usage(1_000_000, 0, 0, 0), SONNET, card.rate_for(SONNET)
            ).micros,
            6_000_000,
        )

    def test_a_failed_price_list_leaves_everything_alone_and_raises(self):
        table = FakeTable(items={(ratecard.CURRENT_SCOPE,): rate_card_item()},
                          key_names=("scope",))
        self._aws_clients = {
            "budgets": table, "usage": FakeTable(),
            "pricing": FakePricing([], fail=True), "sns": FakeSNS(),
        }
        with self.assertRaises(errors.PriceListUnavailable):
            refresh_rates.handler({}, None)
        self.assertEqual(self._scopes(self._aws_clients), [ratecard.CURRENT_SCOPE])

    def test_an_unmapped_model_is_announced_once_a_day(self):
        pages = [sonnet_price_list() + sonnet_price_list(model_name="Claude Nebula 9")]
        aws_clients = self._wire(pages)
        refresh_rates.handler({}, None)
        aws_clients["pricing"] = FakePricing(pages)
        refresh_rates.handler({}, None)
        subjects = [alert["Subject"] for alert in aws_clients["sns"].published]
        self.assertEqual(sum("refreshed with gaps" in subject for subject in subjects), 1)

    def test_a_clean_refresh_sends_no_email(self):
        aws_clients = self._wire([sonnet_price_list()])
        refresh_rates.handler({}, None)
        self.assertEqual(aws_clients["sns"].published, [])


class TestMeterUsesTheCard(WiredTestCase):
    """End to end: the row in the table has to be what the ledger records."""

    def test_the_ledger_records_the_price_on_the_card(self):
        aws_clients = self._aws_clients = wiring()
        handler.handler(cloudwatch_event([log_entry()]), None)
        # 12400*3 + 850*15 + 40000*0.30, from the seeded RATECARD#CURRENT row
        self.assertEqual(int(day_row(aws_clients)["spent_micros"]), 61_950)

    def test_changing_the_card_changes_what_is_charged(self):
        aws_clients = self._aws_clients = wiring()
        aws_clients["budgets"].items[(ratecard.CURRENT_SCOPE,)] = rate_card_item(
            models={SONNET_ID: rates(input_="6", output="30", cache_read="0.6", cache_write="7.5")}
        )
        ratecard.reset_cache()
        handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertEqual(int(day_row(aws_clients)["spent_micros"]), 123_900)

    def test_no_card_at_all_charges_the_fallback_and_says_so(self):
        aws_clients = self._aws_clients = wiring()
        del aws_clients["budgets"].items[(ratecard.CURRENT_SCOPE,)]
        ratecard.reset_cache()

        handler.handler(cloudwatch_event([log_entry()]), None)

        subjects = [alert["Subject"] for alert in aws_clients["sns"].published]
        self.assertTrue(any("unpriced model" in subject for subject in subjects))
        self.assertGreater(int(day_row(aws_clients)["spent_micros"]), 61_950)


class TestStaleRateCardAlerts(WiredTestCase):
    def test_a_stale_card_pages_once_a_day_across_every_track(self):
        aws_clients = self._aws_clients = wiring()
        aws_clients["budgets"].items[(ratecard.CURRENT_SCOPE,)] = rate_card_item(
            fetched_at="2020-01-01T00:00:00+00:00"
        )
        ratecard.reset_cache()
        for call_number in range(3):
            handler.handler(
                cloudwatch_event([log_entry(request_id=f"r{call_number}")]), None
            )
        subjects = [alert["Subject"] for alert in aws_clients["sns"].published]
        self.assertEqual(sum("has not refreshed" in subject for subject in subjects), 1)


def _epoch(stamp: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(stamp).timestamp()


if __name__ == "__main__":
    unittest.main()
