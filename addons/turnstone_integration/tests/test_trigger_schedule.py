"""The schedule scanner: a record fires once per window, and only when it is due.

`turnstone.trigger.schedule` (OTI-5754) is the time-based adapter. Nothing
"happens" to fire it — a cron asks every `schedule` rule which of its records are
due, and a record is due when it passes the rule's domain **and** its date field
has arrived. What these cases pin down is the pair of facts that make the window
mean something:

1. **A due record fires once.** The first tick queues a run; a second tick in the
   same window queues nothing at all, however many times it runs.
2. **A record that is not due fires nothing.** Outside the domain, or before its
   date, is not an event. The date boundary is the classic off-by-a-day, so a
   `date` field is exercised for the whole of its day.
3. **The window is what repeats, not the tick.** The next window is a new event
   and fires again — that is "remind me until it is done" — and the key that
   makes both true is `schedule:<model>,<id>:<bucket>`.

WHERE THE ASSERTION GOES

The scanner's own contract is "which runs did this tick queue", so that is what
most cases assert on (`trigger_res_id`, and one run per record-and-window). Where
a case is about the *launch* that follows, it goes through the real drain and
asserts on the mock Turnstone server's request log: "fired once" is only worth
anything if it means one prompt reached the server, and Odoo's own run table can
say one thing while the server saw two creates.

Since #5786 that launch is two-phase, so a case that asserts on the server drives
both: `_drain_queue()` commits the intent and arms the HTTP on `cr.postcommit`,
and `_deliver_pending()` is what runs it here — a `TestCursor.commit()` clears
that queue instead of running it, exactly as `_deliver_pending`'s own docstring
says. The create still has to happen exactly once; this only puts the assertion
on the right side of the transaction boundary.

WHY A FIXED `now`

`_run_due_schedules(now=...)` takes the moment "due" is judged against — one
instant for the whole tick, so a scan cannot straddle a window boundary. These
cases use that seam rather than sleeping: a test that waited for a real bucket
boundary would be a flaky test that runs for an hour. Every "inside the window"
case is anchored to the first second of a bucket (`_window_start`), so it cannot
accidentally straddle the boundary it is reasoning about.
"""

import calendar
from datetime import datetime, timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.addons.turnstone_integration.models.turnstone_trigger_schedule import (
    TurnstoneTriggerSchedule,
)

from .common import TurnstoneTransactionCase

RUN = "turnstone.automation.run"
RULE = "turnstone.automation"
RATE = "res.currency.rate"


