"""The record adapter: a create/write queues a run, and a non-matching model costs nothing.

`turnstone.trigger.record` (OTI-5752) is what notices that a business record was
created or changed and enqueues one `turnstone.automation.run` per rule that
watches it. It never launches: the launch service (#5749) is the HTTP call, and
an ORM hook runs in somebody's form save. The run the adapter leaves behind is
in state `queued`, which is what #5754's worker drains.

These cases are the ticket's three acceptance criteria, plus the shapes a hook
into every `create` and `write` in the database must not get wrong:

1. **A matching create queues exactly one run.** Asserted on the run table, and
   `state` is asserted to be `queued` rather than `done` — "it queued" and "it
   launched" are different claims and only the first one belongs to an ORM hook.
2. **A non-matching model does no work at all.** "No work" is asserted the only
   way it can be: the database's own query counter (`self.cr.sql_log_count`), so
   a `search()` on every write of every model fails the test even though it
   returns nothing. A `tests.SAVE_QUERIES`-free assertion here is deliberate —
   the point is the absence of a query, not a small number of them.
3. **The field filter is respected.** A write that touches none of the watched
   fields queues nothing; one that touches a watched field queues a run.

The rest are the invariants the design rests on: a write that changes nothing a
rule watches does not fire; the domain narrows a record out; another company's
rule does not fire; a burst of writes in one transaction is one run (not three);
a bulk create stops at the bound and says so; a broken rule does not break the
caller's save; and the hook does not re-enter itself on the recomputes its own
search flushes.

Every case needs no network and no mock server: this adapter's whole job is what
it writes to the database, and the launch that follows is a different ticket's.
"""

import logging
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from odoo.addons.turnstone_integration.models.turnstone_trigger_record import (
    MAX_RUNS_PER_TRANSACTION,
)

PARTNER = "res.partner"
RUN = "turnstone.automation.run"


def _raise_for(rule):
    """A `_matches_domain()` replacement that raises for `rule` only.

    The shape a real broken rule has: a domain naming a field that another
    module renamed out from under it. It cannot be saved that way (#5747
    validates on save), so patching the evaluation is the only faithful way to
    reproduce it — and doing it per rule keeps the *other* rules real, which is
    what `test_one_broken_rule_does_not_stop_a_working_one` needs.
    """
    original = type(rule)._matches_domain

    def matches(self, records):
        if self.id != rule.id:
            return original(self, records)
        raise ValueError("a field this domain names no longer exists")

    return matches


