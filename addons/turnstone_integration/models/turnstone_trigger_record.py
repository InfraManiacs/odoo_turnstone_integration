"""The record adapter: a record was created or changed, so a rule may want to run.

WHY THIS EXISTS

`turnstone.automation` (#5747) says *when* to ask, *what* to ask and *who* to
ask. `turnstone.automation.run` (#5748) records one firing. The launch service
(#5749) turns a run into a real Turnstone session. None of them notices that a
business record changed — until this file. It is the first adapter of the family
(#5752–#5756) and it owns the two Odoo events that matter most: `create` and
`write`.

WHAT "DISPATCH" MEANS HERE: ENQUEUE, NEVER LAUNCH

An ORM hook runs inside the caller's transaction, on a request thread, while a
user waits for their save to come back. Starting a Turnstone session from there
would mean a blocking HTTP call (up to `LONG_TIMEOUT` = 120 s) inside somebody's
form save, and an orphaned remote workstream whenever the save is rolled back
afterwards.

So this adapter does exactly one cheap, transactional thing: it creates a
`turnstone.automation.run` row in state **`queued`**. That is the queue — the
run table itself. `queued` is the state #5748's run model starts in for this
reason, `_mark_running()` is the only way out of it, and it is one of the states
the rule's dedupe window counts (`WINDOW_STATES`). The worker that drains
`queued` runs into `done`/`failed` is #5754; until it lands, a queued run is a
visible, answerable *intent* rather than a silence, and a person can watch it in
the run history on the rule.

This file therefore never calls `_launch_for_record()`. The ticket's summary
says "then call the core launch service" while its requirements say "never call
remote HTTP inside the ORM hook synchronously on user request; enqueue through
the queue mechanism", and its first acceptance criterion is "creates one queued
run". The requirement and the criterion agree with each other; the summary
sentence is the one that cannot be taken literally, because the launch service
*is* the HTTP call. The seam is recorded in
`architecture/automation-record-adapter.md`.

HOW THE HOOK IS WIRED, AND WHY NOT THE WAY `base_automation` DOES IT

Odoo's own `base.automation` patches each watched model class
(`setattr(ModelClass, 'create', wrapper)`) and reinstalls those patches by
*deleting* `create`/`write` from **every** model class in the registry and then
re-adding only its own (`_unregister_hook()` / `_update_registry()`). That
deletion is indiscriminate: it removes every other module's layer on those names
too — so a per-model patch here would silently stop firing the first time an
administrator edited a `base.automation` rule on the same model.

Instead there is **one** patch, on `BaseModel.create` / `BaseModel.write`, whose
first statement is a frozenset lookup on `self._name`. `base_automation` deletes
attributes from registered model *classes*; `BaseModel` is not one, so this
layer can never be removed underneath us, and a patch installed later wraps this
one instead of replacing it. It is installed at **import** time rather than in
`_register_hook()` on purpose: Odoo imports every module before calling any
`_register_hook()`, so being at the bottom of the chain is guaranteed rather
than depending on which of two unrelated modules happens to be set up first.

The cost is the whole performance story, and it is one set-membership test per
`create` and per `write` in the database — no query, no `ir.model` join, and no
adapter recordset built. A model that no rule watches is not in the set, so it
pays nothing else. The alternative, a per-model patch, pays nothing on unwatched
models but cannot be kept installed, and an adapter that quietly stops firing is
a lost automation.

THE WATCHED SET

`_refresh_watch_list()` recomputes the set from the rules table and stores it on
the registry (per process, per database, rebuilt on every registry setup). It
runs on registry load, and again whenever a rule is created or one of the fields
that decides the set is written (`active`, `trigger_type`, `target_model_id`) —
`_dispatch` routes the rule model's own events there by name. Refreshing *after*
the rule's own write means a rule created for `res.partner` fires on the very
next partner save.

Anything else written on a rule leaves the set alone on purpose: `_note_run()`
bumps `run_count`/`last_run_at` on every launch, so refreshing on any write would
make every launch re-search the rules table for nothing.

A rule that is *deleted* leaves its model in the set until the next registry
reload. That is harmless rather than sloppy: dispatching then finds no rule on
the indexed `target_model` search and queues nothing. Archiving a rule
(`active = False`) is the documented way to retire one (#5747), and it is
handled immediately.

WHAT ONE EVENT BECOMES

For each matching rule and each changed record, one run:

* **A create** is the record: `create:<model>,<id>`, because a row is created
  once and a record created twice cannot happen. If the same create event is
  ever handed in twice (another adapter calling in for the same record), the
  key makes it collapse instead of queueing twice.
* **A write** is the record's state at the end of the transaction:
  `write:<model>,<id>:<token>`, with the token minted per transaction. One
  business operation commonly writes the same row three times — a state change,
  then a date, then a counter — and three agent sessions for three intermediate
  states is not what anybody configured. The token is per transaction, so a
  genuinely separate write later is its own event, and a transaction that is
  retried (whose rolled-back runs never existed) starts cleanly instead of
  colliding with its own ghost.
* **The dedupe token** is `model,id`, set at create time because the field is
  frozen afterwards (a re-keyable run could be made to answer for another
  event). It is what the rule's `dedupe_window_minutes` searches when the run is
  launched, so "this rule keeps firing on the same record" is handled by the
  setting that exists for it, not by overloading the idempotency key.
* **The trigger record** is kept as plain text plus an integer, because the
  model may be uninstalled later and the run still has to say what it was about.

THE BOUND

`MAX_RUNS_PER_TRANSACTION` caps how many runs one transaction may enqueue, so a
bulk import of ten thousand contacts cannot become ten thousand agent sessions,
each with a 120-second HTTP call behind it. The count lives in `env.cr.cache`,
which Odoo scopes to one transaction and discards when it ends (or is retried by
`service.model.retrying()`). When the ceiling is reached, further matching events
queue nothing, and **one warning per transaction** names the rule and the record
that was dropped: a silent cap would look like a rule that works for the first
fifty records and then stops for no reason.

The bound is a guard rail, not a security control. A savepoint rollback inside
the transaction clears `cr.cache` and so resets the count early, which makes the
ceiling weaker, never wrong. Raise `MAX_RUNS_PER_TRANSACTION` deliberately
rather than working around it.

RECOMPUTES ARE NOT EVENTS

Odoo flushes and recomputes stored computed fields as part of a write, and a
recompute that changes a stored field is itself a `write()`. Dispatching that
would turn one business change into a second agent run, and — because the flush
can be triggered by this adapter's own `search()` — a recursion. So a dispatch
that happens *inside* another one is suppressed: it is a consequence of the
event already being handled, not a new event.

This is the problem Odoo's own `base.automation` solves with its `__action_done`
context guard, and the reason the flag here lives in `cr.cache` instead of the
caller's context: nothing a caller does (a `with_context(...)`, a nested
savepoint) can leak or lose it, and the transaction's end takes it away. Nested
creates that happen inside `create.origin()` — a `sale.order` creating its lines,
say — are dispatched normally, because our layer runs *after* `origin()` returns,
so they are already finished by the time the guard goes up.

THE HOOK NEVER BREAKS THE CALLER

A customer saving a partner must not get an error because an administrator wrote
a domain that no longer parses, or because a queued run could not be inserted.
Each rule is dispatched inside its own guard, and the whole event inside another
one: a failure is logged with the rule, the model and the record id, and the
business write returns normally. One broken rule does not stop the others.
"""