class TriggerScheduleCase(TurnstoneTransactionCase):
    """Fixtures: a rate row with a real `date` column, and a rule that watches it.

    `res.currency.rate.name` is a `fields.Date` in `base` — a core model, no
    extra module, and the field type whose "due for the whole of its day" rule is
    the one that goes wrong. `mail.mail.scheduled_date` would cover the nullable
    case but it is a `datetime`, and this module's subject is the window.
    """

    #: Source of unique 3-character currency codes for `_currency()`. On the
    #: class, not the instance, because a `TransactionCase` rolls each test back
    #: and an instance attribute would restart at 1 every time.
    _currency_seq = 0

    def setUp(self):
        super().setUp()
        # `TransactionCase` runs every test in one transaction, so the record
        # adapter's in-transaction budget (`env.cr.cache`) survives between
        # tests. The scanner goes through `_enqueue()`, which spends from it, so
        # without this a class with more than fifty due records would start
        # silently queueing nothing.
        self.env["turnstone.trigger.record"]._forget_transaction_state()

    def _rule(self, **vals):
        defaults = {
            "name": "Schedule rule",
            "company_id": self.env.company.id,
            "agent_id": self.agent.id,
            "trigger_type": "schedule",
            "target_model_id": self.env["ir.model"]._get(RATE).id,
            "schedule_date_field": "name",
            "schedule_interval_minutes": 60,
            "prompt_template": "This rate is due.",
            "run_as": "fixed_user",
            "run_as_user_id": self.env.user.id,
        }
        defaults.update(vals)
        return self.env[RULE].create(defaults)

    def _rate(self, when=None, **vals):
        """A currency rate dated `when` (a `date`, default: today).

        ``currency_id`` may be passed to put two rates on one currency; only when
        it is not is a fresh one made, so a case that needs two records the rule
        cannot tell apart by currency does not create a third one anyway.
        """
        values = {
            "name": when or fields.Date.context_today(self.env.user),
            "rate": 1.0,
            # A currency of this test's own, so a rate here can never collide
            # with the unique(name, currency_id, company_id) constraint against
            # a row another case made on the same day.
            "currency_id": vals.pop("currency_id", None) or self._currency().id,
        }
        values.update(vals)
        return self.env[RATE].create(values)

    def _currency(self):
        """A throwaway currency, one per call.

        `base.USD` would be the obvious fixture and is the wrong one: rates are
        unique per (date, currency, company), and a second rate "today" on the
        same currency would be a constraint error that looks like a bug in the
        scanner.

        The code comes from a **class-level counter**, not from
        `search_count()`: `res.currency.name` is `size=3`, so a longer generated
        code is silently truncated (`T150` -> `T15`), and a count-derived name
        then collides across tests while looking unique in the source.
        """
        type(self)._currency_seq += 1
        return self.env["res.currency"].create({
            "name": "T%02d" % (type(self)._currency_seq % 100),
            "symbol": "T",
            "rounding": 0.01,
        })

    def _tick(self, now=None):
        """One scan. Returns the runs it queued."""
        return self.env["turnstone.trigger.schedule"]._run_due_schedules(now=now)

    def _runs_of(self, rule):
        return self.env[RUN].sudo().search([("automation_id", "=", rule.id)])

    @staticmethod
    def _bucket(rule, now):
        """The window number `now` falls into, as the adapter computes it.

        Called through the adapter rather than reimplemented: a case that
        recomputed `floor(epoch / window)` itself could agree with the test
        while disagreeing with the code, which is the one thing a key assertion
        must not do.
        """
        return TurnstoneTriggerSchedule._window_bucket(rule, now)

    @staticmethod
    def _window_start(minutes):
        """The first second of the `minutes` bucket containing now.

        Anchored rather than "now": a case that means "two ticks inside one
        window" must not straddle a boundary because the test started a second
        before one. `_window_bucket()` is `floor(epoch / window)` over **UTC**
        (`calendar.timegm`, not `datetime.timestamp()`), so this computes the
        same floor the adapter does and every offset below the window lands in
        the same bucket.
        """
        window = max(1, minutes) * 60
        now = fields.Datetime.now()
        start = calendar.timegm(now.utctimetuple()) // window * window
        return datetime(1970, 1, 1) + timedelta(seconds=start)

    def _creates(self):
        """The workstream creates the mock server received."""
        return [
            row for row in self.mock.request_log(limit=500)
            if row["path"].endswith("/workstreams/new")
        ]

    def _deliver(self):
        """Run the phase-2 delivery this transaction armed. Stands in for commit.

        #5786 split the launch: `_attempt_launch()` commits the intent and arms
        the HTTP on `cr.postcommit`, and a `TestCursor.commit()` **clears** that
        queue rather than running it. Without this every case below would see
        zero creates and quietly prove nothing about the launch.
        `_deliver_pending()` is the documented seam for a test cursor, and a
        second call in one test is a no-op because it pops the queue.
        """
        return self.env[RULE]._deliver_pending()


@tagged("post_install", "-at_install")
class TestDueRecordFiresOncePerWindow(TriggerScheduleCase):
    """Criterion 1 and 2: one record, one domain match, one window, one launch."""

    def test_a_record_matching_the_domain_and_past_its_date_fires_once(self):
        """The happy path, asserted all the way to the server's request log.

        The scanner queues and the drain launches, so "fired once" is a claim
        about the whole chain: one run, one create on the mock, one workstream.
        A second tick inside the same window must add nothing — if it did, a
        cron running every minute against a due record would deliver sixty
        prompts an hour.
        """
        rule = self._rule(schedule_interval_minutes=60)
        rate = self._rate()
        now = self._window_start(60)

        queued = self._tick(now=now)

        self.assertEqual(len(queued), 1, "the first tick fires the due record")
        self.assertEqual(queued.automation_id, rule)
        self.assertEqual(queued.trigger_res_model, RATE)
        self.assertEqual(queued.trigger_res_id, rate.id)
        self.assertEqual(
            queued.idempotency_key,
            TurnstoneTriggerSchedule.event_key(rate, self._bucket(rule, now)),
            "the key must be the record plus the window it fell into — that is "
            "what makes the second tick the same event rather than a new one",
        )

        # The drain is the second half of "fired": a queued run is an intent, and
        # the prompt reaching the server is the event.
        self.env[RUN]._drain_queue()
        self._deliver()
        self.assertEqual(
            len(self._creates()), 1,
            "one due record must produce one workstream-create on the server",
        )
        self.assertEqual(len(self.mock.workstreams()), 1)
        queued.invalidate_recordset()
        self.assertEqual(queued.state, "done", queued.error)

    def test_a_second_drain_in_the_same_bucket_fires_nothing(self):
        """Criterion 2. Several ticks inside one window, and still one launch."""
        rule = self._rule(schedule_interval_minutes=60)
        self._rate()
        now = self._window_start(60)

        self.assertEqual(len(self._tick(now=now)), 1, "the first tick fires")
        self.env[RUN]._drain_queue()
        self._deliver()
        creates_after_first = len(self._creates())
        self.assertEqual(creates_after_first, 1)

        # Every later tick of the window, each followed by a drain: nothing may
        # be queued and nothing may reach the server.
        for minute in (1, 15, 30, 59):
            later = now + timedelta(minutes=minute)
            self.assertEqual(
                self._tick(now=later).ids, [],
                "a tick at +%d minutes is the same window, so the record has "
                "already been fired for and must not queue a second run" % minute,
            )
            self.env[RUN]._drain_queue()
            self._deliver()

        self.assertEqual(
            len(self._creates()), 1,
            "a record that stays due must produce one prompt per window, not one "
            "per cron tick",
        )
        self.assertEqual(len(self.mock.workstreams()), 1)
        self.assertEqual(
            len(self._runs_of(rule)), 1,
            "one record, one window, one run — the table says so too",
        )

    def test_the_next_window_fires_again(self):
        """The other half of the window: it is a repeat, not a one-time fire."""
        rule = self._rule(schedule_interval_minutes=60)
        self._rate()
        now = self._window_start(60)

        first = self._tick(now=now)
        self.env[RUN]._drain_queue()
        self._deliver()
        later = self._tick(now=now + timedelta(minutes=60))

        self.assertEqual(len(later), 1, "the next window is a new event")
        self.assertNotEqual(later.id, first.id, "and it gets its own run")
        self.env[RUN]._drain_queue()
        self._deliver()
        self.assertEqual(
            len(self._creates()), 2,
            "a reminder that fires once and never again is not a reminder: the "
            "second window must reach the server too",
        )


