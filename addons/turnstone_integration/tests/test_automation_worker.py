"""The run worker and the time-based trigger: drain once, fire once per window.

Two adapters are tested here, and they are the two halves of OTI-5754:

* **The drain** (`turnstone.automation.run._drain_queue()`), which is the cron
  that turns the `queued` intent every other adapter writes into a `done` or
  `failed` run. Criterion 1 is "drains the queue exactly once per run".
* **The schedule scanner** (`turnstone.trigger.schedule`), which selects the
  records a `schedule` rule is due for by its domain and a date field. Criterion
  2 is "fires once per window per record".

Criterion 3 — "two concurrent cron workers do not double-process a run" — is
**not** tested here, and cannot be: a `TransactionCase` hands out `TestCursor`s
that share one connection behind a re-entrant lock, so two test cursors are never
in flight at once and a concurrency test written this way passes whatever the
code does. It lives in `scripts/check-concurrency.py` T11, which opens two real
connections. What *is* tested here is the two walls `_claim_for_drain()` is built
from, each in isolation: a run that is no longer `queued` is never claimed, and a
transaction that already holds the lock does not care (the lock is re-entrant
within a transaction, which is what makes a nested caller safe).

Everything here is asserted on what the launch actually did, and the launch's own
tests (#5749) assert on the mock server's state. Here the seam is
`_attempt_launch()`, which is the one implementation of "what a launch does" and
is reached through the real `_drain_queue()` -> `_claim_for_drain()` path; it is
patched only where a case is about *which* run was reached rather than what the
launch sent, so a stub cannot hide a claim bug.
"""

import calendar
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.turnstone_integration.models.turnstone_automation_run import (
    DRAIN_BATCH_SIZE,
    DRAIN_INTERVAL_MINUTES,
    DRAIN_INTERVAL_PARAM,
    MAX_DRAIN_ATTEMPTS,
)
from odoo.addons.turnstone_integration.models.turnstone_trigger_schedule import (
    TurnstoneTriggerSchedule,
)

RUN = "turnstone.automation.run"
RULE = "turnstone.automation"
PARTNER = "res.partner"