class RecordTriggerCase(TransactionCase):
    """Fixtures: a rule that watches partners, and helpers to make records.

    No mock server and no `TurnstoneTransactionCase`: nothing here reaches the
    network, and a case that needed the harness would be testing #5749 rather
    than this adapter.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Record adapter (no network)",
            "base_url": "http://127.0.0.1:1",
            "api_token": "irrelevant-this-ticket-makes-no-calls",
            "is_default": True,
        })
        cls.agent = cls.env["turnstone.agent"].create({
            "name": "Record Adapter Agent",
            "connection_id": cls.connection.id,
            "kind": "interactive",
        })
        cls.partner_model = cls.env["ir.model"]._get(PARTNER)

    def setUp(self):
        super().setUp()
        # A `TransactionCase` runs every test in ONE transaction, so `cr.cache`
        # survives from one test to the next — unlike production, where each
        # request gets its own cursor and the cache dies with it. Clearing the
        # adapter's keys here is what makes "this transaction's budget" mean one
        # test rather than one class.
        self.env["turnstone.trigger.record"]._forget_transaction_state()
        # The watched set is cached on the registry, and a rule created in a
        # previous test is gone when that test's savepoint rolls back — so the
        # set must be recomputed rather than trusted.
        self.env["turnstone.trigger.record"]._refresh_watch_list()

    # -- fixtures ----------------------------------------------------------

    def _rule(self, **vals):
        """A record rule on partners. `suffix` keeps the name unique per test."""
        suffix = vals.pop("suffix", "a")
        defaults = {
            "name": "Record adapter rule %s" % suffix,
            "company_id": self.company.id,
            "agent_id": self.agent.id,
            "trigger_type": "record_create",
            "target_model_id": self.partner_model.id,
            "prompt_template": "Greet {{ name }}.",
            "run_as": "fixed_user",
            "run_as_user_id": self.env.user.id,
        }
        defaults.update(vals)
        rule = self.env["turnstone.automation"].create(defaults)
        self.env["turnstone.trigger.record"]._refresh_watch_list()
        return rule

    def _partner(self, **vals):
        values = {"name": "Record Adapter Partner"}
        values.update(vals)
        return self.env[PARTNER].create(values)

    def _runs(self, rule=None):
        domain = [("automation_id", "=", rule.id)] if rule else []
        return self.env[RUN].sudo().search(domain)

    def _sql_count(self):
        """Queries this cursor has executed. The only honest way to prove "none"."""
        return self.cr.sql_log_count


@tagged("post_install", "-at_install")
class TestMatchingCreate(RecordTriggerCase):
    """Criterion 1: a matching create queues exactly one run."""

    def test_a_matching_create_queues_one_queued_run(self):
        rule = self._rule()
        partner = self._partner(name="Ada Lovelace")

        runs = self._runs(rule)
        self.assertEqual(len(runs), 1, "one rule watching one record must queue one run")
        run = runs
        self.assertEqual(
            run.state, "queued",
            "an ORM hook queues the event; only #5754's worker may launch it",
        )
        self.assertEqual(run.trigger_res_model, PARTNER)
        self.assertEqual(run.trigger_res_id, partner.id)
        self.assertEqual(
            run.dedupe_token, "%s,%s" % (PARTNER, partner.id),
            "the dedupe token must be the record, or the rule's repeat window "
            "can never match",
        )

    def test_the_run_is_queued_not_launched(self):
        """No session, no attempt: a queued run has not tried anything yet."""
        rule = self._rule()
        self._partner()

        run = self._runs(rule)
        self.assertEqual(run.state, "queued")
        self.assertEqual(run.attempt_count, 0)
        self.assertFalse(run.session_id, "an ORM hook must not create a session")
        self.assertFalse(
            self.env["turnstone.session"].sudo().search([
                ("agent_id", "=", self.agent.id),
            ]),
            "the adapter must never reach the launch service",
        )

    def test_the_rules_counter_does_not_move(self):
        """`run_count` counts launches (#5749's `_note_run`), not queued events."""
        rule = self._rule()
        self._partner()

        self.assertEqual(
            rule.run_count, 0,
            "a queued run has not run; moving the counter here would make the "
            "rule's own statistics claim work that never happened",
        )

    def test_create_or_write_fires_on_a_create(self):
        rule = self._rule(trigger_type="record_create_or_write")
        self._partner()
        self.assertEqual(len(self._runs(rule)), 1)

    def test_a_record_write_rule_does_not_fire_on_a_create(self):
        rule = self._rule(trigger_type="record_write")
        self._partner()
        self.assertEqual(
            len(self._runs(rule)), 0,
            "a rule that asked only about updates must not hear about creates",
        )

    def test_a_manual_rule_never_fires_by_itself(self):
        # No target model, so the prompt cannot use a placeholder (#5747 refuses
        # one that would render a hole) — a static instruction is the shape a
        # manual rule legitimately has.
        rule = self._rule(
            trigger_type="manual", target_model_id=False,
            prompt_template="Do the thing on demand.",
        )
        self._partner()
        self.assertEqual(len(self._runs(rule)), 0)

    def test_an_archived_rule_does_not_fire(self):
        rule = self._rule()
        rule.active = False
        self.env["turnstone.trigger.record"]._refresh_watch_list()
        self._partner()
        self.assertEqual(len(self._runs(rule)), 0)

    def test_two_rules_on_one_model_make_two_runs(self):
        first = self._rule(suffix="one", name="First rule")
        second = self._rule(suffix="two", name="Second rule")
        partner = self._partner()

        self.assertEqual(len(self._runs(first)), 1)
        self.assertEqual(len(self._runs(second)), 1)
        self.assertEqual(self._runs(first).trigger_res_id, partner.id)
        self.assertEqual(self._runs(second).trigger_res_id, partner.id)


@tagged("post_install", "-at_install")
class TestNonMatchingModel(RecordTriggerCase):
    """Criterion 2: a model no rule watches incurs no work at all."""

    def _assert_adapter_not_entered(self, event, action):
        """Assert one `event` caused by `action` never reaches the adapter.

        Patching `_on_create`/`_on_write` is the direct proof of "no work at
        all": if the adapter is never entered, no `search()` ran, no run was
        written, and no ORM work happened. A query-count comparison would be
        indirect and would break on Odoo internals this ticket does not own
        (creating a currency legitimately issues two dozen queries of its own).

        Patched on the **registry class**, which is the dynamically built
        subclass the environment instantiates — patching the module-level class
        would not affect it.
        """
        adapter_class = type(self.env["turnstone.trigger.record"])
        method = "_on_%s" % event
        calls = []
        original = getattr(adapter_class, method)

        def counting(self, *args, **kwargs):
            calls.append(args)
            return original(self, *args, **kwargs)

        # Assigned on the instance's own class and removed afterwards: `delattr`
        # rather than a restore, so the class gets its inherited method back and
        # nothing is left shadowed for the next test.
        setattr(adapter_class, method, counting)
        try:
            action()
        finally:
            delattr(adapter_class, method)
        return calls

    def test_creating_an_unwatched_model_does_no_work(self):
        """One rule on partners must cost nothing on another model's create."""
        self._rule()
        calls = self._assert_adapter_not_entered("create", lambda: self.env["res.currency"].create({
            "name": "Adapter Test Coin", "symbol": "ATC",
        }))

        self.assertEqual(
            calls, [],
            "a create of a model no rule watches must not reach the adapter at "
            "all — a `search()` there would run on every create in the database",
        )

    def test_writing_an_unwatched_model_does_no_work(self):
        self._rule()
        currency = self.env["res.currency"].create({
            "name": "Adapter Test Coin", "symbol": "ATC",
        })
        calls = self._assert_adapter_not_entered(
            "write", lambda: currency.write({"symbol": "ATD"}),
        )

        self.assertEqual(
            calls, [],
            "a write of a model no rule watches must not reach the adapter at all",
        )

    def test_the_adapter_returns_before_touching_the_database(self):
        """The gate itself: an unwatched model is not in the watched set."""
        rule = self._rule()
        adapter = self.env["turnstone.trigger.record"]
        self.assertIn(PARTNER, adapter._watched_models())
        self.assertNotIn("res.currency", adapter._watched_models())

        before = self._sql_count()
        adapter._on_create(self.env["res.currency"].browse())
        adapter._on_write(self.env["res.currency"].browse(), {"symbol": "X"})
        self.assertEqual(self._sql_count(), before, "an empty recordset does nothing")
        self.assertFalse(
            self._runs(rule),
            "and it certainly must not queue a run for a model the rule does not watch",
        )

    def test_a_rule_with_no_target_model_watches_nothing(self):
        """`manual` and `webhook` rules carry their own context, not a model."""
        self._rule(
            trigger_type="manual", target_model_id=False, suffix="manual",
            prompt_template="Do the thing on demand.",
        )
        self.assertNotIn(
            PARTNER, self.env["turnstone.trigger.record"]._watched_models(),
            "a rule with no target model must not put its model in the watched set",
        )


@tagged("post_install", "-at_install")
class TestFieldFilter(RecordTriggerCase):
    """Criterion 3: only a change to a watched field fires the rule."""

    def _filtered_rule(self):
        field = self.env["ir.model.fields"]._get(PARTNER, "email")
        return self._rule(
            trigger_type="record_write",
            trigger_field_ids=[(6, 0, [field.id])],
            prompt_template="Email changed on {{ name }}.",
        )

    def test_a_write_of_an_untracked_field_queues_nothing(self):
        rule = self._filtered_rule()
        partner = self._partner()

        partner.write({"phone": "+1 555 0100"})

        self.assertEqual(
            len(self._runs(rule)), 0,
            "the rule watches `email`; a `phone` change is not its event",
        )

    def test_a_write_of_the_watched_field_queues_one_run(self):
        rule = self._filtered_rule()
        partner = self._partner()

        partner.write({"email": "ada@example.com"})

        runs = self._runs(rule)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs.state, "queued")

    def test_a_write_touching_several_fields_including_the_watched_one_fires_once(self):
        rule = self._filtered_rule()
        partner = self._partner()

        partner.write({"phone": "+1 555 0101", "email": "ada@example.com"})

        self.assertEqual(len(self._runs(rule)), 1)

    def test_the_filter_still_lets_a_create_through(self):
        """A create has no old values, so every field counts as changed.

        That is how `base.automation` treats it, and it is what an administrator
        means by "tell me when a partner's email changes" — a new partner with
        an email is such a partner.
        """
        rule = self._rule(
            trigger_type="record_create",
            trigger_field_ids=[(6, 0, [
                self.env["ir.model.fields"]._get(PARTNER, "email").id,
            ])],
        )
        self._partner()
        self.assertEqual(len(self._runs(rule)), 1)

    def test_an_unfiltered_rule_fires_on_any_field(self):
        rule = self._rule(trigger_type="record_write")
        partner = self._partner()
        partner.write({"phone": "+1 555 0102"})
        self.assertEqual(len(self._runs(rule)), 1)