import logging
import uuid

from odoo import api, models
from odoo.models import BaseModel
from odoo.tools.sql import table_exists

_logger = logging.getLogger(__name__)

# Which `turnstone.automation.trigger_type` values this adapter answers. The
# other four belong to their own adapters: `chatter_message` to #5753,
# `schedule` to #5754's worker, `webhook` to #5755, and `manual` never fires by
# itself. Listing `record_create_or_write` in both is what makes that one trigger
# behave as its name says.
CREATE_TRIGGERS = ("record_create", "record_create_or_write")
WRITE_TRIGGERS = ("record_write", "record_create_or_write")
RECORD_TRIGGERS = ("record_create", "record_write", "record_create_or_write")

# How many runs one transaction may enqueue. See the module docstring: the point
# is that a bulk operation cannot become thousands of remote sessions. 50 is
# generous for a person's form save (which is one record) and small enough to
# stop a runaway loop early.
MAX_RUNS_PER_TRANSACTION = 50

# The model whose own create/write changes which models are watched, and the
# fields of it that can do so.
RULE_MODEL = "turnstone.automation"
WATCH_LIST_FIELDS = frozenset({"active", "trigger_type", "target_model_id"})

# Per-transaction bookkeeping in `env.cr.cache`: the run budget, the "already
# warned about the ceiling" flag, the re-entrancy guard, the per-transaction
# write token and the write events already queued.
_BUDGET_KEY = "turnstone_record_adapter.runs_enqueued"
_BOUND_LOGGED_KEY = "turnstone_record_adapter.bound_logged"
_DISPATCHING_KEY = "turnstone_record_adapter.dispatching"
_WRITE_TOKEN_KEY = "turnstone_record_adapter.write_token"
_WRITE_EVENTS_KEY = "turnstone_record_adapter.write_events"