class AutomationWorkerCase(TransactionCase):
    """Fixtures: a rule, its agent, and a patched launch that reports itself.

    No mock server. The drain's contract is "which run did this worker hand to
    the launch service", and a real launch would need a server to talk to
    without testing the server. Where a case does care about a real outcome, it
    asserts on the run's state, which `_attempt_launch`'s real failure paths
    write.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Automation worker (no network)",
            "base_url": "http://127.0.0.1:1",
            "api_token": "irrelevant-this-ticket-asserts-on-claims",
            "is_default": True,
        })
        cls.agent = cls.env["turnstone.agent"].create({
            "name": "Worker Agent",
            "connection_id": cls.connection.id,
            "kind": "interactive",
        })
        cls.partner_model = cls.env["ir.model"]._get(PARTNER)

    def setUp(self):
        super().setUp()
        self.calls = []

    # -- fixtures ----------------------------------------------------------

    def _rule(self, **vals):
        suffix = vals.pop("suffix", "a")
        defaults = {
            "name": "Worker rule %s" % suffix,
            "company_id": self.company.id,
            "agent_id": self.agent.id,
            # `manual` on purpose: the drain cases create runs by hand, and a
            # rule with a live trigger would have an adapter queue extra runs
            # behind them from the fixtures' own writes, turning "how many runs
            # did the drain reach" into a question about the adapter.
            "trigger_type": "manual",
            "target_model_id": self.partner_model.id,
            "prompt_template": "Look at {{ name }}.",
            "run_as": "fixed_user",
            "run_as_user_id": self.env.user.id,
        }
        defaults.update(vals)
        return self.env[RULE].create(defaults)

    def _queued_run(self, rule, key="event:1", record=None):
        return self.env[RUN].sudo().create({
            "automation_id": rule.id,
            "idempotency_key": key,
            "trigger_res_model": record._name if record else PARTNER,
            "trigger_res_id": record.id if record else 1,
        })

    def _partner(self, **vals):
        values = {"name": "Worker Partner"}
        values.update(vals)
        return self.env[PARTNER].create(values)

    def _patch_launch(self, side_effect=None):
        """Replace `_attempt_launch` with a recorder, for claim-only cases.

        The real method is captured first: a stub that hid a claim bug would
        make every test here pass, and the claim is the thing under test.

        `side_effect(run)` may move the run, and its return value is ignored.
        """
        original = type(self.env[RULE])._attempt_launch

        # Same signature as `_attempt_launch(self, run, record, triggering_user,
        # prompt)` -- patched onto the class, so `self` arrives first.
        def fake(self_rule, run, record, triggering_user, prompt):
            self.calls.append({
                "rule": self_rule.id, "run": run.id,
                "record": record.id if record else None,
                "prompt": prompt,
            })
            if side_effect:
                side_effect(run)
            return run

        return patch.object(type(self.env[RULE]), "_attempt_launch", fake), original


@tagged("post_install", "-at_install")
class TestDrainClaimsOnce(AutomationWorkerCase):
    """Criterion 1: the queue drains, and no run is reached twice."""

    def test_a_queued_run_is_launched_once(self):
        rule = self._rule()
        run = self._queued_run(rule)
        patcher, _original = self._patch_launch(
            lambda r: r._mark_running()._mark_done()
        )
        with patcher:
            self.env[RUN]._drain_queue()
            # A second tick, immediately: the queue is now empty, and a worker
            # that re-read its batch from its own cache would launch it again.
            self.env[RUN]._drain_queue()
        self.assertEqual(
            [call["run"] for call in self.calls], [run.id],
            "one queued run must be handed to the launch service exactly once, "
            "however many cron ticks run",
        )

    def test_only_queued_runs_are_drained(self):
        """A run that is already `running` or finished is somebody else's."""
        rule = self._rule()
        running = self._queued_run(rule, key="event:running")
        running._mark_running()
        done = self._queued_run(rule, key="event:done")
        done._mark_running()._mark_done()
        failed = self._queued_run(rule, key="event:failed")
        failed._mark_running()._mark_failed("somewhere in the network")
        queued = self._queued_run(rule, key="event:queued")

        patcher, _original = self._patch_launch(
            lambda r: r._mark_running()._mark_done()
        )
        with patcher:
            self.env[RUN]._drain_queue()

        self.assertEqual(
            [call["run"] for call in self.calls], [queued.id],
            "the drain must pick only `queued` runs: a `running` one is owned by "
            "another worker and a finished one is history",
        )

    def test_a_run_that_another_worker_claimed_is_skipped(self):
        """The state wall on its own: the lock is held, the row is not queued.

        `_claim_for_drain()` has two walls — the advisory lock and a re-read of
        the state from the database — and either one alone must refuse. This
        simulates the loser of a real race: it won the lock (there is no other
        transaction here), and the row it then reads has already moved.
        """
        rule = self._rule()
        run = self._queued_run(rule)
        patcher, _original = self._patch_launch(
            lambda r: r._mark_running()._mark_done()
        )
        with patcher:
            self.env[RUN]._drain_queue()
            # Refill the queue the way a concurrent worker's own commit would be
            # seen by the next read, and drain again.
            self.assertEqual(len(self.calls), 1)
            self.env[RUN]._drain_queue()
        self.assertEqual(len(self.calls), 1, "a finished run is never re-launched")

    def test_a_run_is_claimed_by_the_lock_it_takes(self):
        """The lock is transaction-scoped, so taking it twice in one is safe.

        `pg_try_advisory_xact_lock` is re-entrant within a transaction. That is
        what makes a nested caller (a drain that is somehow reached twice in one
        request) work rather than deadlock against itself — and it is why the
        lock cannot be the only wall, which the state re-read provides.
        """
        rule = self._rule()
        run = self._queued_run(rule)
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (0x54534452, run.id),
        )
        self.assertTrue(self.env.cr.fetchone()[0])
        self.assertTrue(
            self.env[RUN]._claim_for_drain(run),
            "a transaction that already holds its own lock may still claim",
        )

    def test_the_batch_is_bounded(self):
        rule = self._rule()
        for index in range(DRAIN_BATCH_SIZE + 5):
            self._queued_run(rule, key="event:%s" % index)
        patcher, _original = self._patch_launch(
            lambda r: r._mark_running()._mark_done()
        )
        with patcher:
            self.env[RUN]._drain_queue()
        self.assertEqual(
            len(self.calls), DRAIN_BATCH_SIZE,
            "one tick must not attempt more runs than the batch ceiling; the "
            "rest are the next tick's, which is a minute away",
        )

    def test_a_launch_failure_lands_on_failed_and_says_why(self):
        """The real `_attempt_launch`, with a rule that cannot resolve a user.

        A rule set to "Whoever triggered it" has no user in a cron, and the rule
        model refuses rather than falling back to the superuser. The run must
        record that: `failed`, no attempt counted, and the rule's own words.
        """
        rule = self._rule(
            run_as="triggering_user", run_as_user_id=False, suffix="b",
        )
        run = self._queued_run(rule)
        self.env[RUN]._drain_queue()

        self.assertEqual(run.state, "failed")
        self.assertEqual(
            run.attempt_count, 0,
            "a refusal before anything is tried must not be counted as an attempt",
        )
        self.assertIn(
            "Run As", run.error,
            "the run must explain that the rule needs a run-as user, not that a "
            "network call failed",
        )

    def test_a_run_over_the_attempt_ceiling_is_left_alone(self):
        rule = self._rule()
        run = self._queued_run(rule)
        # An attempt count at the ceiling with the run still queued cannot happen
        # through `_mark_*` (a counted attempt moves the run to `running`), so it
        # is written directly: it stands in for a row left behind by an earlier
        # worker that crashed between counting and moving.
        self.env.cr.execute(
            """UPDATE turnstone_automation_run
                  SET attempt_count = %s
                WHERE id = %s""",
            [MAX_DRAIN_ATTEMPTS, run.id],
        )
        run.invalidate_recordset()
        patcher, _original = self._patch_launch(
            lambda r: r._mark_running()._mark_done()
        )
        with patcher:
            self.env[RUN]._drain_queue()
        self.assertEqual(
            self.calls, [],
            "a run already attempted to the ceiling must not be attempted again "
            "forever; leaving it with its reason is the answer",
        )

    def test_an_empty_queue_does_nothing(self):
        patcher, _original = self._patch_launch()
        with patcher:
            runs = self.env[RUN]._drain_queue()
        self.assertFalse(runs)
        self.assertEqual(self.calls, [])

    def test_the_drain_resolves_the_trigger_record(self):
        """The record is rebuilt from the run's plain-text model and id."""
        rule = self._rule()
        partner = self._partner()
        self._queued_run(rule, record=partner)
        patcher, _original = self._patch_launch(
            lambda r: r._mark_running()._mark_done()
        )
        with patcher:
            self.env[RUN]._drain_queue()
        self.assertEqual(self.calls[0]["record"], partner.id)

    def test_a_deleted_trigger_record_does_not_stop_the_drain(self):
        """A message can outlive the record it was posted on."""
        rule = self._rule()
        partner = self._partner(name="Doomed Party")
        run = self._queued_run(rule, record=partner)
        self.env.cr.execute("DELETE FROM res_partner WHERE id = %s", [partner.id])
        patcher, _original = self._patch_launch(
            lambda r: r._mark_running()._mark_done()
        )
        with patcher:
            self.env[RUN]._drain_queue()
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(
            self.calls[0]["record"],
            "a record that is gone must answer \"nothing\", not raise into the "
            "batch the other runs need",
        )


