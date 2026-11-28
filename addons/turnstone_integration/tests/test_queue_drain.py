"""The drain: one run, one claim, one workstream — even with a second worker.

`turnstone.automation.run._drain_queue()` (OTI-5754) is the cron that turns the
`queued` intent every trigger adapter writes into a `done` or `failed` run. The
claim it makes per run is the only thing between two cron workers and two
sessions for one event, so it is what these cases are about:

1. **Two drains, one claim.** A queued run is launched exactly once when a
   second database session is competing for it.
2. **A failure is re-attempted once, not re-processed.** A run whose launch
   failed is not picked up again by the next tick, and the event's own retry
   finishes the same event without a second workstream on the server.
3. **A terminal run is never picked up.** `done`, `skipped` and `running` are
   somebody else's or history; only `queued` is the drain's.

WHERE THE ASSERTION GOES

Every case here is about *whether a launch happened*, so the assertion is on the
mock Turnstone server's request log (`/v1/api/admin/mock/requests`) and its own
workstream table — the only view a third party has of "one prompt was
delivered". Odoo bookkeeping (`state`, `attempt_count`) is asserted alongside,
never instead: a stale `queued` next to zero creates is a different bug from two
creates, and a test that reads only the row cannot tell them apart.

WHY A SECOND CONNECTION, AND WHAT IT CAN AND CANNOT PROVE

`TransactionCase` hands out `TestCursor`s that share ONE connection behind a
re-entrant lock, so two cursors from the registry are never in flight at once
and a "concurrency" test written with them passes whatever the code does. The
claim's first wall is a Postgres advisory lock, and advisory locks live in the
server's global lock table — not in MVCC — so a lock taken by a **genuinely
separate connection** is visible to this transaction without either side having
to see the other's uncommitted rows. That is exactly what
`test_two_drain_sessions_claim_a_queued_run_exactly_once` uses: a real second
connection takes the claim lock the way the other worker's `_claim_for_drain()`
would, this worker's drain must then launch nothing, and the moment that session
ends the same run is claimed once and launched once.

What a test still cannot do is let two workers each *see* the queued row and race
for it, because the row only exists uncommitted inside this transaction. That
race needs two committed transactions, which no test case may have, so it lives
in `scripts/check-concurrency.py` T11 (12 runs, 5 rounds, two real connections,
every run attempted exactly once). These cases cover the walls the claim is built
from; T11 covers the full race.
"""

import io
import urllib.error
from unittest.mock import patch

from odoo.sql_db import db_connect
from odoo.tests import tagged

from odoo.addons.turnstone_integration.models.turnstone_automation_run import (
    DRAIN_LOCK_NAMESPACE,
)

from .common import TurnstoneTransactionCase

RUN = "turnstone.automation.run"
RULE = "turnstone.automation"


class _ServerError:
    """`urlopen` replaced by one that raises the way a broken server does.

    A stand-in for the socket, not for `TurnstoneClient`: the real client
    classifies the status, the real launch service cleans and records the
    message, and the real run model owns the transition. Only the failure is
    injected, so a case proves the chain rather than proving a stub agreed with
    itself.

    A 500 rather than an unreachable host on purpose: pointing the connection at
    a dead port would hit the fixture's own network pin (`common._mock_only_urlopen`
    refuses every authority but the mock) and the test would then be asserting on
    the fixture's `AssertionError`, not on a launch that failed. A server that
    answers 500 is also the more realistic transient failure — and the one whose
    retry must not become a second session.
    """

    def __init__(self, code=500, body='{"error": "upstream is unwell"}'):
        self.code = code
        self.body = body

    def __call__(self, request, timeout=None):
        raise urllib.error.HTTPError(
            getattr(request, "full_url", str(request)), self.code,
            "Internal Server Error", {}, io.BytesIO(self.body.encode()),
        )