@tagged("post_install", "-at_install")
class TestWriteSemantics(RecordTriggerCase):
    """What one write event is, and what it is not."""

    def test_a_burst_of_writes_in_one_transaction_is_one_run(self):
        """One business operation commonly writes the same row several times."""
        rule = self._rule(trigger_type="record_write")
        partner = self._partner()

        partner.write({"phone": "+1 555 0200"})
        partner.write({"function": "Mathematician"})
        partner.write({"comment": "Changed three times in one save."})

        self.assertEqual(
            len(self._runs(rule)), 1,
            "three writes inside one transaction are one event: the run's prompt "
            "renders the record's final state, so the intermediate ones are not "
            "separate events anyone configured",
        )

    def test_a_write_of_a_setting_field_only_queues_nothing(self):
        """`_note_run()` writes counters on the rule itself; that is not an event."""
        rule = self._rule()
        self.assertEqual(len(self._runs(rule)), 0)
        rule._note_run()
        self.assertEqual(
            len(self._runs(rule)), 0,
            "a counter bump must not be mistaken for a watched business change",
        )

    def test_the_trigger_record_is_the_record_that_changed(self):
        rule = self._rule(trigger_type="record_create_or_write")
        first = self._partner(name="First")
        second = self._partner(name="Second")

        runs = self._runs(rule)
        self.assertEqual(len(runs), 2)
        self.assertEqual(
            sorted(runs.mapped("trigger_res_id")), sorted([first.id, second.id]),
        )

    def test_a_multi_record_write_queues_one_run_per_record(self):
        rule = self._rule(trigger_type="record_write")
        partners = self._partner() | self._partner(name="Another")

        partners.write({"function": "Both"})

        self.assertEqual(len(self._runs(rule)), 2)
        self.assertEqual(
            sorted(self._runs(rule).mapped("trigger_res_id")),
            sorted(partners.ids),
        )