@tagged("post_install", "-at_install")
class TestDrainInterval(AutomationWorkerCase):
    """The cron's frequency is a setting, and the setting reaches the cron row."""

    def test_the_default_is_one_minute(self):
        self.env["ir.config_parameter"].sudo().set_param(DRAIN_INTERVAL_PARAM, "")
        self.assertEqual(
            self.env[RUN]._drain_interval_minutes(), DRAIN_INTERVAL_MINUTES,
        )

    def test_a_configured_interval_is_read(self):
        self.env["ir.config_parameter"].sudo().set_param(DRAIN_INTERVAL_PARAM, "7")
        self.assertEqual(self.env[RUN]._drain_interval_minutes(), 7)

    def test_a_nonsense_value_falls_back_rather_than_stopping_the_queue(self):
        """A bad setting must not be able to stop runs from launching."""
        for raw in ("seven", "-3", "0", " ", None):
            self.env["ir.config_parameter"].sudo().set_param(
                DRAIN_INTERVAL_PARAM, raw or "",
            )
            self.assertEqual(
                self.env[RUN]._drain_interval_minutes(), DRAIN_INTERVAL_MINUTES,
                "%r must fall back to the default, not raise out of the cron"
                % (raw,),
            )

    def test_the_parameter_is_mirrored_onto_the_cron_row(self):
        """`ir.config_parameter` alone would change nothing the scheduler reads."""
        cron = self.env.ref("turnstone_integration.ir_cron_turnstone_drain_runs")
        self.env["ir.config_parameter"].sudo().set_param(DRAIN_INTERVAL_PARAM, "4")
        self.env[RUN]._sync_drain_interval()
        self.assertEqual(cron.interval_number, 4)
        self.assertEqual(cron.interval_type, "minutes")

    def test_saving_the_setting_pushes_it_onto_the_cron(self):
        cron = self.env.ref("turnstone_integration.ir_cron_turnstone_drain_runs")
        settings = self.env["res.config.settings"].create({
            "turnstone_drain_interval_minutes": 9,
        })
        settings.set_values()
        self.assertEqual(cron.interval_number, 9)