# The attribute the watched-model set lives under on the registry. The registry
# is per process and per database and is rebuilt on a module upgrade, which is
# exactly the lifetime the set should have.
_WATCH_ATTR = "turnstone_record_adapter_models"

# Marks the functions this module installs, so a second install (or one by
# another module that kept this layer as its origin) is detected rather than
# wrapped twice.
_PATCH_MARKER = "_turnstone_record_adapter_patch"

# The patch is on `BaseModel`, which every registry and every model class in
# this process shares. Installing it twice would dispatch every event twice.
_PATCH_INSTALLED = False


def _watched_models(registry):
    """The models some active record rule watches, for this registry."""
    return getattr(registry, _WATCH_ATTR, frozenset())


def _dispatch(env, event, records, vals):
    """Hand one ORM event to the adapter, swallowing anything that goes wrong.

    The hot path: this runs on every `create` and every `write` in the database,
    so the first thing it does is a set lookup — no query, no `ir.model` join, no
    `sudo()`, and no adapter recordset built for a model no rule watches.

    The outermost guard: `_enqueue()` already refuses to raise, but reading a
    stored field can fail too (a record rule on a related field, a broken
    compute), and none of that may turn a user's save into an error.
    """
    name = records._name
    is_rule = name == RULE_MODEL
    if not (is_rule or name in _watched_models(env.registry)):
        return
    cache = env.cr.cache
    if cache.get(_DISPATCHING_KEY):
        # A create or write caused by this adapter's own work — a recompute
        # flushed by its search, say. A consequence of the event being handled,
        # not a new event. See "Recomputes are not events" above.
        return
    cache[_DISPATCHING_KEY] = True
    try:
        adapter = env["turnstone.trigger.record"]
        if is_rule:
            if event == "create" or WATCH_LIST_FIELDS.intersection(vals or ()):
                adapter._refresh_watch_list()
            if name not in _watched_models(env.registry):
                return
        if event == "create":
            adapter._on_create(records)
        else:
            adapter._on_write(records, vals)
    except Exception:  # noqa: BLE001 - see the module docstring
        _logger.exception(
            "Turnstone record adapter failed on %s of %s; the records were saved "
            "normally.", event, name,
        )
    finally:
        cache.pop(_DISPATCHING_KEY, None)


def _install_base_patch():
    """Install the one `BaseModel.create`/`write` layer this adapter needs.

    Called at import time, before any module's `_register_hook()` (Odoo imports
    every module, then calls the hooks). So this layer is at the bottom of
    whatever chain of patches the database ends up with: a later patch keeps
    this one as its `origin` and calls through, and no `_unregister_hook()` can
    take it out, because those only delete attributes from registered model
    classes and `BaseModel` is not one.

    Idempotent: a second call (a test may make one) sees the marker and returns.
    """
    global _PATCH_INSTALLED  # noqa: PLW0601 - one patch per process, on purpose
    if _PATCH_INSTALLED or getattr(BaseModel.create, _PATCH_MARKER, False):
        _PATCH_INSTALLED = True
        return

    # Captured before anything is replaced: the layers, if any, already
    # installed. `.origin` is the attribute Odoo's own patch helper
    # (`base_automation.patch()`) uses, so the convention is the platform's.
    base_create, base_write = BaseModel.create, BaseModel.write

    @api.model_create_multi
    def create(self, vals_list, **kw):
        records = create.origin(self, vals_list, **kw)
        _dispatch(self.env, "create", records, None)
        return records

    def write(self, vals, **kw):
        # Read the field names *before* calling through: `write` is free to
        # normalise or consume its `vals`, and what fired this event is what the
        # caller asked to change.
        written = set(vals)
        result = write.origin(self, vals, **kw)
        _dispatch(self.env, "write", self, written)
        return result

    create.origin = base_create
    write.origin = base_write
    setattr(create, _PATCH_MARKER, True)
    setattr(write, _PATCH_MARKER, True)
    BaseModel.create = create
    BaseModel.write = write
    _PATCH_INSTALLED = True
    _logger.debug(
        "Turnstone record adapter: installed the BaseModel.create/write layer.",
    )