class QueueDrainCase(TurnstoneTransactionCase):
    """Fixtures: a rule the drain can really launch, and mock-log helpers.

    The rule is a real one pointed at the fixture's mock connection, so a drain
    case is a launch observed by the server rather than a stub agreeing with
    itself. `_attempt_launch()` is deliberately NOT patched anywhere in this
    module: the claim is what is under test, and a stub on the far side of it
    would hide exactly the double launch these cases exist to catch.
    """

    def _rule(self, **vals):
        defaults = {
            "name": "Drain rule",
            "company_id": self.env.company.id,
            "agent_id": self.agent.id,
            # `manual`: the drain cases create their runs by hand, and a live
            # trigger would have an adapter queue extra runs behind them from
            # the fixtures' own writes.
            "trigger_type": "manual",
            # No `{{ }}` in the prompt, so a run with no trigger record still
            # renders — the drain's record resolution is OTI-5754's test, not
            # this module's subject.
            "prompt_template": "Check the queue.",
            "run_as": "fixed_user",
            "run_as_user_id": self.env.user.id,
        }
        defaults.update(vals)
        return self.env[RULE].create(defaults)

    def _queued_run(self, rule, key, **vals):
        """A run in the state every adapter leaves behind: `queued`, no session."""
        values = {"automation_id": rule.id, "idempotency_key": key}
        values.update(vals)
        return self.env[RUN].sudo().create(values)

    def _creates(self):
        """The workstream-create calls the mock received, in order.

        `endswith`, not a prefix: an interactive create goes to the routing
        proxy (`/v1/api/route/workstreams/new`) and a coordinator's to the
        console (`/v1/api/workstreams/new`), and "how many sessions did this
        launch start" must count both.
        """
        return [
            row for row in self.mock.request_log(limit=500)
            if row["path"].endswith("/workstreams/new")
        ]

    def _other_session(self):
        """A second, genuinely separate database connection.

        Not `registry.cursor()`: in test mode that is patched to return a
        `TestCursor` over this test's own connection, which is the whole reason
        a concurrency case cannot be written the obvious way. `db_connect()`
        borrows a real connection from the pool and is untouched by the patch.
        """
        return db_connect(self.env.cr.dbname).cursor()


@tagged("post_install", "-at_install")
class TestTwoDrainsClaimOnce(QueueDrainCase):
    """Criterion 1: two drains competing for one queued run claim it once."""

    def test_two_drain_sessions_claim_a_queued_run_exactly_once(self):
        """A real second connection holds the claim; this drain launches nothing.

        The other worker's `_claim_for_drain()` takes
        `pg_try_advisory_xact_lock(DRAIN_LOCK_NAMESPACE, run.id)` before it does
        anything else, so a second session holding that lock is exactly the state
        this worker finds when it loses the race. Nothing about the run row
        changes to make the claim fail — it is still `queued` — which is the
        point: the lock, not the state, is what refuses here.

        Advisory locks are server-global rather than MVCC, so the second session
        does not need to see this transaction's uncommitted row to contend for
        it. That is what makes this case a real cross-session proof rather than
        two cursors taking turns.
        """
        rule = self._rule()
        run = self._queued_run(rule, "event:once")

        other = self._other_session()
        try:
            other.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)",
                (DRAIN_LOCK_NAMESPACE, run.id),
            )
            self.assertTrue(
                other.fetchone()[0],
                "the stand-in worker must actually hold the claim lock, or the "
                "case below proves nothing",
            )
            attempted = self.env[RUN]._drain_queue()
            self.assertEqual(
                attempted.ids, [],
                "the worker that lost the claim must not report having "
                "attempted anything",
            )
            self.assertEqual(
                self._creates(), [],
                "a run another worker has claimed must not reach the server — "
                "this is the double launch the claim exists to prevent",
            )
            self.assertEqual(
                run.state, "queued",
                "the losing worker must leave the run exactly as it found it: "
                "the winner owns its state from here",
            )
        finally:
            # Ends that transaction, which is what releases a transaction-scoped
            # advisory lock; `close()` alone would too, but rollback is explicit
            # about not committing anything from the stand-in worker.
            other.rollback()
            other.close()

        self.env[RUN]._drain_queue()

        self.assertEqual(
            len(self._creates()), 1,
            "once the other worker is gone the run is claimed by this one, and "
            "the server must have seen exactly one create for it",
        )
        self.assertEqual(len(self.mock.workstreams()), 1)
        run.invalidate_recordset()
        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(run.attempt_count, 1)

    def test_a_second_drain_does_not_launch_the_same_run_again(self):
        """The wall that survives a re-entrant lock: the state re-read.

        `pg_try_advisory_xact_lock` is re-entrant inside one transaction, so a
        drain reached twice in the same one passes the lock. The second wall —
        re-reading the state from the database, not from this transaction's
        cache — is what stops the second pass from launching the finished run
        again.
        """
        rule = self._rule()
        run = self._queued_run(rule, "event:twice")

        self.env[RUN]._drain_queue()
        self.assertEqual(len(self._creates()), 1, "the first tick launches it")

        self.env[RUN]._drain_queue()

        self.assertEqual(
            len(self._creates()), 1,
            "a later tick must not launch a run that is already done; the "
            "server's log is the honest witness, because a stale `queued` in "
            "this transaction's cache would otherwise say it was still due",
        )
        self.assertEqual(len(self.mock.workstreams()), 1, "one event, one session")
        run.invalidate_recordset()
        self.assertEqual(run.state, "done", run.error)