@tagged("post_install", "-at_install")
class TestTimeBasedTrigger(AutomationWorkerCase):
    """Criterion 2: a record fires once per window, and never twice in one."""

    def _rate(self, **vals):
        """A record with a real `date` field, from a module this addon requires.

        `res.currency.rate.name` is a `fields.Date` in `base`, which is the type
        `_deadline()` has to treat as "due for the whole of its day". A core model
        with a plain date column, so no extra module and no constraint games —
        unlike `project.task` (a module nobody asked for) or `mail.activity`
        (whose `res_model`/`res_id` pair must both name a live document).
        """
        values = {
            "name": fields.Date.context_today(self.env.user),
            "rate": 1.0,
            "currency_id": self.env.ref("base.USD").id,
        }
        values.update(vals)
        return self.env["res.currency.rate"].create(values)

    def _rate_rule(self, **vals):
        defaults = {
            "suffix": "rate",
            "trigger_type": "schedule",
            "target_model_id": self.env["ir.model"]._get("res.currency.rate").id,
            "schedule_date_field": "name",
            "schedule_interval_minutes": 60,
            "prompt_template": "This rate is due.",
        }
        defaults.update(vals)
        return self._rule(**defaults)

    def _idle_rule(self, **vals):
        """A `datetime` schedule: "this record has not been touched since X"."""
        defaults = {
            "suffix": "idle",
            "trigger_type": "schedule",
            "target_model_id": self.partner_model.id,
            "schedule_date_field": "write_date",
            "schedule_interval_minutes": 60,
            "prompt_template": "This record has been idle.",
        }
        defaults.update(vals)
        return self._rule(**defaults)

    def _fire(self, now=None):
        return self.env["turnstone.trigger.schedule"]._run_due_schedules(now=now)

    @staticmethod
    def _window_start(minutes):
        """A moment at the very start of a `minutes` window.

        Anchored rather than "now": a case that means "three ticks inside one
        window" must not accidentally straddle a boundary because the test began
        59 seconds before one. The bucket is a floor of `now / window`, so a
        multiple of the window is the first second of a window, and every offset
        below the window lands inside it.
        """
        now = fields.Datetime.now()
        window = max(1, minutes) * 60
        # UTC, like the adapter: a naive Odoo datetime is UTC, and `timestamp()`
        # would read it as local time.
        start = calendar.timegm(now.utctimetuple()) // window * window
        return datetime(1970, 1, 1) + timedelta(seconds=start)

    def _runs_of(self, rule):
        return self.env[RUN].sudo().search([("automation_id", "=", rule.id)])

    # -- the happy path -----------------------------------------------------

    def test_a_due_record_queues_one_run(self):
        rule = self._rate_rule()
        activity = self._rate()

        runs = self._fire()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs.automation_id, rule)
        self.assertEqual(runs.state, "queued", "the scanner queues; the drain launches")
        self.assertEqual(runs.trigger_res_model, "res.currency.rate")
        self.assertEqual(runs.trigger_res_id, activity.id)

    def test_a_record_that_is_not_due_yet_does_not_fire(self):
        self._rate_rule()
        self._rate(
            name=fields.Date.context_today(self.env.user) + timedelta(days=2),
        )
        self.assertEqual(self._fire(), self.env[RUN])

    def test_the_domain_narrows_the_records_that_fire(self):
        today = fields.Date.context_today(self.env.user)
        rule = self._rate_rule(target_domain="[('name', '=', '%s')]" % today)
        self._rate(name=fields.Date.context_today(self.env.user))
        self._rate(name=fields.Date.context_today(self.env.user) - timedelta(days=1))
        runs = self._fire()
        self.assertEqual(
            len(runs), 1,
            "a record outside the rule's filter must not fire it, even when its "
            "date is due",
        )
        self.assertEqual(runs.automation_id, rule)

    def test_a_record_with_no_date_does_not_fire(self):
        """`NULL <= now()` is `NULL`, so a dateless record must not be due.

        `mail.mail.scheduled_date` is a nullable `datetime`, which is the shape
        a real "not scheduled yet" record has. A required date column (like
        `res.currency.rate.name`) can never be empty, so it cannot exercise this
        at all.
        """
        rule = self._rule(
            suffix="nullable",
            trigger_type="schedule",
            target_model_id=self.env["ir.model"]._get("mail.mail").id,
            schedule_date_field="scheduled_date",
            prompt_template="This mail is due.",
        )
        self.env["mail.mail"].create({
            "subject": "No scheduled date",
            "email_from": "sender@example.com",
            "email_to": "recipient@example.com",
        })
        self.assertEqual(self._fire(), self.env[RUN])
        self.assertFalse(
            self._runs_of(rule),
            "a record with no date is not due, and must not be selected by a "
            "comparison against null",
        )

    def test_a_datetime_field_is_judged_against_now(self):
        """The other field type: `write_date` on a partner.

        `write_date` is shared by every partner, so the assertion is that this
        rule fired at all and that the record the test just made is among the
        runs — not that it was the only one, which would be a claim about the
        fixtures rather than about the adapter.
        """
        rule = self._idle_rule()
        partner = self.env[PARTNER].create({"name": "Idle Lead"})

        runs = self._fire()
        self.assertTrue(runs, "a datetime field in the past is due now")
        self.assertIn(
            partner.id, runs.filtered(
                lambda r: r.trigger_res_model == PARTNER
            ).mapped("trigger_res_id"),
        )
        self.assertEqual(set(runs.automation_id.ids), {rule.id})

    # -- the window --------------------------------------------------------

    def test_a_record_fires_once_per_window(self):
        """The criterion. Several ticks inside one window, one run."""
        rule = self._rate_rule(schedule_interval_minutes=60)
        record = self._rate()
        now = self._window_start(60)

        first = self._fire(now=now)
        self.assertEqual(len(first), 1, "the first tick fires")
        for minute in (1, 15, 59):
            self._fire(now=now + timedelta(minutes=minute))
            # The scanner queues what is new and nothing else, so a tick inside
            # a window that has already fired adds no run. What matters is the
            # table: one record, one window, one run.
            self.assertEqual(
                len(self._runs_of(rule)), 1,
                "a tick inside the window must not queue a second run for a "
                "record that already has one",
            )
        runs = self._runs_of(rule)
        self.assertEqual(runs.trigger_res_id, record.id)
        self.assertEqual(
            runs.idempotency_key,
            TurnstoneTriggerSchedule.event_key(record, self._bucket(rule, now)),
            "the key must be the record plus the window it fell into",
        )

    def _bucket(self, rule, now):
        return TurnstoneTriggerSchedule._window_bucket(rule, now)

    def test_the_next_window_fires_again(self):
        rule = self._rate_rule(schedule_interval_minutes=60)
        activity_id = self._rate()
        now = self._window_start(60)

        first = self._fire(now=now)
        later = self._fire(now=now + timedelta(minutes=61))
        self.assertEqual(len(later), 1)
        self.assertNotEqual(
            later.id, first.id,
            "a later window is a genuinely new event and must get its own run — "
            "that is what 'remind me until it is done' needs",
        )
        self.assertEqual(len(self._runs_of(rule)), 2)

    def test_a_zero_window_fires_once_and_never_again(self):
        """0 means "something became due", so the key is the record alone."""
        rule = self._rate_rule(schedule_interval_minutes=0)
        self._rate()
        now = fields.Datetime.now()

        first = self._fire(now=now)
        self.assertEqual(len(first), 1)
        for hours in (1, 24, 24 * 30):
            self._fire(now=now + timedelta(hours=hours))
            self.assertEqual(
                len(self._runs_of(rule)), 1,
                "with no window the record has already been handled; a second "
                "run would be a rule that fires forever",
            )
        self.assertEqual(self._runs_of(rule), first)

    def test_the_bucket_is_a_wall_clock_number_not_a_duration(self):
        """Two ticks in one window must compute the same key, whatever else moved."""
        rule = self._rate_rule(schedule_interval_minutes=60)
        now = self._window_start(60)
        first = TurnstoneTriggerSchedule._window_bucket(rule, now)
        second = TurnstoneTriggerSchedule._window_bucket(
            rule, now + timedelta(seconds=1),
        )
        self.assertEqual(
            first, second,
            "an idle-duration key would change on every tick and the window "
            "would not exist",
        )
        self.assertNotEqual(
            TurnstoneTriggerSchedule._window_bucket(
                rule, now + timedelta(minutes=61),
            ),
            first,
        )

    def test_the_window_is_the_rules_not_the_crons(self):
        """A weekly rule must exist even when the cron runs every minute."""
        rule = self._rate_rule(schedule_interval_minutes=7 * 24 * 60)
        now = self._window_start(7 * 24 * 60)
        self.assertEqual(
            TurnstoneTriggerSchedule._window_bucket(rule, now),
            TurnstoneTriggerSchedule._window_bucket(rule, now + timedelta(days=1)),
            "one day into a weekly window is still the same window",
        )

    def test_two_rules_fire_independently_for_one_record(self):
        """The key is unique *per automation*, so two rules each get their run."""
        first_rule = self._rate_rule(suffix="one")
        second_rule = self._rate_rule(suffix="two")
        self._rate()
        runs = self._fire()
        self.assertEqual(len(runs), 2)
        self.assertEqual(set(runs.automation_id.ids), {first_rule.id, second_rule.id})

    # -- date vs datetime --------------------------------------------------

    def test_a_date_field_is_due_for_the_whole_of_its_day(self):
        """A task due today is due all day, not only from midnight."""
        self._rate_rule(schedule_interval_minutes=0)
        today = fields.Date.context_today(self.env.user)
        self._rate(name=today)

        # Late in the same day: still due. Comparing a date against a timestamp
        # would make today's records due only in the first second of the day.
        late = fields.Datetime.to_datetime(today) + timedelta(hours=23, minutes=30)
        self.assertEqual(len(self._fire(now=late)), 1)

    def test_a_date_field_tomorrow_is_not_due_today(self):
        self._rate_rule()
        self._rate(
            name=fields.Date.context_today(self.env.user) + timedelta(days=1),
        )
        self.assertEqual(self._fire(), self.env[RUN])

    # -- the ceiling -------------------------------------------------------

    def test_the_ceiling_leaves_the_rest_due_for_the_next_tick(self):
        """Records over the ceiling are taken next tick, not dropped."""
        today = fields.Date.context_today(self.env.user)
        rule = self._rate_rule(schedule_limit=2)
        for index in range(4):
            self._rate(name=today - timedelta(days=index))

        first = self._fire()
        self.assertEqual(len(first), 2, "the ceiling bounds one tick")
        second = self._fire()
        self.assertEqual(
            len(second), 2,
            "the records over the ceiling must still be due on the next tick; "
            "dropping them would be a rule that silently stops",
        )
        self.assertFalse(
            set(first.ids) & set(second.ids),
            "the second tick must take the ones the first did not",
        )
        self.assertEqual(len(self._runs_of(rule)), 4)

    # -- configuration -----------------------------------------------------

    def test_a_schedule_with_no_date_field_fires_nothing(self):
        """Guessing at a field would be worse than doing nothing."""
        self._rate_rule(schedule_date_field="")
        self._rate()
        self.assertEqual(self._fire(), self.env[RUN])

    def test_a_field_that_is_not_a_date_is_refused_at_save_time(self):
        with self.assertRaises(ValidationError):
            self._rate_rule(schedule_date_field="rate")

    def test_a_field_that_does_not_exist_is_refused_at_save_time(self):
        """The ordinary way this goes wrong is a typo, and it must be a form error."""
        with self.assertRaises(ValidationError):
            self._rate_rule(schedule_date_field="no_such_field")

    def test_a_negative_window_is_refused(self):
        with self.assertRaises(ValidationError):
            self._rate_rule(schedule_interval_minutes=-5)

    def test_a_broken_rule_does_not_stop_the_others(self):
        """One bad schedule must not disable every good one."""
        broken = self._rate_rule(suffix="broken")
        working = self._rate_rule(suffix="working")
        self._rate()

        original = type(broken)._eval_target_domain

        def eval_domain(self):
            if self.id == broken.id:
                raise ValueError("a field this domain names no longer exists")
            return original(self)

        with patch.object(type(broken), "_eval_target_domain", eval_domain):
            runs = self._fire()
        self.assertEqual(
            len(runs), 1,
            "the working rule must still fire when another rule's domain raises",
        )
        self.assertEqual(runs.automation_id, working)

    def test_the_due_count_reports_without_writing(self):
        rule = self._rate_rule()
        self._rate()
        count = self.env["turnstone.trigger.schedule"]._due_count(rule)
        self.assertEqual(count, 1)
        self.assertEqual(len(self._runs_of(rule)), 0)
        self.env["turnstone.trigger.schedule"]._due_count(rule)
        self.assertEqual(
            len(self._runs_of(rule)), 0,
            "counting what is due must not queue anything",
        )



class TestScheduleThenDrain(AutomationWorkerCase):
    """The two halves together: a scan queues, and the drain launches what it queued."""

    def test_a_scanned_run_is_drained(self):
        rule = self._rule(
            suffix="endtoend",
            trigger_type="schedule",
            target_model_id=self.env["ir.model"]._get("res.currency.rate").id,
            schedule_date_field="name",
            schedule_interval_minutes=60,
            prompt_template="This rate is due.",
        )
        self.env["res.currency.rate"].create({
            "name": fields.Date.context_today(self.env.user),
            "rate": 1.0,
            "currency_id": self.env.ref("base.USD").id,
        })
        queued = self.env["turnstone.trigger.schedule"]._run_due_schedules()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued.state, "queued")

        calls = []

        def fake(self_rule, run, record, triggering_user, prompt):
            calls.append(run.id)
            return run._mark_running()._mark_done()

        with patch.object(type(self.env[RULE]), "_attempt_launch", fake):
            self.env[RUN]._drain_queue()
        self.assertEqual(
            calls, queued.ids, "the drain must pick up what the scan queued",
        )
        self.assertEqual(queued.state, "done")
        self.assertEqual(rule.run_count, 0, "the stub never reached `_note_run`")
