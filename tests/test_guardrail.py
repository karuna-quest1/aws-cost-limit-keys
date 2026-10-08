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
import pricing  # noqa: E402
import unblock  # noqa: E402
from events import (  # noqa: E402
    ARN_ASSUMED,
    ARN_USER,
    SONNET,
    TRACK,
    USER_NAME,
    cloudwatch_event,
    log_entry,
)
from fakes import FakeIAM, FakeSNS, FakeTable  # noqa: E402

TOPIC = "arn:aws:sns:us-east-1:123456789012:bedrock-alerts"


def budget_table(daily_usd="50", enforce_flag=False, multiple="1.5", scope=f"TRACK#{TRACK}"):
    return FakeTable(
        items={
            (scope,): {
                "scope": scope,
                "daily_usd": Decimal(daily_usd),
                "runaway_multiple": Decimal(multiple),
                "alert_pcts": {80, 100},
                "notify_topic_arn": TOPIC,
                "enforce": enforce_flag,
            }
        },
        key_names=("scope",),
    )


def wiring(daily_usd="50", enforce_flag=False, tags=None, **iam_kwargs):
    identity.reset_cache()
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


def day_row(c, track=TRACK):
    return c["usage"].items[(f"TRACK#{track}", f"DAY#{ledger.utc_day()}")]


class WiredTestCase(unittest.TestCase):
    """Replaces handler.clients() with the fakes for the duration of a test."""

    def setUp(self):
        self._c = None
        self._real_clients = handler.clients
        handler._clients.clear()
        handler.clients = lambda: self._c
        # The deployed function always carries a topic, from the template. It is
        # what makes the no-budget alert deliverable at all: a budget row that
        # does not exist cannot name a topic, so without this default the alert
        # announcing the missing budget has nowhere to go.
        self._real_topic = handler.NOTIFY_TOPIC_ARN
        handler.NOTIFY_TOPIC_ARN = TOPIC
        identity.reset_cache()

    def tearDown(self):
        handler.clients = self._real_clients
        handler.NOTIFY_TOPIC_ARN = self._real_topic
        handler._clients.clear()


# --------------------------------------------------------------------------
# 1. A fake log line goes in, the right dollar amount comes out.
# --------------------------------------------------------------------------
class TestPricing(unittest.TestCase):
    def test_worked_example_from_the_readme(self):
        usage = pricing.usage_from_log(log_entry())
        self.assertEqual(usage, pricing.Usage(12_400, 850, 40_000, 0))
        # 12400*3 + 850*15 + 40000*0.30 = 37200 + 12750 + 12000 micro-USD
        self.assertEqual(pricing.price(usage, SONNET).micros, 61_950)

    def test_cache_reads_are_not_billed_as_input(self):
        """The whole reason the card carries four rates and not two."""
        as_input = pricing.price(pricing.Usage(40_000, 0, 0, 0), SONNET).micros
        as_cache = pricing.price(pricing.Usage(0, 0, 40_000, 0), SONNET).micros
        self.assertEqual(as_input, 120_000)
        self.assertEqual(as_cache, 12_000)  # 10x cheaper; collapsing them overstates badly

    def test_geography_prefix_resolves_to_the_same_card(self):
        bare = pricing.price(pricing.Usage(1000, 0, 0, 0), "anthropic.claude-sonnet-4-5-20250929-v1:0")
        prefixed = pricing.price(pricing.Usage(1000, 0, 0, 0), SONNET)
        self.assertEqual(bare.micros, prefixed.micros)
        self.assertFalse(prefixed.is_fallback)

    def test_unknown_model_is_expensive_and_flagged_never_free(self):
        priced = pricing.price(pricing.Usage(1000, 0, 0, 0), "meta.llama-not-in-card")
        self.assertTrue(priced.is_fallback)
        self.assertGreater(priced.micros, 0)  # free would mean an unlimited budget

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
        first = len([c for c in self.table.calls if c[0] == "update_item"])
        self.table.calls.clear()
        self._accumulate()
        second = len([c for c in self.table.calls if c[0] == "update_item"])
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
        c = self._c = wiring()
        event = cloudwatch_event([log_entry()])

        first = handler.handler(event, None)
        second = handler.handler(event, None)

        self.assertEqual(first["counted"], 1)
        self.assertEqual(second["counted"], 0)
        self.assertEqual(second["duplicate"], 1)

        row = day_row(c)
        self.assertEqual(int(row["spent_micros"]), 61_950)
        self.assertEqual(int(row["calls"]), 1)

    def test_distinct_requests_accumulate(self):
        c = self._c = wiring()
        handler.handler(
            cloudwatch_event([log_entry(request_id="r1"), log_entry(request_id="r2")]), None
        )
        row = day_row(c)
        self.assertEqual(int(row["calls"]), 2)
        self.assertEqual(int(row["spent_micros"]), 123_900)

    def test_a_duplicate_still_reacts_to_the_current_total(self):
        """Dedup protects the money, not the reaction.

        If the invocation that counted the spend died before disabling, the
        redelivery has to finish the job. Simulated by counting the line with
        enforcement off, then redelivering it with enforcement on.
        """
        c = self._c = wiring(daily_usd="0.01", enforce_flag=False)
        handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertEqual(c["iam"].deactivated, [])

        c["budgets"] = budget_table("0.01", enforce_flag=True)
        stats = handler.handler(cloudwatch_event([log_entry()]), None)

        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(len(c["iam"].deactivated), 2)
        self.assertEqual(int(day_row(c)["calls"]), 1)  # and it did not re-count


