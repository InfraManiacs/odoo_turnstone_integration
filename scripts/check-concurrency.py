"""Two real transactions on one session: do they conflict, and does a send replay?

Run inside the addon's Odoo container against a throwaway database:

    docker compose run --rm --no-deps odoo \
        odoo shell --config=/etc/odoo/odoo.conf -d <db> --no-http \
        < scripts/check-concurrency.py

WHY THIS IS A SCRIPT AND NOT A `TransactionCase`

Every `TransactionCase` cursor is a `TestCursor` sharing ONE underlying
connection behind a re-entrant lock (`odoo/tests/test_cursor.py`), so two test
cursors can never be in flight at once. A concurrency test written that way
passes whatever the code does: it is not measuring concurrency at all. The
defect being guarded (OTI-5729 §J, RC-6) needs two genuine transactions, which
means two connections and a commit — neither of which a test case may have.

So the two checks whose whole point is concurrency live here, and the Odoo test
suite covers the parts that are single-threaded by nature (the lock's
exclusivity contract, the dropped-write path, the create's idempotency).

T9  Two concurrent `sync_from_server()` calls on one session must not raise a
    serialisation failure, and must leave each message exactly once.
T10 A send must reach the remote exactly once when the transaction it was made
    in is rolled back and the handler re-run -- Odoo's `retrying()` does exactly
    that on a `SerializationFailure`, up to five times.
T11 Two drain workers running the automation cron at the same time must not
    double-process a queued run: every run is attempted by exactly one worker.
T12 A launch must reach the remote exactly once when the transaction it was made
    in is rolled back and the handler re-run. Same defect as T10, on the launch
    path, and T10's own fix could not cover it: the launch's idempotency token
    was derived from a row the same transaction created, so a rollback destroyed
    the token along with the intent (OTI-5786). T11's number was already taken by
    the drain check above when this was added, so this is T12.

Exit status is 0 only when all of them hold. Printed lines are the evidence; run it
with `--verbose` for the per-round detail.
"""

import argparse
import contextlib
import functools
import os
import sys
import threading
import time
from unittest.mock import patch

import odoo
from odoo.modules.registry import Registry

from odoo.addons.turnstone_integration.models.turnstone_client import (
    TurnstoneClient,
    TurnstoneConflict,
)

class CannedClient(TurnstoneClient):
    """A Turnstone whose answers are fixed, and which records what it was asked.

    `created` is shared across instances on purpose: a create is idempotent at
    the REAL server by `ws_id` (409 on a repeat, verified 1.8.3), so the stub
    has to model that or a re-run would look harmless when it is not.
    """

    def __init__(self, messages=None, created=None):
        self.messages = list(messages or [])
        self.created = created if created is not None else {}
        self.calls = []

    def create_session(self, **kwargs):
        ws_id = kwargs.get("ws_id")
        self.calls.append(("create", ws_id))
        if ws_id is None:
            # What the real proxy does with no id offered: make a new one. This
            # is the whole point -- without a caller-supplied id there is
            # nothing to recognise a repeat by, so a re-run makes a SECOND
            # workstream and the user's message is delivered twice.
            ws_id = "ws-unnamed-%d" % (len(self.created) + 1)
        elif ws_id in self.created:
            raise TurnstoneConflict("HTTP 409: Workstream already exists")
        self.created[ws_id] = True
        return {"ws_id": ws_id, "node_id": "node-a"}

    def session_detail(self, ws_id):
        self.calls.append(("detail", ws_id))
        return {
            "persisted": {"ws_id": ws_id, "state": "idle", "kind": "interactive"},
            "live": {"state": "idle", "tool_calls": 0, "tokens": 0, "context_ratio": 0.0},
            "messages": list(self.messages),
        }

    def send(self, ws_id, message, kind=None):
        self.calls.append(("send", ws_id, message))
        return {}

    def cluster_workstreams(self, **kwargs):
        return {"workstreams": []}


def _env(cr):
    return odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})