@tagged("post_install", "-at_install")
class TestNotDueFiresNothing(TriggerScheduleCase):
    """Criterion 3: outside the domain, or before the date, is not an event."""

    def test_a_record_outside_the_domain_fires_nothing(self):
        """The rule's own filter narrows what is due, and nothing is launched.

        Both records share a currency and a company, so only `target_domain` can
        tell them apart — which is the point: a test that also differed in
        company would pass for the wrong reason.
        """
        currency = self._currency()
        today = fields.Date.context_today(self.env.user)
        rule = self._rule(
            target_domain="[('rate', '>', 5.0)]",
            schedule_interval_minutes=60,
        )
        inside = self._rate(when=today, currency_id=currency.id, rate=9.0)
        outside = self._rate(
            when=today - timedelta(days=1), currency_id=currency.id, rate=1.0,
        )
        self.assertNotEqual(inside.id, outside.id)

        queued = self._tick(now=self._window_start(60))

        self.assertEqual(
            queued.trigger_res_id, inside.id,
            "only the record the rule's filter accepts may fire; the other one "
            "is due by its date and still not this rule's event",
        )
        self.env[RUN]._drain_queue()
        self._deliver()
        self.assertEqual(
            len(self._creates()), 1,
            "the record outside the domain must not reach the server — one "
            "matching record, one prompt",
        )

    def test_a_record_before_its_date_fires_nothing(self):
        """A date in the future is not due, and nothing is launched for it."""
        rule = self._rule(schedule_interval_minutes=60)
        tomorrow = fields.Date.context_today(self.env.user) + timedelta(days=1)
        future = self._rate(when=tomorrow)

        queued = self._tick(now=self._window_start(60))

        self.assertEqual(
            queued.ids, [],
            "a record whose date has not arrived is not due, so the rule has no "
            "event for it",
        )
        self.env[RUN]._drain_queue()
        self._deliver()
        self.assertEqual(
            self._creates(), [],
            "nothing may be launched for a record that is not due yet",
        )
        self.assertFalse(self._runs_of(rule))
        self.assertTrue(future.exists(), "the record itself is untouched")

    def test_a_date_field_is_due_for_the_whole_of_its_day(self):
        """A task due today is due all day, not only in the day's first second.

        `_deadline()` compares a `date` column against the **end** of its day.
        Comparing it against a timestamp instead would make today's records due
        only for a cron that happened to run at 00:00:00, which is the classic
        off-by-a-day and the reason this case exists.
        """
        self._rule(schedule_interval_minutes=60)
        today = fields.Date.context_today(self.env.user)
        rate = self._rate(when=today)

        late = fields.Datetime.to_datetime(today) + timedelta(hours=23, minutes=30)
        queued = self._tick(now=late)

        self.assertEqual(
            queued.trigger_res_id, rate.id,
            "a record dated today must still be due at 23:30 of that day",
        )

    def test_a_record_with_no_date_does_not_fire(self):
        """`NULL <= now` is `NULL`: a dateless record is not due, not "due now"."""
        rule = self._rule(
            target_model_id=self.env["ir.model"]._get("mail.mail").id,
            schedule_date_field="scheduled_date",
            schedule_interval_minutes=60,
        )
        self.env["mail.mail"].create({
            "subject": "No scheduled date",
            "email_from": "sender@example.com",
            "email_to": "recipient@example.com",
        })

        self.assertEqual(self._tick(now=self._window_start(60)).ids, [])
        self.env[RUN]._drain_queue()
        self._deliver()
        self.assertEqual(self._creates(), [])
        self.assertFalse(self._runs_of(rule))

    def test_a_rule_with_no_date_field_fires_nothing(self):
        """Guessing at a field would be worse than doing nothing."""
        rule = self._rule(schedule_date_field="")
        self._rate()

        self.assertEqual(self._tick(now=self._window_start(60)).ids, [])
        self.env[RUN]._drain_queue()
        self._deliver()
        self.assertEqual(self._creates(), [])
        self.assertFalse(self._runs_of(rule))