# --------------------------------------------------------------------------
# 4. The runaway path fires at the right threshold, and enforce=false
#    means nothing is actually disabled.
# --------------------------------------------------------------------------
class TestEnforcement(WiredTestCase):
    def test_threshold_arithmetic(self):
        b = budgets.load(budget_table(), TRACK)
        self.assertEqual(b.daily_micros, 50_000_000)
        self.assertEqual(b.runaway_micros, 75_000_000)  # 1.5x
        self.assertEqual(enforce.evaluate(39_000_000, b).crossed, ())
        self.assertEqual(enforce.evaluate(40_000_000, b).crossed, (80,))
        self.assertEqual(enforce.evaluate(50_000_000, b).crossed, (80, 100))
        self.assertFalse(enforce.evaluate(74_999_999, b).runaway)
        self.assertTrue(enforce.evaluate(75_000_000, b).runaway)

    def test_absent_budget_row_is_never_unlimited(self):
        b = budgets.load(FakeTable(key_names=("scope",)), "brand-new")
        self.assertEqual(b.daily_micros, int(budgets.FLOOR_DAILY_USD * 1_000_000))
        self.assertTrue(b.is_floor)
        self.assertFalse(b.enforce)  # and watch-only until someone opts in

    def test_watch_only_alerts_but_disables_nothing(self):
        c = self._run_until_runaway(enforce_flag=False)
        self.assertEqual(c["iam"].deactivated, [])
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), c["usage"].items)
        self.assertTrue(any("100%" in p["Subject"] for p in c["sns"].published))

    def test_enforcing_deactivates_keys_and_records_why(self):
        c = self._run_until_runaway(enforce_flag=True)
        self.assertEqual(
            sorted(c["iam"].deactivated),
            [(USER_NAME, "AKIAEXAMPLE1"), (USER_NAME, "AKIAEXAMPLE2")],
        )
        record = c["usage"].items[(f"TRACK#{TRACK}", "DISABLED")]
        self.assertEqual(record["principals"], {USER_NAME})
        self.assertEqual(
            record["keys"], {f"{USER_NAME}/AKIAEXAMPLE1", f"{USER_NAME}/AKIAEXAMPLE2"}
        )
        self.assertNotIn("ttl", record)  # must not expire and strand a dead key
        self.assertTrue(any("DISABLED" in p["Subject"] for p in c["sns"].published))

    def test_each_threshold_emails_once_per_day(self):
        # $0.06195 a call against a $0.20 day: crosses 80% on call 3, 100% on call 4
        c = self._c = wiring(daily_usd="0.2")
        for i in range(6):
            handler.handler(cloudwatch_event([log_entry(request_id=f"r{i}")]), None)
        subjects = [p["Subject"] for p in c["sns"].published]
        self.assertEqual(sum("80%" in s for s in subjects), 1)
        self.assertEqual(sum("100%" in s for s in subjects), 1)

    def test_disable_happens_once_not_on_every_later_line(self):
        c = self._run_until_runaway(enforce_flag=True)
        before = len(c["iam"].deactivated)
        handler.handler(cloudwatch_event([log_entry(request_id="later")]), None)
        self.assertEqual(len(c["iam"].deactivated), before)

    def test_unpriced_model_alerts_once_not_once_per_call(self):
        c = self._c = wiring()
        for i in range(4):
            handler.handler(
                cloudwatch_event([log_entry(request_id=f"r{i}", model_id="meta.llama-x")]), None
            )
        subjects = [p["Subject"] for p in c["sns"].published]
        self.assertEqual(sum("unpriced model" in s for s in subjects), 1)

    def test_no_budget_row_alerts_once_and_applies_the_floor(self):
        c = self._c = wiring()
        c["budgets"] = FakeTable(key_names=("scope",))
        for i in range(3):
            handler.handler(cloudwatch_event([log_entry(request_id=f"r{i}")]), None)
        subjects = [p["Subject"] for p in c["sns"].published]
        self.assertEqual(sum("no budget row" in s for s in subjects), 1)

    def test_a_role_past_the_ceiling_says_so_instead_of_faking_a_disable(self):
        """A role has no access key, so enforcement cannot bite. It must not pretend."""
        identity.reset_cache()
        c = self._c = {
            "usage": FakeTable(),
            "budgets": budget_table("0.01", enforce_flag=True),
            "iam": FakeIAM(tags={"ShinroRunner": {"Track": TRACK}}),
            "sns": FakeSNS(),
        }
        handler.handler(cloudwatch_event([log_entry(arn=ARN_ASSUMED)]), None)
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), c["usage"].items)
        self.assertTrue(any("no access key" in p["Subject"] for p in c["sns"].published))

    def _run_until_runaway(self, enforce_flag):
        # $0.06195 a call, $0.01 budget -> the first call is already past 1.5x
        c = wiring(daily_usd="0.01", enforce_flag=enforce_flag)
        self._c = c
        handler.handler(cloudwatch_event([log_entry()]), None)
        return c