@tagged("post_install", "-at_install")
class TestFailedRunRetriesOnce(QueueDrainCase):
    """Criterion 2: a failed attempt is retried, and never double-processed."""

    def test_a_failed_run_is_not_reprocessed_and_its_retry_delivers_once(self):
        """A transport failure, then one retry, and exactly one workstream.

        Three claims, in the order they matter:

        1. The attempt that failed did not reach the server at all (no create),
           and the run says so: `failed`, one attempt counted.
        2. The **next tick does not pick it up.** The drain searches `queued`
           only, so a failed run is a person's or an adapter's to retry, not
           something a cron silently re-attempts forever.
        3. The event's own retry reuses that run and its idempotency key, so the
           server ends up with **one** workstream — not two. That is what "does
           not double-process" means, and it is asserted on the server's log
           rather than on the run row, which would only prove what Odoo thinks.
        """
        rule = self._rule()
        run = self._queued_run(rule, "event:retry")

        with patch("urllib.request.urlopen", _ServerError()):
            self.env[RUN]._drain_queue()

        run.invalidate_recordset()
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 1, "a real attempt was made")
        self.assertEqual(
            self._creates(), [],
            "the failed attempt never reached the server, so there is nothing "
            "for it to have created",
        )

        self.assertEqual(
            self.env[RUN]._drain_queue().ids, [],
            "a failed run is not queued, so the next tick has nothing to do "
            "with it — re-attempting it forever would bury the reason",
        )
        run.invalidate_recordset()
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 1)

        retried = rule._launch_for_record(idempotency_key="event:retry")

        self.assertEqual(retried.id, run.id, "a retry reuses the event's run")
        self.assertEqual(retried.state, "done", retried.error)
        self.assertEqual(retried.attempt_count, 2, "both attempts are counted")
        self.assertEqual(
            len(self._creates()), 1,
            "two attempts at one event must produce one workstream: the retry "
            "presents the same idempotency token, so the server sees one create "
            "land rather than two sessions carrying one prompt",
        )
        self.assertEqual(len(self.mock.workstreams()), 1)


@tagged("post_install", "-at_install")
class TestOnlyQueuedRunsAreDrained(QueueDrainCase):
    """Criterion 3: a run in a terminal state is never picked up."""

    def test_terminal_and_in_flight_runs_are_not_picked_up(self):
        """Only `queued` is the drain's; everything else is history or someone's."""
        rule = self._rule()
        done = self._queued_run(rule, "event:done")
        done._mark_running()._mark_done()
        skipped = self._queued_run(rule, "event:skipped")
        skipped._mark_skipped("the rule's own window suppressed this event")
        running = self._queued_run(rule, "event:running")
        running._mark_running()
        queued = self._queued_run(rule, "event:queued")

        attempted = self.env[RUN]._drain_queue()

        self.assertEqual(
            attempted.ids, [queued.id],
            "the drain's answer must name the one run it actually attempted",
        )
        self.assertEqual(
            len(self._creates()), 1,
            "a `done` run is history, a `skipped` one was a decision not to "
            "launch, and a `running` one belongs to another worker: none of them "
            "may reach the server again",
        )
        self.assertEqual(len(self.mock.workstreams()), 1)
        for run, state in ((done, "done"), (skipped, "skipped"), (running, "running")):
            run.invalidate_recordset()
            self.assertEqual(run.state, state, "the drain changed a run it must not touch")