_install_base_patch()


class TurnstoneTriggerRecord(models.AbstractModel):
    """Dispatch record create/write events to the rules that watch that model.

    An `AbstractModel` because there is no row to keep and nothing to show: a
    bag of behaviour attached to two ORM events, so it needs no table, no access
    rules and no menu (an abstract model is skipped by Odoo's "no access rules"
    install check for exactly this reason).
    """

    _name = "turnstone.trigger.record"
    _description = "Turnstone Record Trigger"

    # -- registry wiring ----------------------------------------------------

    def _register_hook(self):
        """Learn which models any rule watches.

        Called once per registry setup and again after every `_setup_models__`
        (a module install or upgrade). The `BaseModel` layer itself is installed
        at import; this only refreshes the data it reads.
        """
        self._refresh_watch_list()

    def _unregister_hook(self):
        """Forget the watched models; leave the `BaseModel` layer installed.

        Removing that layer would take every layer installed on top of it with
        it (the failure `base_automation._unregister_hook` causes for everyone
        else), and it is free when idle: an empty set means the patched
        functions return on their first statement.
        """
        self._set_watch_list(frozenset())

    @api.model
    def _set_watch_list(self, models_watched):
        setattr(self.env.registry, _WATCH_ATTR, frozenset(models_watched))

    @api.model
    def _refresh_watch_list(self):
        """Recompute the set of models at least one active record rule watches."""
        if not table_exists(self.env.cr, RULE_MODEL.replace(".", "_")):
            # A registry setup that runs while this module is still installing
            # (its table is created later in the same load) must not crash the
            # load. The next setup, or the first rule save, fills the set.
            self._set_watch_list(frozenset())
            return
        rules = self.env[RULE_MODEL].sudo().with_context(active_test=True).search([
            ("trigger_type", "in", list(RECORD_TRIGGERS)),
        ])
        self._set_watch_list(name for name in rules.mapped("target_model") if name)
        _logger.debug(
            "Turnstone record adapter watches %s.",
            sorted(_watched_models(self.env.registry)),
        )

    @api.model
    def _watched_models(self):
        return _watched_models(self.env.registry)

    # -- dispatch (the entry points; the tests drive these directly too) ----

    @api.model
    def _on_create(self, records):
        """Queue a run for every rule an ordinary record creation meets.

        A create counts as "all fields changed", which is how `base.automation`
        treats it too: a new record has no old values to differ from, so a rule
        that watches named fields still hears about the record being created. A
        field filter narrows *writes*, which is what it is documented to do.
        """
        self._dispatch_per_rule(records, CREATE_TRIGGERS, self._queue_create)

    @api.model
    def _on_write(self, records, vals):
        """Queue a run for every rule this particular change meets."""
        written = set(vals or ())
        self._dispatch_per_rule(
            records, WRITE_TRIGGERS,
            lambda rule, matching: self._queue_write(rule, matching, written),
        )

    @api.model
    def _dispatch_per_rule(self, records, triggers, queue):
        """Run `queue(rule, matching_records)` once per rule, each in its own guard.

        The guard is per rule, not per event, and that is the point: an
        administrator's broken domain on one rule must not stop the other rules
        on the same model from firing. A single try around the whole loop would
        make one bad rule disable every good one, which is a far worse failure
        than the broken rule itself.
        """
        if not records:
            return
        for rule in self._rules_for(records._name, triggers):
            try:
                queue(rule, self._matching_records(rule, records))
            except Exception:  # noqa: BLE001 - one bad rule may not stop the rest
                _logger.exception(
                    "Turnstone automation %s could not evaluate %s records of %s; "
                    "the records were saved normally, and other rules still ran.",
                    rule.name, len(records), records._name,
                )

    @api.model
    def _queue_create(self, rule, matching):
        for record in matching:
            if self._budget_spent():
                self._log_bound_hit(rule, record)
                return
            self._enqueue(
                rule, record,
                idempotency_key="create:%s,%s" % (record._name, record.id),
                matched=True,
            )

    @api.model
    def _queue_write(self, rule, matching, written):
        if not self._watched_field_changed(rule, written):
            return
        for record in matching:
            if self._budget_spent():
                self._log_bound_hit(rule, record)
                return
            self._enqueue(rule, record, matched=True)

    @api.model
    def _watched_field_changed(self, rule, written):
        """Whether this write touches a field the rule asked to hear about.

        An empty filter means "any change fires it", the field's documented
        default. The whole of field-filter support is this set intersection on
        the names Odoo passes to `write()`: one query per rule per write, and no
        field-value comparison.

        Writing the same value still counts. Odoo cannot tell "the form sent the
        value it already had" from "the value changed" without reading the old
        row, and reading it on every write of a watched model is a real cost for
        a distinction the rule's dedupe window already covers.
        """
        watched = set(rule.trigger_field_ids.mapped("name"))
        if not watched:
            return True
        return bool(watched & written)

    # -- finding the rules that apply ---------------------------------------

    @api.model
    def _rules_for(self, model_name, triggers):
        """The active rules of `model_name`, for one or more `triggers`.

        Searched under `sudo()`: `turnstone.automation` is manager-only since
        #5759, so a rule would never fire for the clerk who triggered it if this
        search went through their own rights. `target_model` is a stored,
        indexed field, so this is one indexed query per watched model per event
        — and no query at all for a model in no rule.

        Company narrowing happens per record (`_may_fire`), because one search
        cannot know which record a rule is being asked about.
        """
        if not model_name:
            return self.env[RULE_MODEL]
        return self.env[RULE_MODEL].sudo().with_context(active_test=True).search([
            ("target_model", "=", model_name),
            ("trigger_type", "in", list(triggers)),
        ])

    @api.model
    def _matching_records(self, rule, records):
        """`records` narrowed to the ones this rule's company and domain accept.

        Company and domain are applied to the whole recordset at once rather
        than per record inside a loop: a bulk create of a thousand rows is one
        filter, not a thousand. That matters because this runs on a request
        thread, and a rule that watched a busy model would otherwise turn a
        bulk import into a thousand domain evaluations.
        """
        allowed = records.filtered(lambda rec: self._company_ok(rule, rec))
        if not allowed:
            return allowed
        # `sudo()`: a domain is administrator-written configuration about a
        # field, and the person whose save triggered the rule may not be allowed
        # to read that field. Refusing the rule for them would make the same rule
        # fire for a manager and not for a clerk, which is the least debuggable
        # failure a rule can have.
        return rule._matches_domain(allowed.sudo())

    @api.model
    def _company_ok(self, rule, record):
        """Whether this record may fire a rule of `rule`'s company.

        A rule belongs to a company and its runs inherit it, so a record of
        company A must not start a run attributed to company B. A model with no
        `company_id` at all, or a record with an empty one (`res.partner` with
        no company means "shared"), fires rules of the companies this
        environment may see — the same set the record rules give those rows.
        """
        if "company_id" not in record._fields:
            return rule.company_id.id in self.env.companies.ids
        company = record.sudo().company_id
        if company:
            return company.id == rule.company_id.id
        return rule.company_id.id in self.env.companies.ids

    @api.model
    def _may_fire(self, rule, record):
        """Whether one record passes the rule's company wall and its domain.

        The single-record form of `_matching_records()`, kept for `_enqueue()`
        (which is also called directly, by tests and by any later adapter that
        already has one record in hand).
        """
        if not self._company_ok(rule, record):
            return False
        return bool(rule._matches_domain(record.sudo()))

    # -- queueing one run ---------------------------------------------------

    @api.model
    def _enqueue(self, rule, record, idempotency_key=None, matched=False):
        """Queue one run for `rule` about `record`, or find it already queued.

        ``matched`` says the caller has already put this record through
        `_matching_records()` / `_may_fire()`. The loops set it, so a bulk create
        does not evaluate the rule's domain twice per row; a direct caller leaves
        it out and gets the check.
        """
        empty = self.env["turnstone.automation.run"]
        if not record.id:
            # Not persisted (a NewId, or a create that has not been flushed).
            # There is nothing for a run to point at yet.
            return empty
        runs = empty.sudo()
        try:
            if not matched and not self._may_fire(rule, record):
                return empty
            if not self._take_budget():
                self._log_bound_hit(rule, record)
                return empty
            run, created = runs._open_for_event(
                rule, idempotency_key or self._write_event_key(rule, record),
                record=record,
                # Set here rather than later because it is frozen after create,
                # and it is what the rule's "Suppress Repeats" window searches
                # when the run is finally launched.
                dedupe_token="%s,%s" % (record._name, record.id),
            )
            if not created:
                _logger.debug(
                    "Turnstone automation %s: event %s already has run %s (%s).",
                    rule.name, idempotency_key, run.id, run.state,
                )
            return run
        except Exception:  # noqa: BLE001 - a hook may not break the caller's save
            # Named on the rule that failed rather than re-raised: the user's
            # partner save is not the place to report an administrator's broken
            # configuration. The log line carries the rule and the record, so the
            # cause is one grep away.
            _logger.exception(
                "Turnstone automation %s could not queue an event for %s,%s; the "
                "record was saved normally.",
                rule.name, record._name, record.id,
            )
            return empty

    @api.model
    def _write_event_key(self, rule, record):
        """This write event's idempotency key.

        A write event is the record's state at the end of the transaction, not
        each `write()` call: a create followed by two writes inside one business
        operation is one event and must start one session. The token is minted
        per transaction, so a separate write later is a separate event, and a
        retried transaction — whose rolled-back runs never existed — starts
        cleanly rather than colliding with itself.
        """
        cache = self.env.cr.cache
        token = cache.get(_WRITE_TOKEN_KEY)
        if not token:
            token = uuid.uuid4().hex
            cache[_WRITE_TOKEN_KEY] = token
        return "write:%s,%s:%s" % (record._name, record.id, token)

    # -- the per-transaction bound ------------------------------------------

    @api.model
    def _forget_transaction_state(self):
        """Drop this transaction's bookkeeping. For tests, and for `cr.clear()`.

        Not needed in production: `env.cr.cache` is discarded with the cursor, and
        a retried transaction calls `Transaction.clear()`, which empties it too.
        A `TransactionCase` is the exception — every test shares one transaction,
        so without this the bound and the write token from the first test would
        be in force for the last.
        """
        cache = self.env.cr.cache
        for key in (_BUDGET_KEY, _BOUND_LOGGED_KEY, _DISPATCHING_KEY,
                    _WRITE_TOKEN_KEY):
            cache.pop(key, None)

    @api.model
    def _budget_spent(self):
        """Whether this transaction has already queued its maximum."""
        return self.env.cr.cache.get(_BUDGET_KEY, 0) >= MAX_RUNS_PER_TRANSACTION

    @api.model
    def _take_budget(self):
        """Consume one unit of this transaction's run budget, or refuse."""
        cache = self.env.cr.cache
        spent = cache.get(_BUDGET_KEY, 0)
        if spent >= MAX_RUNS_PER_TRANSACTION:
            return False
        cache[_BUDGET_KEY] = spent + 1
        return True

    @api.model
    def _log_bound_hit(self, rule, record):
        """Say once per transaction what the ceiling dropped, and why.

        `record` is a record the rule would have fired for: the loops check the
        budget only after `_matching_records()` has accepted it, so the message
        names something that was genuinely dropped rather than the next record in
        table order.
        """
        cache = self.env.cr.cache
        if cache.get(_BOUND_LOGGED_KEY):
            return
        cache[_BOUND_LOGGED_KEY] = True
        _logger.warning(
            "Turnstone automation %s reached the ceiling of %s queued runs in one "
            "transaction, so further matching records in this transaction queue "
            "nothing (first dropped: %s,%s). This is MAX_RUNS_PER_TRANSACTION in "
            "turnstone_trigger_record.py — a bulk operation must not become "
            "thousands of agent sessions.",
            rule.name, MAX_RUNS_PER_TRANSACTION, record._name, record.id,
        )