@contextlib.contextmanager
def _patched_client(client):
    """Route every `TurnstoneClient` API call to `client`, wherever it is built.

    The four methods are patched on the **class**, not on
    `turnstone.connection._client()`. The post-commit hook that delivers a launch
    opens a cursor of its own and builds a fresh environment from it, so a patch
    on a model class reached through the caller's environment is not guaranteed
    to be the one that environment resolves — and when it is not, the real client
    runs against a stub URL and the check measures nothing. `TurnstoneClient` is
    one class object for the whole process, so patching its methods reaches every
    environment, including the hook's.

    Patching `__init__` is not enough and was tried: the class stays
    `TurnstoneClient`, so copying the canned instance's `__dict__` gives it the
    stub's *attributes* but not its *methods*, and the real `create_session`
    opens a socket.
    """
    def delegate(name):
        def call(self, *args, **kwargs):
            return getattr(client, name)(*args, **kwargs)
        return call

    with contextlib.ExitStack() as stack:
        for name in ("create_session", "session_detail", "send", "cluster_workstreams"):
            stack.enter_context(patch.object(TurnstoneClient, name, delegate(name)))
        yield client


def _session(env, name, **vals):
    connection = env["turnstone.connection"].search([], limit=1)
    if not connection:
        connection = env["turnstone.connection"].create({
            "name": "concurrency check",
            "base_url": "http://stub.invalid",
            "api_token": "stub-token",
            "is_default": True,
        })
    return env["turnstone.session"].create(
        dict({"name": name, "connection_id": connection.id}, **vals)
    )