@tagged("post_install", "-at_install")
class TestNarrowing(RecordTriggerCase):
    """The rule's own filters: its domain and its company."""

    def test_a_record_outside_the_domain_queues_nothing(self):
        rule = self._rule(target_domain="[('is_company', '=', True)]")
        self._partner(name="An individual", is_company=False)
        self.assertEqual(len(self._runs(rule)), 0)

    def test_a_record_inside_the_domain_queues_one_run(self):
        rule = self._rule(target_domain="[('is_company', '=', True)]")
        self._partner(name="A company", is_company=True)
        self.assertEqual(len(self._runs(rule)), 1)

    def test_another_companys_rule_does_not_fire(self):
        """A run inherits its rule's company, so the wall is the rule's own.

The rule needs an agent of *its* company: `_check_company` refuses a rule
        whose agent belongs elsewhere (that is #5747's constraint, not this
        adapter's). So the fixture builds a second connection and agent in the
        other company, and the assertion is that the partner of company A, which
        the rule does not belong to, does not fire it.
        """
        other = self.env["res.company"].create({"name": "Other Company"})
        other_connection = self.env["turnstone.connection"].create({
            "name": "Other company connection",
            "base_url": "http://127.0.0.1:1",
            "api_token": "irrelevant",
            "company_id": other.id,
        })
        other_agent = self.env["turnstone.agent"].create({
            "name": "Other company agent",
            "connection_id": other_connection.id,
            "kind": "interactive",
        })
        rule = self.env["turnstone.automation"].create({
            "name": "Other company rule",
            "company_id": other.id,
            "agent_id": other_agent.id,
            "trigger_type": "record_create",
            "target_model_id": self.partner_model.id,
            "prompt_template": "Greet {{ name }}.",
            "run_as": "fixed_user",
            "run_as_user_id": self.env.user.id,
        })
        self.env["turnstone.trigger.record"]._refresh_watch_list()

        # An explicit company of this company, so this is the cross-company case
        # and not the shared-record case (a partner with NO company is shared by
        # design; see `test_a_shared_record_fires_a_visible_companys_rule`).
        self._partner(company_id=self.company.id)

        self.assertEqual(
            len(self._runs(rule)), 0,
            "a partner of this company must not start a run for a rule of "
            "another company, whose managers would then be able to read it",
        )

    def test_a_shared_record_fires_a_visible_companys_rule(self):
        """A partner with no company is shared, so it fires a visible company's rule.

        `res.partner.company_id` empty means "every company uses this contact".
        Refusing every rule for such a record would make automations on partners
        work only for records somebody remembered to stamp a company on.
        """
        rule = self._rule()
        partner = self._partner()
        self.assertFalse(partner.company_id, "the fixture's partner is shared")
        self.assertEqual(len(self._runs(rule)), 1)

    def test_an_ordinary_user_still_fires_a_manager_only_rule(self):
        """`turnstone.trigger.record` searches rules with `sudo()` on purpose.

        `turnstone.automation` is manager-only (#5759): a clerk cannot read a
        single rule. If the adapter's search ran through the caller's rights,
        every rule would fire for managers and none for the people who actually
        create the records — the least debuggable failure a rule can have. So the
        claim is tested on the search itself: the same lookup returns nothing for
        the clerk and the rule for the adapter.
        """
        rule = self._rule()
        clerk = self.env["res.users"].create({
            "name": "Clerk",
            "login": "record-adapter-clerk",
            "company_id": self.company.id,
            "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
            ])],
        })
        clerk_env = self.env(user=clerk)

        with self.assertRaises(
            AccessError,
            msg="the fixture is only meaningful if a clerk really cannot read rules",
        ):
            clerk_env["turnstone.automation"].search([])
        self.env["turnstone.trigger.record"]._refresh_watch_list()
        self.assertIn(
            rule, clerk_env["turnstone.trigger.record"]._rules_for(
                PARTNER, ("record_create",),
            ),
            "the adapter's rule search must not run under the caller's rights — "
            "a clerk cannot read the rule at all, and the adapter still finds it",
        )


