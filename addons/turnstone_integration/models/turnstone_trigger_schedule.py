"""The time-based adapter: a record is due, so a rule may want to run on it.

WHY THIS EXISTS

`turnstone.trigger.record` (#5752) notices a record being created or changed.
`turnstone.trigger.chatter` (#5753) notices a customer writing. Both are
*events*: something happened, and the rule hears about it. Neither can answer
the other half of what an automation is for — "this lead has been idle three
days", "this task is due today", "this invoice is a week overdue" — because
nothing *happens*. The passage of time is not an event; it is a comparison
between a date on a record and the clock.

So this adapter is not a hook. It is a **scan**, run by the drain cron (#5754),
which asks every `schedule` rule the same question: which of your records are
due now, and which of those have you not already fired for?

WHAT "DUE" MEANS, AND WHY IT IS A DOMAIN PLUS A FIELD

A rule says `target_domain` (which records it is about) and
`schedule_date_field` (which date decides when one is due). A record is due when
both hold: it passes the domain, and its date field is at or before now.

Both halves are administrator-written configuration, and the date field is
*validated at save time* against the target model (`_check_schedule_fields`), so
"due by a field that does not exist" is a form error rather than a cron that
silently selects nothing. An empty date field is allowed and selects nothing —
guessing at one would be worse than doing nothing.

A `date` field (no time) is compared as the end of its day. A task due
"2026-11-28" is due for the whole of that day, not from midnight; comparing a
date against a datetime otherwise makes every record due at 00:00:00 and
"today's" tasks would only fire for someone whose cron happened to run in the
first second.

A datetime field is compared against now. The field is also required to be
non-null in the domain: a record with no due date is not due, and in SQL
`NULL <= now()` is `NULL`, which a domain would silently drop anyway — saying
it in the domain makes that explicit rather than incidental.

THE IDEMPOTENCY KEY IS THE WINDOW, AND THAT IS THE WHOLE DESIGN

Criterion 2 of this ticket is "a time-based automation fires once per record per
window", and a cron that runs every minute against a record whose due date does
not move would otherwise fire sixty times an hour. So the event identity is
**the record plus the window it fell into**:

    schedule:<model>,<id>:<bucket>

where `bucket = floor(now / window)`. Two consequences, both wanted:

* A cron that runs twice in the same window — two workers, a retried job, a
  tick that arrives early — computes the same bucket, so the second one presents
  the *same* key and `unique(automation_id, idempotency_key)` answers it with
  the run the first one queued. Nothing fires twice. This is why the key is a
  wall-clock bucket and not, say, "how long has it been idle": an idle-duration
  key changes continuously, so every tick would be a new event and the window
  would not exist.
* The next window is a genuinely new event, and gets a new run — which is what
  "remind me every day until it is done" needs.

With `schedule_interval_minutes == 0` there is no window: the bucket is a
constant (`0`), so the key is the record alone and the rule fires for it exactly
once, ever. That is the right default for "something became due", and the wrong
one for a reminder — which is why the field's help says so.

**The bucket is computed from the rule's window, not the cron's interval.** If
they were the same number a rule could not ask to be reminded weekly while the
cron runs every minute, which is the ordinary case.

QUEUE, NEVER LAUNCH — THE SAME SEAM AS EVERY OTHER ADAPTER

This scan runs inside the cron worker's transaction, and the launch service is
an HTTP call. So the adapter's only write is a `turnstone.automation.run` row in
state `queued`; the drain then picks it up in the same tick and launches it
through `_attempt_launch()`. Reusing `turnstone.trigger.record._enqueue()` keeps
the race-safe insert, the company wall and the rule's domain one implementation,
so a schedule run and a record run mean the same thing by "this record".

THE BOUND

`schedule_limit` caps how many records one rule fires for per tick. A due-date
field on a busy model can select thousands of rows, and every one of them is an
agent session with a 120-second HTTP call behind it. Records over the ceiling
are *not dropped* — they are still due, and the next tick selects them again
(the ordering is oldest-due first, so nothing starves). The warning names the
rule and the ceiling, once per rule per tick, because a silent cap looks like a
rule that works for the first fifty records and then stops.

WHAT IS STILL NOT COVERED

A schedule only ever *fires*; it never notices a record becoming *not* due. A
record whose date field is pushed into the future by an agent's own work simply
stops matching the domain, which is the correct behaviour and needs no code.

Nothing here runs on a user's request, so nothing here may raise into one: the
scan is guarded per rule, and a broken domain on one rule cannot stop the
others.
"""

import calendar
import logging

from odoo import api, fields, models
from odoo.fields import Domain

_logger = logging.getLogger(__name__)

# The one trigger value this adapter answers, defined here rather than imported
# from the rule model, for the reason #5753 gives: importing a *constant* across
# modules would make import order matter for a value that never changes.
SCHEDULE_TRIGGERS = ("schedule",)

# The rule model, and the fields of it that decide whether this adapter has
# anything to do.
RULE_MODEL = "turnstone.automation"