def t9_concurrent_syncs(registry, session_id, rounds, verbose):
    """Two pollers, one session, the same transcript. Every round must be clean."""
    transcript = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    failures = []
    seen = []

    for round_no in range(rounds):
        # Both pollers answer the same transcript, so whichever wins the race
        # leaves the same rows: a duplicate would mean the mirror was written
        # twice for one response, which is exactly what used to happen.
        if round_no == 0:
            messages = list(transcript)
        else:
            messages = transcript + [
                {"role": "assistant", "content": "reply %d" % round_no},
            ]
        clients = {}
        barrier = threading.Barrier(2)
        results = {}

        def one(name):
            """One request: its own cursor, its own client, one commit."""
            try:
                with registry.cursor() as cr:
                    env = _env(cr)
                    session = env["turnstone.session"].browse(session_id)
                    client = clients.setdefault(name, CannedClient(messages))
                    with patch.object(
                        type(session.connection_id), "_client",
                        lambda self, timeout=None: client,
                    ):
                        barrier.wait(timeout=30)
                        session.sync_from_server()
                        env.flush_all()
                    cr.commit()
                results[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - the point is to classify it
                results[name] = "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0])

        threads = [threading.Thread(target=one, args=(n,)) for n in ("A", "B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        bad = {n: r for n, r in results.items() if r != "ok"}
        if bad:
            failures.append("round %d: %s" % (round_no, bad))
        if verbose:
            print("  T9 round %d: %s" % (round_no, results))

    with registry.cursor() as cr:
        env = _env(cr)
        session = env["turnstone.session"].browse(session_id)
        rows = [
            (m.role, m.body) for m in
            env["turnstone.message"].search([("session_id", "=", session.id)],
                                            order="sequence, id")
        ]
    duplicates = len(rows) - len({(role, body) for role, body in rows})
    # The mirror must be exactly the LAST transcript a poller applied: the
    # mirror follows the server, and the last round's response is the newest
    # thing the server said. Anything longer means two writers both landed;
    # anything shorter means a response was applied that was not the newest.
    expected = [(m["role"], m["content"]) for m in (
        transcript if rounds == 1 else messages
    )]
    if duplicates:
        failures.append("duplicate transcript rows: %d" % duplicates)
    if rows != expected:
        failures.append("mirror is %r, expected %r" % (rows, expected))
    seen.append((len(rows), duplicates))
    return failures, seen


def t10_send_under_retry(registry, verbose):
    """One logical first send, re-run the way `retrying()` re-runs it."""
    created = {}
    with registry.cursor() as cr:
        env = _env(cr)
        session = _session(env, "concurrency check: T10")
        session_id = session.id
        cr.commit()

    calls = []

    def patched(session, client):
        return patch.object(
            type(session.connection_id), "_client",
            lambda self, timeout=None: client,
        )

    def attempt(roll_back):
        with registry.cursor() as cr:
            env = _env(cr)
            session = env["turnstone.session"].browse(session_id)
            client = CannedClient([{"role": "user", "content": "hello"}], created=created)
            with patched(session, client):
                result = session.action_send("hello")
            calls.extend(client.calls)
            if roll_back:
                # `service.model.retrying()` rolls the transaction back and
                # re-runs the whole handler; nothing the attempt wrote survives.
                cr.rollback()
            else:
                cr.commit()
            return result

    attempt(roll_back=True)
    attempt(roll_back=False)

    failures = []
    creates = [c for c in calls if c[0] == "create"]
    sends = [c for c in calls if c[0] == "send"]
    if len(created) != 1:
        failures.append(
            "the remote ended up with %d workstreams for one logical send: %r"
            % (len(created), list(created))
        )
    if sends:
        failures.append(
            "the message travelled as a separate send %d time(s) instead of "
            "inside the create: %r" % (len(sends), sends)
        )
    with registry.cursor() as cr:
        env = _env(cr)
        session = env["turnstone.session"].browse(session_id)
        if session.ws_id != list(created)[0]:
            failures.append(
                "the retry did not adopt the workstream that already existed "
                "(row says %r, remote made %r)" % (session.ws_id, list(created))
            )
    if verbose:
        print("  T10 calls: %r" % (calls,))
        print("  T10 workstreams made: %d" % len(created))
    return failures, {"creates": len(creates), "workstreams": len(created)}


def t11_concurrent_drains(registry, run_count, rounds, verbose):
    """Two drain workers, one queue. Every run must be attempted exactly once.

    This is OTI-5754's third acceptance criterion, and it can only be checked
    with two genuine transactions on two connections: a `TransactionCase`
    serialises every cursor behind one re-entrant lock, so a concurrency test
    written there passes whatever the code does.

    `_attempt_launch()` is replaced with a recording stub so this measures the
    *claim*, not the network. That is the right seam: the launch itself is
    #5749/#5786's subject and has its own tests, while "did two workers both pick
    up the same row" is decided entirely by `_claim_for_drain()`. The stub still
    moves the row (`running` -> `done`), so the state re-read wall is exercised
    too, and it holds its claim long enough for the other worker to genuinely
    collide rather than miss it by luck.
    """
    run_model = "turnstone.automation.run"
    with registry.cursor() as cr:
        env = _env(cr)
        connection = env["turnstone.connection"].search([], limit=1)
        if not connection:
            connection = env["turnstone.connection"].create({
                "name": "concurrency check",
                "base_url": "http://stub.invalid",
                "api_token": "stub-token",
                "is_default": True,
            })
        # Re-entrant: a previous run of this script leaves its rule, agent and
        # runs behind, and the unique names make a second create fail. The
        # diagnostic cleans up after itself rather than needing a fresh database
        # per attempt. Committed before the creates, so the deletes cannot be
        # flushed into them.
        env["turnstone.automation"].with_context(active_test=False).search([
            ("name", "=", "concurrency check: drain rule"),
        ]).unlink()
        env["turnstone.agent"].search([
            ("name", "=", "drain check agent"),
        ]).unlink()
        cr.commit()
        agent = env["turnstone.agent"].create({
            "name": "drain check agent",
            "connection_id": connection.id,
            "kind": "interactive",
        })
        partner_model = env["ir.model"]._get("res.partner")
        rule = env["turnstone.automation"].create({
            "name": "concurrency check: drain rule",
            "company_id": env.company.id,
            "agent_id": agent.id,
            "trigger_type": "manual",
            "target_model_id": partner_model.id,
            "prompt_template": "Drain check.",
            "run_as": "fixed_user",
            "run_as_user_id": env.user.id,
        })
        run_ids = []
        for index in range(run_count):
            run = env[run_model].create({
                "automation_id": rule.id,
                "idempotency_key": "drain-check:%s" % index,
            })
            run_ids.append(run.id)
        cr.commit()

    failures = []
    attempts = []
    attempts_lock = threading.Lock()

    def record_launch(self, run, record, triggering_user, prompt):
        """Stand in for a real launch: record the claim, hold it, move the row.

        Same signature as `turnstone.automation._attempt_launch(self, run, record,
        triggering_user, prompt)` -- it is patched onto that model class, so
        `self` arrives first.
        """
        with attempts_lock:
            attempts.append(run.id)
        time.sleep(0.05)  # hold the claim so the other worker really collides
        run._mark_running()
        run._mark_done()

    for round_no in range(rounds):
        del attempts[:]
        barrier = threading.Barrier(2)
        results = {}

        def one(name):
            """One drain worker: its own connection, its own cron-sized batch."""
            try:
                with registry.cursor() as cr:
                    env = _env(cr)
                    runs = env[run_model]
                    # `_attempt_launch` lives on the RULE model -- it is one
                    # attempt of a rule's launch, not a method of the run. The
                    # class is looked up through the *registry*, not through
                    # `type(env[...])`: Odoo builds a model class per registry,
                    # so a class patched from this thread's environment would
                    # not be the one the other thread's environment uses.
                    rule_model = registry["turnstone.automation"]
                    original = rule_model._attempt_launch
                    rule_model._attempt_launch = record_launch
                    try:
                        barrier.wait(timeout=30)
                        # Both workers see the WHOLE queue, so nothing is left
                        # over simply because one worker's batch ended: the claim
                        # alone decides who gets each run.
                        runs._drain_queue(limit=len(run_ids))
                        env.flush_all()
                    finally:
                        rule_model._attempt_launch = original
                    cr.commit()
                results[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - the point is to classify it
                results[name] = "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0])

        threads = [
            threading.Thread(target=one, args=(name,)) for name in ("A", "B")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        bad = {name: result for name, result in results.items() if result != "ok"}
        if bad:
            failures.append("round %d: a drain worker failed: %s" % (round_no, bad))
        duplicated = sorted(
            {run_id for run_id in attempts if attempts.count(run_id) > 1}
        )
        if duplicated:
            failures.append(
                "round %d: two workers launched the same run(s) %r"
                % (round_no, duplicated)
            )
        missing = sorted(set(run_ids) - set(attempts))
        if missing:
            failures.append(
                "round %d: no worker launched run(s) %r" % (round_no, missing)
            )
        if verbose:
            print("  T11 round %d: %d attempt(s) for %d run(s)"
                  % (round_no, len(attempts), len(run_ids)))

        # Refill the queue for the next round. A direct UPDATE because `write()`
        # refuses to move a finished run back to `queued` -- the state graph is
        # the guard under test elsewhere, and this is a fixture standing in for
        # "the queue filled up again", not a behaviour being tested.
        if round_no + 1 < rounds:
            with registry.cursor() as cr:
                cr.execute(
                    """UPDATE turnstone_automation_run
                          SET state = 'queued', attempt_count = 0,
                              started_at = NULL, finished_at = NULL,
                              session_id = NULL
                        WHERE id = ANY(%s)""",
                    [run_ids],
                )
                cr.commit()

    return failures, {"runs": len(run_ids), "attempts": len(attempts)}



def t12_launch_under_retry(registry, verbose):
    """One launch, re-run the way `retrying()` re-runs it. Must reach the remote once.

    T10's defect, on the launch path. A send's workstream id derives from a
    session row that **pre-exists** its transaction, so a rollback leaves the
    token intact and the re-run presents the same id. A launch's did not: the
    session row was created in the same transaction, so a rollback took the id
    with it, the re-run minted a new one, the proxy had nothing to answer 409
    about, and a second workstream carrying the same prompt was created.

    Two things make this a script rather than a test case, and both are the same
    reason T10 is here. It needs a **real** `rollback()`, which a
    `TransactionCase` patches to raise (`odoo/tests/common.py`), and it needs a
    real `commit()` to fire the post-commit hook that phase two runs on. In a test
    case `TestCursor.commit()` *clears* `postcommit` instead of running it, so a
    launch there never delivers at all — which is why the module drives
    `_deliver_pending()` by hand. Neither substitute can show this defect: the
    whole question is what survives a transaction boundary, and there is no
    boundary inside one cursor.

    The stub stands in for the network only. The launch service, its two-phase
    split, `_arm_delivery()`, the real post-commit hook and `_deliver_queued_run()`
    all run, and `created` is shared across the two attempts so the stub's 409
    behaves like the real proxy's: a repeat of an id it already made is refused,
    a new id is accepted. That is what makes the count below mean "one workstream
    or two", not merely "create was called twice".
    """
    created = {}
    calls = []
    # Unique per invocation, because the script leaves its rows behind: a fixed
    # key would find the previous run's `done` row, answer the launch with it as
    # a duplicate, and pass while measuring nothing.
    key = "launch-retry-check:%s" % int(time.time() * 1000)

    with registry.cursor() as cr:
        env = _env(cr)
        connection = env["turnstone.connection"].search([], limit=1)
        if not connection:
            connection = env["turnstone.connection"].create({
                "name": "concurrency check",
                "base_url": "http://stub.invalid",
                "api_token": "stub-token",
                "is_default": True,
            })
        # Re-entrant, for the same reason T11 is: the diagnostic leaves its
        # fixtures behind and a second run has to be able to start.
        env["turnstone.automation"].with_context(active_test=False).search([
            ("name", "like", "concurrency check: launch retry rule"),
        ]).unlink()
        env["turnstone.agent"].search([
            ("name", "like", "launch retry check agent"),
        ]).unlink()
        cr.commit()
        agent = env["turnstone.agent"].create({
            "name": "launch retry check agent %s" % key,
            "connection_id": connection.id,
            "kind": "interactive",
        })
        rule = env["turnstone.automation"].create({
            "name": "concurrency check: launch retry rule %s" % key,
            "company_id": env.company.id,
            "agent_id": agent.id,
            "trigger_type": "manual",
            "prompt_template": "Launch retry check.",
            "run_as": "fixed_user",
            "run_as_user_id": env.user.id,
        })
        rule_id = rule.id
        cr.commit()

    def attempt(roll_back):
        """One run of the handler. Both attempts share one stub and its state.

        Two details are load-bearing and were both got wrong first:

        * the cursor is managed by hand, not with `with registry.cursor()`.
          `Cursor.__exit__` **commits** on a clean exit, so returning after
          `cr.rollback()` from inside a `with` block would commit the very
          transaction that was just rolled back and run the delivery anyway --
          the check would silently become a test of the commit path.
        * the client patch wraps the `cr.commit()` too. The delivery's calls are
          made by the post-commit hook **during** that commit, so a patch that
          had already exited would let the real client run against the stub URL
          and the check would measure nothing.
        """
        client = CannedClient([], created=created)
        with _patched_client(client):
            cr = registry.cursor()
            try:
                env = _env(cr)
                rule = env["turnstone.automation"].browse(rule_id)
                rule._launch_for_record(idempotency_key=key)
                if roll_back:
                    # `service.model.retrying()` rolls the transaction back and
                    # re-runs the whole handler. `rollback()` clears
                    # `cr.postcommit`, so the armed delivery never runs and no
                    # create is made -- the guarantee under test. The run row
                    # goes with it, which is why the re-run is not a duplicate.
                    cr.rollback()
                else:
                    # A committed launch: the hook fires on commit, in a cursor
                    # of its own, exactly as it does in production.
                    cr.commit()
            finally:
                # Never commits on the way out: a rolled-back attempt stays
                # rolled back, and a committed one has nothing left to say.
                cr.close()
        calls.extend(client.calls)

    attempt(roll_back=True)
    attempt(roll_back=False)

    failures = []
    # The run is re-read **by key**, not taken from `_launch_for_record()`'s
    # return value: the service returns an empty recordset on the retry path when
    # it loses the row-lock claim, which is correct for its callers and not what
    # this check is about. What it is about is what ended up in the database and
    # at the remote.
    with registry.cursor() as cr:
        env = _env(cr)
        run = env["turnstone.automation.run"].sudo().search([
            ("automation_id", "=", rule_id),
            ("idempotency_key", "=", key),
        ], limit=1)
        if not run:
            failures.append(
                "the re-run left no run row at all for key %r; the event was "
                "lost, which a rollback-retry must never do" % key
            )
            return failures, {"creates": 0, "workstreams": len(created)}
        # Read inside the block: the recordset's cursor is closed when it exits,
        # and a lazy field read afterwards raises "Cursor already closed" instead
        # of telling us what happened.
        state, error = run.state, run.error or ""
        adopted_ws_id = run.session_id.ws_id or None

    creates = [c for c in calls if c[0] == "create"]
    unnamed = [c for c in creates if c[1] is None]
    if unnamed:
        failures.append(
            "%d create(s) presented no ws_id at all, so the remote had nothing "
            "to recognise a repeat by: %r" % (len(unnamed), unnamed)
        )
    if len(created) != 1:
        failures.append(
            "the remote ended up with %d workstream(s) for one logical launch: %r"
            % (len(created), list(created))
        )
    if state != "done":
        failures.append(
            "the re-run did not deliver the launch (run is %r: %s)" % (state, error)
        )
    made = (list(created) or [None])[0]
    if made is not None and adopted_ws_id != made:
        failures.append(
            "the run did not adopt the workstream that was made (row says %r, "
            "remote made %r)" % (adopted_ws_id, made)
        )
    if verbose:
        print("  T12 calls: %r" % (calls,))
        print("  T12 workstreams made: %d" % len(created))
        print("  T12 final run: %r" % (state,))
    return failures, {"creates": len(creates), "workstreams": len(created)}


def _exit(status):
    """Leave without Odoo's shell trying to keep going.

    `os._exit` skips stdio flushing, and a piped stdout is block-buffered -- so
    a failing run would lose the very lines that say what failed. Flush first.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--db", default=None)
    args, _odoo_argv = parser.parse_known_args()

    dbname = args.db or os.environ.get("PGDATABASE") or odoo.tools.config["db_name"]
    if isinstance(dbname, list):
        dbname = dbname[0]
    if not dbname:
        print("FAIL: no database name; pass --db or -d to `odoo shell`.")
        _exit(2)

    registry = Registry(dbname)
    failures = []

    print("T9  two concurrent syncs on one session (%d rounds)" % args.rounds)
    with registry.cursor() as cr:
        env = _env(cr)
        session = _session(env, "concurrency check: T9", ws_id="ws-t9", state="idle")
        session_id = session.id
        cr.commit()
    found, seen = t9_concurrent_syncs(registry, session_id, args.rounds, args.verbose)
    failures.extend(found)
    print("T9  %s (mirror ended at %d rows)" % (
        "PASS: no serialisation failure, each message once" if not found
        else "FAIL", seen[-1][0] if seen else 0))

    print("T10 a send must reach the remote exactly once under a transaction retry")
    found, stats = t10_send_under_retry(registry, args.verbose)
    failures.extend(found)
    print("T10 %s (%d create call(s), %d workstream(s) at the remote)" % (
        "PASS: one send, one workstream, retry adopted it" if not found else "FAIL",
        stats["creates"], stats["workstreams"]))

    print("T11 two drain workers must not double-process a queued run")
    found, stats = t11_concurrent_drains(registry, 12, args.rounds, args.verbose)
    failures.extend(found)
    print("T11 %s (%d run(s), last round attempted %d)"
          % ("PASS: every run attempted exactly once" if not found else "FAIL",
             stats["runs"], stats["attempts"]))

    print("T12 a launch must reach the remote exactly once under a transaction retry")
    found, stats = t12_launch_under_retry(registry, args.verbose)
    failures.extend(found)
    print("T12 %s (%d create call(s), %d workstream(s) at the remote)"
          % ("PASS: one launch, one workstream, the re-run delivered it" if not found
             else "FAIL", stats["creates"], stats["workstreams"]))

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print("  - %s" % failure)
        _exit(1)
    print("\nOK: all concurrency checks hold.")
    _exit(0)


main()