@tagged("post_install", "-at_install")
class TestTheBound(RecordTriggerCase):
    """`MAX_RUNS_PER_TRANSACTION`: a bulk operation cannot become thousands of runs."""

    def test_a_bulk_create_stops_at_the_ceiling(self):
        rule = self._rule()
        values = [{"name": "Bulk %s" % index} for index in range(MAX_RUNS_PER_TRANSACTION + 5)]
        self.env[PARTNER].create(values)
        self.assertEqual(len(self._runs(rule)), MAX_RUNS_PER_TRANSACTION)

    def test_the_ceiling_logs_one_warning_naming_what_it_dropped(self):
        rule = self._rule()
        values = [{"name": "Bulk %s" % index} for index in range(MAX_RUNS_PER_TRANSACTION + 5)]
        with self.assertLogs(
            "odoo.addons.turnstone_integration.models.turnstone_trigger_record",
            level=logging.WARNING,
        ) as captured:
            self.env[PARTNER].create(values)
        warnings = [line for line in captured.output if "reached the ceiling" in line]
        self.assertEqual(
            len(warnings), 1,
            "the ceiling must be reported once per transaction, not once per "
            "dropped record: %s" % captured.output,
        )
        self.assertIn(rule.name, warnings[0])
        self.assertIn(
            PARTNER, warnings[0],
            "the warning must name what was dropped, or nobody can tell which "
            "records never reached an agent",
        )

    def test_a_transaction_under_the_ceiling_logs_nothing(self):
        self._rule()
        with self.assertNoLogs(
            "odoo.addons.turnstone_integration.models.turnstone_trigger_record",
            level=logging.WARNING,
        ):
            self.env[PARTNER].create([{"name": "Bulk %s" % index} for index in range(5)])


