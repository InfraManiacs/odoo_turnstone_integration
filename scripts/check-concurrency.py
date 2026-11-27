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

Exit status is 0 only when both hold. Printed lines are the evidence; run it
with `--verbose` for the per-round detail.
"""

import argparse
import functools
import os
import sys
import threading
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

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print("  - %s" % failure)
        _exit(1)
    print("\nOK: both concurrency checks hold.")
    _exit(0)


main()