# A `date` field is due for the whole of its day, so the comparison uses the end
# of the day: 23:59:59 rather than 00:00:00. Comparing a date against a
# timestamp otherwise makes "today's" records due only for a cron that runs in
# the first second of the day.
DATE_END = " 23:59:59"


class TurnstoneTriggerSchedule(models.AbstractModel):
    """Select the records a `schedule` rule is due to fire for, and queue them.

    An `AbstractModel` for the same reason as #5752 and #5753: there is no row to
    keep, no table, no access rules and no menu — a bag of behaviour the cron
    calls.
    """

    _name = "turnstone.trigger.schedule"
    _description = "Turnstone Schedule Trigger"

    # -- the entry point the cron calls -------------------------------------

    @api.model
    def _run_due_schedules(self, now=None):
        """Fire every `schedule` rule that has due records, and return the runs.

        ``now``
            The moment "due" is judged against. A parameter rather than
            `fields.Datetime.now()` inside the loop so a tick judges every rule
            against one instant — a scan that crossed a window boundary halfway
            through would put two rules of the same window in different buckets
            — and so a test can place a record on either side of the line
            without waiting for a clock.

        Each rule is handled in its own guard. An administrator's broken domain
        on one schedule must not stop the other schedules from firing: a single
        try around the whole loop would make one bad rule disable every good
        one, which is a far worse failure than the broken rule itself.
        """
        now = now or fields.Datetime.now()
        queued = self.env["turnstone.automation.run"]
        for rule in self._scheduled_rules():
            try:
                queued |= self._fire_rule(rule, now)
            except Exception:  # noqa: BLE001 - one bad rule may not stop the rest
                _logger.exception(
                    "Turnstone automation %s could not run its schedule; the "
                    "other schedules still ran.",
                    rule.name,
                )
        return queued

    @api.model
    def _scheduled_rules(self):
        """The active `schedule` rules, searched under `sudo()`.

        `sudo()` for the reason #5752 documents: the rule model is
        manager-only since #5759, and a rule has to fire for the deployment, not
        only when a manager happens to be looking. `trigger_type` is stored and
        indexed, so this is one indexed query per tick.
        """
        return self.env[RULE_MODEL].sudo().with_context(active_test=True).search([
            ("trigger_type", "in", list(SCHEDULE_TRIGGERS)),
        ])

    # -- one rule -----------------------------------------------------------

    @api.model
    def _fire_rule(self, rule, now):
        """Queue a run for each of `rule`'s records that is due, and return them.

        The order is oldest-due first, which is what makes `schedule_limit` fair:
        the records over the ceiling stay due and are selected again next tick,
        and because the oldest are taken first nothing can starve behind a
        constant stream of newer ones.
        """
        empty = self.env["turnstone.automation.run"]
        field_name = (rule.schedule_date_field or "").strip()
        if not field_name or not rule.target_model_id:
            # A schedule with no date field selects nothing — see the module
            # docstring on why guessing at one would be worse.
            return empty
        model = self.env.get(rule.target_model)
        if model is None:
            # The target model was uninstalled after the rule was written. There
            # is nothing to select from, and this is not a reason to fail a tick.
            _logger.warning(
                "Turnstone automation %s watches %s, which is not installed; its "
                "schedule fires nothing.", rule.name, rule.target_model,
            )
            return empty
        due = self._due_records(rule, model, field_name, now)
        if not due:
            return empty
        bucket = self._window_bucket(rule, now)
        # Drop the records this window has already fired for, BEFORE the
        # ceiling is applied. A due date does not move because the rule fired
        # on it, so without this a tick with a ceiling would re-select the same
        # oldest records forever: `_enqueue()` would answer each with the run it
        # already has, the ceiling would be spent on records that need nothing,
        # and the records behind them would never be reached. That is the
        # failure this filter exists to prevent, and it only shows up once a
        # rule has more due records than its ceiling.
        fresh = self._without_runs(rule, due, bucket)
        if not fresh:
            # Every due record has already been fired for in this window. The
            # next window is when they become events again.
            return empty
        limit = max(0, rule.schedule_limit or 0)
        selected = fresh[:limit] if limit else fresh
        if limit and len(fresh) > limit:
            _logger.warning(
                "Turnstone automation %s has %s records due and fired for the "
                "first %s (oldest due first). The rest are still due and the "
                "next tick will take them — this is \"At Most (records)\" on the "
                "rule, a ceiling so one busy model cannot become an outage.",
                rule.name, len(fresh), limit,
            )
        runs = empty
        for record in selected:
            # Reused rather than reimplemented: `_enqueue()` is the one place
            # that knows how a run is opened race-safely, what the company wall
            # is, and what the dedupe token should be. `matched=True` because the
            # domain has already been applied by `_due_records()`.
            run = self.env["turnstone.trigger.record"]._enqueue(
                rule, record,
                idempotency_key=self.event_key(record, bucket),
                matched=True,
            )
            runs |= run
        return runs

    @api.model
    def _without_runs(self, rule, records, bucket):
        """`records` minus the ones that already have a run for this window.

        One query, not one per record: the keys are built from the bucket and
        the record ids, and a `search` on `idempotency_key in (...) scoped to
        this rule` answers the whole question at once. A rule with a thousand
        due records therefore costs one indexed lookup, not a thousand.
        """
        if not records:
            return records
        keys = [self.event_key(record, bucket) for record in records]
        taken = self.env["turnstone.automation.run"].sudo().search([
            ("automation_id", "=", rule.id),
            ("idempotency_key", "in", keys),
        ]).mapped("idempotency_key")
        if not taken:
            return records
        already = set(taken)
        return records.filtered(
            lambda record: self.event_key(record, bucket) not in already
        )

    @api.model
    def _due_records(self, rule, model, field_name, now):
        """`rule`'s target records that are due at `now`, oldest due first.

        Two conditions, in one search: the rule's own domain, and the date field
        being at or before the deadline. They are combined rather than applied
        one after the other so the database does the work — a rule on a model
        with a hundred thousand rows must not read all of them to find the fifty
        that are due.

        `sudo()` on the search: a domain is administrator-written configuration
        about a field, and a due record must fire for the deployment even if the
        cron's user could not read that field. The *record* handed to `_enqueue`
        is then read under the rule's own run-as identity at launch time, which
        is where access actually has to hold.
        """
        deadline = self._deadline(model, field_name, now)
        if deadline is None:
            # The field was removed by a later module upgrade after the rule was
            # saved. Nothing is due by a field that is gone, and a tick is not
            # the place to report it: the save-time check already told whoever
            # could have fixed it.
            return model.browse()
        domain = Domain(rule._eval_target_domain()) & Domain(
            [(field_name, "!=", False), (field_name, "<=", deadline)]
        )
        return model.sudo().with_context(active_test=True).search(
            domain, order="%s, id" % field_name,
        )

    @api.model
    def _deadline(self, model, field_name, now):
        """The value a record's date field is compared against.

        A `date` field holds no time, so its deadline is the **end** of the day
        it names — a task due today is due all day. A `datetime` field is
        compared against `now` directly, because it does carry a time and the
        administrator chose it for that.

        Formatted rather than passed as a datetime: the domain is executed by
        the ORM against a `date` column or a `timestamp` column, and Odoo's own
        convention for comparing a `date` is a date string. Handing a datetime
        to a `date` comparison is the classic off-by-a-day.

        Returns `None` when the field is not on the model at all, which the
        caller treats as "nothing is due" rather than as a comparison against
        null.
        """
        field = model._fields.get(field_name)
        if field is None:
            return None
        if field.type == "date":
            return fields.Date.to_string(
                fields.Datetime.to_datetime(now)
            ) + DATE_END
        return fields.Datetime.to_string(now)

    # -- identity -----------------------------------------------------------

    @staticmethod
    def event_key(record, bucket):
        """The idempotency key for one record in one window.

        `schedule:<model>,<id>:<bucket>` — the record, and the window it fell
        into. Two ticks inside one window compute the same key, so the second
        is answered with the run the first queued instead of starting a second
        session; the next window is a new key and so a new run.

        The bucket is a **wall-clock** number, not a duration: an idle-duration
        key ("three days since the last write") changes on every tick, which
        would make every tick a new event and the window would not exist at all.
        """
        return "schedule:%s,%s:%s" % (record._name, record.id, bucket)

    @staticmethod
    def _window_bucket(rule, now):
        """Which window `now` falls into for `rule`, as a stable integer.

        `floor(now / window)`. A window of 0 means "no window", and the bucket
        is a constant `0`: the key is then the record alone, so the rule fires
        for a record exactly once and never again. That is the documented
        meaning of the setting — "something became due" — and the alternative
        (a new bucket every tick) would be a rule that fires every minute
        forever, which nobody configures on purpose.

        **The seconds are taken as UTC**, not through `datetime.timestamp()`.
        Odoo's `fields.Datetime` values are naive and *are* UTC, while
        `timestamp()` reads a naive datetime as the process's local time — so a
        server an hour off UTC would put two ticks a minute apart in two
        different buckets at a boundary, and the window would leak exactly once
        per hour. `calendar.timegm` is the function that says "this is UTC" out
        loud.
        """
        window = max(0, rule.schedule_interval_minutes or 0)
        if not window:
            return 0
        seconds = calendar.timegm(now.utctimetuple())
        return seconds // (window * 60)

    # -- reporting ----------------------------------------------------------

    @api.model
    def _due_count(self, rule, now=None):
        """How many of `rule`'s records are due right now. For the form, and tests.

        Deliberately its own method rather than a stored field: "due" is a
        function of the clock, and a stored count would be wrong the moment it
        was written. A rule's form can call this when it needs the number, and
        nothing has to keep it true.
        """
        rule.ensure_one()
        field_name = (rule.schedule_date_field or "").strip()
        if not field_name or not rule.target_model_id:
            return 0
        model = self.env.get(rule.target_model)
        if model is None:
            return 0
        now = now or fields.Datetime.now()
        return len(self._due_records(rule, model, field_name, now))