@tagged("post_install", "-at_install")
class TestWindowIsTheUnitsOfRepetition(TriggerScheduleCase):
    """The bucket, in the two shapes a deployment configures it in."""

    def test_a_zero_window_fires_once_and_never_again(self):
        """`0` means "something became due": one prompt, ever.

        The key is then the record alone (`bucket = 0`), so no later tick can be
        a new event. The alternative — a bucket per tick — would be a rule that
        fires sixty times an hour forever for one record.
        """
        rule = self._rule(schedule_interval_minutes=0)
        rate = self._rate()
        now = self._window_start(60)

        self.assertEqual(len(self._tick(now=now)), 1)
        self.env[RUN]._drain_queue()
        self._deliver()
        creates = len(self._creates())

        for hours in (1, 24, 24 * 30):
            self.assertEqual(
                self._tick(now=now + timedelta(hours=hours)).ids, [],
                "with no window the record has already been handled",
            )
            self.env[RUN]._drain_queue()
            self._deliver()

        self.assertEqual(len(self._creates()), creates)
        self.assertEqual(len(self._runs_of(rule)), 1)
        self.assertEqual(
            self._runs_of(rule).idempotency_key,
            TurnstoneTriggerSchedule.event_key(rate, 0),
            "the no-window key is the record alone, so it can never be a new "
            "event on a later tick",
        )

    def test_the_bucket_uses_the_rules_window_not_the_cron_interval(self):
        """A weekly rule must be one window even when the scan runs every minute.

        If the bucket were computed from the cron's cadence, a rule asking to be
        reminded weekly would fire every minute — the setting would be
        decorative.

        The record is dated **before** the weekly anchor on purpose: the anchor
        is the first second of a seven-day bucket, so it can be up to a week in
        the past, and a record dated today is legitimately not due yet at a
        moment before its own date. Dating it relative to the anchor is what
        keeps the case about the bucket rather than about the date comparison.
        """
        rule = self._rule(schedule_interval_minutes=7 * 24 * 60)
        now = self._window_start(7 * 24 * 60)
        self._rate(when=fields.Date.to_date(now) - timedelta(days=1))

        self.assertEqual(len(self._tick(now=now)), 1)
        self.assertEqual(
            self._tick(now=now + timedelta(days=1)).ids, [],
            "one day into a weekly window is still the same window",
        )
        self.env[RUN]._drain_queue()
        self._deliver()
        self.assertEqual(len(self._creates()), 1)

    def test_two_ticks_a_second_apart_share_a_bucket(self):
        """The floor, measured across the value the adapter actually computes.

        A duration-derived key ("how long has this been idle") would change on
        every tick and the window would not exist at all; a wall-clock bucket is
        what makes two ticks one event. This pins the property directly, so a
        later "improvement" to the key has to argue with a test that says why it
        is like this.
        """
        rule = self._rule(schedule_interval_minutes=60)
        now = self._window_start(60)

        self.assertEqual(
            TurnstoneTriggerSchedule._window_bucket(rule, now),
            TurnstoneTriggerSchedule._window_bucket(
                rule, now + timedelta(seconds=1),
            ),
            "two ticks a second apart are one window and must compute one key",
        )
        self.assertNotEqual(
            TurnstoneTriggerSchedule._window_bucket(
                rule, now + timedelta(minutes=60),
            ),
            TurnstoneTriggerSchedule._window_bucket(rule, now),
            "the next window must be a different bucket, or nothing ever fires "
            "twice",
        )