# --------------------------------------------------------------------------
# 5. Every AWS failure that used to be swallowed.
# --------------------------------------------------------------------------
class TestFailuresRaise(WiredTestCase):
    def test_a_failed_batch_fails_the_invocation(self):
        """Otherwise the invocation reports success while spend goes uncounted."""
        self._c = wiring()
        with self.assertRaises(errors.BatchIncomplete) as caught:
            handler.handler(cloudwatch_event(["not json at all"]), None)
        self.assertEqual(caught.exception.stats["failed"], 1)

    def test_one_bad_line_does_not_stop_the_good_ones(self):
        c = self._c = wiring()
        with self.assertRaises(errors.BatchIncomplete) as caught:
            handler.handler(
                cloudwatch_event([log_entry(request_id="good"), "{{{ broken"]), None
            )
        self.assertEqual(caught.exception.stats, {"seen": 2, "counted": 1, "duplicate": 0, "failed": 1})
        self.assertEqual(int(day_row(c)["calls"]), 1)

    def test_untagged_principal_raises_instead_of_charging_a_bucket(self):
        self._c = wiring(tags={})
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
        c = self._c = wiring(daily_usd="0.01", enforce_flag=True, fail_list_keys=True)
        with self.assertRaises(errors.BatchIncomplete) as caught:
            handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertIsInstance(caught.exception.failures[0][1], errors.KeyLookupFailed)
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), c["usage"].items)

    def test_a_total_deactivation_failure_releases_the_claim_for_the_retry(self):
        c = self._c = wiring(
            daily_usd="0.01", enforce_flag=True, fail_update=["AKIAEXAMPLE1", "AKIAEXAMPLE2"]
        )
        with self.assertRaises(errors.BatchIncomplete):
            handler.handler(cloudwatch_event([log_entry()]), None)
        # Claim released, so the next log line tries again rather than finding it taken.
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), c["usage"].items)

        c["iam"].fail_update.clear()
        handler.handler(cloudwatch_event([log_entry(request_id="next")]), None)
        self.assertEqual(len(c["iam"].deactivated), 2)

    def test_a_partial_deactivation_keeps_the_claim_and_still_raises(self):
        c = self._c = wiring(daily_usd="0.01", enforce_flag=True, fail_update=["AKIAEXAMPLE2"])
        with self.assertRaises(errors.BatchIncomplete):
            handler.handler(cloudwatch_event([log_entry()]), None)
        self.assertEqual(c["iam"].deactivated, [(USER_NAME, "AKIAEXAMPLE1")])
        # Kept: it is an accurate record of what was switched off.
        self.assertIn((f"TRACK#{TRACK}", "DISABLED"), c["usage"].items)

    def test_a_failed_alert_raises_because_the_threshold_is_already_spent(self):
        c = self._c = wiring(daily_usd="0.01")
        c["sns"].fail = True
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
        b = budgets.load(table, TRACK)
        self.assertEqual(b.daily_usd, Decimal("25"))  # not the $10 floor
        self.assertTrue(b.enforce)  # its own value wins
        self.assertEqual(b.notify_topic_arn, TOPIC)

    def test_a_complete_row_costs_one_read(self):
        table = budget_table()
        budgets.load(table, TRACK)
        self.assertEqual(len(table.calls), 1)

    def test_evaluate_refuses_a_zero_budget_as_a_second_line_of_defence(self):
        zero = budgets.Budget("TRACK#x", 0, (80,), 0, None, True)
        with self.assertRaises(errors.InvalidBudget):
            enforce.evaluate(1_000_000, zero)


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
        iam = FakeIAM(tags={USER_NAME: {"Track": TRACK, "Owner": "platform-team"}})
        who = identity.resolve(ARN_USER, iam, FakeTable())
        self.assertEqual(who.track, TRACK)
        self.assertEqual(who.owner, "platform-team")
        self.assertEqual(who.principal.name, USER_NAME)

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
        who = identity.resolve(ARN_USER, iam, table)
        self.assertEqual(who.track, TRACK)
        self.assertEqual(iam.tag_calls, 1)