@tagged("post_install", "-at_install")
class TestHookSafety(RecordTriggerCase):
    """A broken rule must not break the caller's save, and must not recurse."""

    def test_a_broken_rule_does_not_stop_the_save(self):
        """A rule that cannot be evaluated must not fail the caller's save.

        The broken rule is simulated at the seam that can actually break: a
        domain is validated on save, but it is *evaluated* later, against a model
        that another module may have changed underneath it. So
        `_matches_domain()` is made to raise for one rule, which is exactly what
        a domain naming a field that no longer exists does in production.
        """
        rule = self._rule()
        with patch.object(
            type(rule), "_matches_domain", _raise_for(rule),
        ):
            partner = self.env[PARTNER].create({"name": "Saved Anyway"})

        self.assertTrue(partner.id, "the record must be created regardless")
        self.assertEqual(
            len(self._runs(rule)), 0,
            "the rule refused the record, so there is no run to show for it",
        )

    def test_one_broken_rule_does_not_stop_a_working_one(self):
        broken = self._rule(suffix="broken", name="Broken rule")
        working = self._rule(suffix="working", name="Working rule")

        with patch.object(
            type(broken), "_matches_domain", _raise_for(broken),
        ):
            self._partner()

        self.assertEqual(len(self._runs(broken)), 0)
        self.assertEqual(
            len(self._runs(working)), 1,
            "one administrator's broken rule must not disable every other rule "
            "on the same model",
        )

    def test_the_adapter_does_not_dispatch_its_own_rule_write(self):
        """Creating the run must not look like a watched model changing.

        The run model is not in any rule's target model here, so this is really
        a check that `_open_for_event()`'s insert (and the `search()` before it)
        does not find its way back into the adapter — the recursion the
        re-entrancy guard exists for.
        """
        rule = self._rule()
        with self.assertNoLogs(
            "odoo.addons.turnstone_integration.models.turnstone_trigger_record",
            level=logging.ERROR,
        ):
            self._partner()
        self.assertEqual(len(self._runs(rule)), 1)

    def test_the_layer_is_installed_once(self):
        """A second install must not wrap the first, or every event doubles."""
        adapter_class = type(self.env["turnstone.trigger.record"])
        calls = []
        original = adapter_class._on_create

        def counting(self, records):
            calls.append(records.ids)
            return original(self, records)

        adapter_class._on_create = counting
        try:
            # Two more registry setups: each calls `_register_hook()`, and a
            # second `BaseModel` layer would dispatch this create twice.
            self.env.registry._setup_models__(self.cr)
            self.env.registry._setup_models__(self.cr)
            self._rule()
            partner = self.env[PARTNER].create({"name": "Dispatched once"})
        finally:
            delattr(adapter_class, "_on_create")

        self.assertEqual(
            calls, [[partner.id]],
            "one partner create must reach `_on_create` exactly once, however "
            "many times the registry was set up",
        )

    def test_the_patch_survives_a_registry_setup(self):
        """`base_automation` deletes model-class methods; ours is on `BaseModel`.

        Re-running the registry setup is what `base_automation` does when an
        administrator edits a rule, and it is the exact operation that would
        remove a per-model patch.
        """
        from odoo.models import BaseModel  # noqa: PLC0415 - asserted inside the test

        before = (BaseModel.create, BaseModel.write)
        self.env.registry._setup_models__(self.cr)
        self.assertIs(BaseModel.create, before[0])
        self.assertIs(BaseModel.write, before[1])

        rule = self._rule()
        self._partner()
        self.assertEqual(len(self._runs(rule)), 1)