# --------------------------------------------------------------------------
# 8. Event unpacking
# --------------------------------------------------------------------------
class TestUnpack(unittest.TestCase):
    def test_one_event_carries_many_log_lines(self):
        raws = handler.unpack(
            cloudwatch_event([log_entry(request_id="a"), log_entry(request_id="b")])
        )
        import json

        self.assertEqual([json.loads(r)["requestId"] for r in raws], ["a", "b"])

    def test_control_message_carries_no_invocations(self):
        import base64, gzip, json

        blob = gzip.compress(json.dumps({"messageType": "CONTROL_MESSAGE"}).encode())
        self.assertEqual(
            handler.unpack({"awslogs": {"data": base64.b64encode(blob).decode()}}), []
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
        unblock.clients = lambda: self._c
        unblock.NOTIFY_TOPIC_ARN = TOPIC

    def tearDown(self):
        unblock.clients = self._real_unblock_clients
        unblock.NOTIFY_TOPIC_ARN = self._real_unblock_topic
        unblock._clients.clear()
        super().tearDown()

    def _disable(self, row_topic=None):
        """Run a track past its ceiling so there is something to release."""
        c = self._c = wiring(daily_usd="0.01", enforce_flag=True)
        if row_topic is not None:
            c["budgets"].items[(f"TRACK#{TRACK}",)]["notify_topic_arn"] = row_topic
        handler.handler(cloudwatch_event([log_entry()]), None)
        return c

    def _subjects_to(self, c, topic):
        return [p["Subject"] for p in c["sns"].published if p["TopicArn"] == topic]

    def test_release_is_announced_on_the_tracks_own_topic(self):
        c = self._disable(row_topic=self.PROD_TOPIC)
        self.assertTrue(any("DISABLED" in s for s in self._subjects_to(c, self.PROD_TOPIC)))

        unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)

        on_prod = self._subjects_to(c, self.PROD_TOPIC)
        self.assertTrue(any("RE-ENABLED" in s for s in on_prod))
        # and nothing leaked to the stack default
        self.assertEqual(self._subjects_to(c, TOPIC), [])

    def test_release_falls_back_to_the_default_topic(self):
        c = self._disable()  # budget row names no topic
        unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)
        self.assertTrue(any("RE-ENABLED" in s for s in self._subjects_to(c, TOPIC)))

    def test_the_release_actually_reactivates_and_clears(self):
        c = self._disable()
        out = unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(k for _, k in c["iam"].activated), ["AKIAEXAMPLE1", "AKIAEXAMPLE2"])
        self.assertNotIn((f"TRACK#{TRACK}", "DISABLED"), c["usage"].items)

    def test_a_failed_budget_read_refuses_before_touching_anything(self):
        """Resolved first, so the keys are not reactivated and then left unannounced."""
        c = self._disable()
        c["budgets"].fail_on.add("get_item")
        with self.assertRaises(errors.BudgetUnavailable):
            unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)
        self.assertEqual(c["iam"].activated, [])
        self.assertIn((f"TRACK#{TRACK}", "DISABLED"), c["usage"].items)

    def test_re_enabling_something_not_disabled_is_refused(self):
        self._c = wiring()
        with self.assertRaises(errors.NotDisabled):
            unblock.handler({"track": TRACK, "reason": "wave stopped, loop cap added"}, None)

    def test_a_short_reason_is_refused(self):
        self._c = self._disable()
        with self.assertRaises(errors.MalformedLogEntry):
            unblock.handler({"track": TRACK, "reason": "oops"}, None)


if __name__ == "__main__":
    unittest.main()
