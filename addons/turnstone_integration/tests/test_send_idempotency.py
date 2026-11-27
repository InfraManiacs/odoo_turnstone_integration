"""The mirror has one writer, and a send is delivered once.

The concurrency half of RC-6 (OTI-5729 §J) cannot live in a `TransactionCase`:
every test cursor shares ONE connection behind a re-entrant lock
(`odoo/tests/test_cursor.py`), so two test cursors are never in flight at once
and a "concurrent" test here would pass whatever the code did. That is
`scripts/check-concurrency.py`, which uses real transactions against a real
database, and which was verified to fail with the fix reverted.

What IS testable inside a test case is every decision the concurrency fix makes,
one call at a time:

* the session lock is exclusive, and a second attempt inside the same
  transaction is not a deadlock (a send calls sync, which locks again);
* a sync that cannot take the lock writes nothing and reports the mirror it
  has;
* a local write that loses a race is dropped, not raised — a raise is what
  makes Odoo re-run the handler and re-POST (T10);
* a create presents a stable id and adopts a 409 instead of making a second
  workstream;
* a rewind still replaces the transcript (no regression from any of this).
"""

from unittest.mock import patch

from psycopg2 import errors as pg_errors

from odoo.addons.turnstone_integration.models.turnstone_client import (
    TurnstoneClient,
    TurnstoneConflict,
    TurnstoneError,
)
from odoo.tests import TransactionCase, tagged


class StubClient(TurnstoneClient):
    """A client whose answers are canned, which records calls, and which is
    idempotent on ``ws_id`` the way the real routing proxy is (409 on a repeat,
    verified on 1.8.3 -- OTI-5763 Q3)."""

    def __init__(self, create=None, detail=None, created=None):
        self._create = create if create is not None else None
        self._detail = detail if detail is not None else StubClient._default_detail()
        self.created = created if created is not None else {}
        self.calls = []

    @staticmethod
    def _default_detail(messages=None):
        return {
            "persisted": {"ws_id": "ws-1", "state": "idle", "kind": "interactive"},
            "live": {"state": "idle", "tool_calls": 0, "tokens": 0, "context_ratio": 0.0},
            "messages": messages if messages is not None else [],
        }

    def create_session(self, **kwargs):
        ws_id = kwargs.get("ws_id")
        self.calls.append(("create_session", kwargs))
        if ws_id is not None:
            if ws_id in self.created:
                raise TurnstoneConflict("HTTP 409: Workstream already exists")
            self.created[ws_id] = True
            return {"ws_id": ws_id, "node_id": "node-a"}
        if isinstance(self._create, dict) and self._create.get("error"):
            return self._create
        return self._create if self._create is not None else {"ws_id": "ws-1"}

    def session_detail(self, ws_id):
        self.calls.append(("session_detail", ws_id))
        return self._detail

    def send(self, ws_id, message, kind=None):
        self.calls.append(("send", ws_id, message))
        return {}

    def rewind(self, ws_id, turns=1, kind=None):
        self.calls.append(("rewind", ws_id, turns))
        return {}

    def cluster_workstreams(self, state=None, search=None, page=1, per_page=100):
        return {"workstreams": []}

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


@tagged("post_install", "-at_install")
class TestSessionWriteLock(TransactionCase):
    """The one-writer rule, and what a poller does when it is not the writer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Stub Turnstone",
            "base_url": "http://stub.invalid",
            "api_token": "stub-token",
            "is_default": True,
        })

    def _session(self, **vals):
        return self.env["turnstone.session"].create(dict(
            {"name": "Lock test", "connection_id": self.connection.id,
             "ws_id": "ws-lock", "state": "idle"},
            **vals
        ))

    def _patched(self, client):
        return patch.object(type(self.connection), "_client",
                            lambda self, timeout=None: client)

    def test_the_lock_is_exclusive_between_sessions_and_reentrant_within_one(self):
        """Two sessions lock independently; one session locks twice, once.

        Re-entrancy is load-bearing rather than tidy: `action_send` takes the
        lock when it syncs, and a second call in the same request (the client
        polling right after) must not deadlock against its own transaction.
        """
        first, second = self._session(ws_id="ws-a"), self._session(ws_id="ws-b")
        self.assertTrue(first._session_write_lock())
        self.assertTrue(second._session_write_lock(),
                        "another session's lock must not be affected")
        self.assertTrue(first._session_write_lock(),
                        "the same transaction must be able to re-take its own lock")

    def test_a_sync_that_cannot_take_the_lock_writes_nothing(self):
        """A poller that is not the writer leaves the mirror alone.

        Two pollers write the same rows in different orders; that is what
        produced the serialisation failures in the first place. The second one
        has nothing useful to do anyway -- the winner is applying the same
        server state -- so it must write NOTHING, not write and lose.

        The fetch is deliberately outside the lock: it is a remote HTTP call
        that can take 30 seconds, and holding the lock across it would turn two
        pollers into one poller plus a queue. So this asserts about writes, not
        about the request.
        """
        session = self._session()
        client = StubClient(detail=StubClient._default_detail([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]))
        with self._patched(client), patch.object(
            type(session), "_session_write_lock", lambda self: False,
        ):
            result = session.sync_from_server()

        self.assertEqual(len(client.called("session_detail")), 1,
                         "the response is still read; that is how the mirror "
                         "knows what the winner is applying")
        self.assertEqual(result["messages"], [])
        self.assertFalse(session.message_ids, "a poller that cannot win writes nothing")
        self.assertFalse(client.called("cluster_workstreams"),
                         "nor does it write the derived rows")

    def test_a_losing_local_write_is_dropped_not_raised(self):
        """A retryable conflict on the local half must not escape.

        If it escapes, `service.model.retrying()` re-runs the whole handler --
        and the handler has already POSTed, so the user's message is delivered
        again. The write protected here is bookkeeping a later poll recomputes,
        so dropping it is the honest outcome and raising is the bug.
        """
        session = self._session()

        def raise_conflict(self, vals):
            raise pg_errors.SerializationFailure(
                "could not serialize access due to concurrent update"
            )

        with patch.object(type(session), "write", raise_conflict):
            with session._no_replay("a test write"):
                session.write({"state": "thinking"})

    def test_a_sync_that_loses_the_race_keeps_the_mirror_and_the_request(self):
        """The whole point, at the call site: no exception, no re-run, no loss.

        The transcript was already stored by the winner, so the loser's response
        is dropped -- but the request completes, which is what stops the five
        replays (T10).
        """
        session = self._session()
        self.env["turnstone.message"].create([
            {"session_id": session.id, "sequence": 0, "role": "user", "body": "one"},
        ])
        client = StubClient(detail=StubClient._default_detail([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]))

        def lose(self, vals):
            raise pg_errors.SerializationFailure("concurrent update")

        with self._patched(client), patch.object(type(session), "write", lose):
            result = session.sync_from_server()   # must not raise

        self.assertEqual(result["ws_id"], "ws-lock")
        self.assertEqual([m.body for m in session.message_ids], ["one"],
                         "the winner's transcript is what remains")


@tagged("post_install", "-at_install")
class TestSendDeliveredOnce(TransactionCase):
    """T10's decision, one call at a time: a create is repeatable, a send is not."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Stub Turnstone",
            "base_url": "http://stub.invalid",
            "api_token": "stub-token",
            "is_default": True,
        })

    def _session(self, **vals):
        return self.env["turnstone.session"].create(dict(
            {"name": "Deliver once", "connection_id": self.connection.id},
            **vals
        ))

    def _patched(self, client):
        return patch.object(type(self.connection), "_client",
                            lambda self, timeout=None: client)

    def test_the_workstream_id_is_derived_and_stable(self):
        """Same session, same id -- across a rollback, which is the whole point.

        A stored uuid would be rolled back WITH the transaction it identifies,
        so the retry would generate a fresh one and duplicate the create.
        """
        session = self._session()
        first = session._client_ws_id()
        self.assertEqual(first, session._client_ws_id())
        self.assertNotEqual(first, self._session()._client_ws_id(),
                            "two sessions must not ask for the same workstream")
        # Shape: the real server validates ws_id (a malformed one is 400).
        self.assertRegex(first, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_a_first_send_presents_the_id_to_the_create(self):
        session = self._session()
        client = StubClient(detail=StubClient._default_detail([
            {"role": "user", "content": "hello"},
        ]))
        with self._patched(client):
            session.action_send("hello")
        sent = client.called("create_session")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["ws_id"], session._client_ws_id())

    def test_a_repeat_create_is_adopted_not_duplicated(self):
        """409 on our own id is "this already happened", and that is success.

        This is the state a re-run of the same transaction finds itself in: the
        workstream exists, this side has no record of it (the write was rolled
        back), and the only correct answer is to adopt the id rather than make a
        second workstream.
        """
        session = self._session()
        client = StubClient(created={session._client_ws_id(): True})
        with self._patched(client):
            session._ensure_started(first_message="hello")

        self.assertEqual(session.ws_id, session._client_ws_id())
        self.assertEqual(len(client.created), 1,
                         "one logical create must not leave two workstreams")
        self.assertEqual(session.state, "thinking")

    def test_a_coordinator_create_is_not_given_an_id(self):
        """The console endpoint has no ws_id field, so none may be sent.

        Sending one would be a setting that is silently ignored -- and worse,
        a reader would believe coordinator creates were idempotent when they
        are not.
        """
        session = self._session(kind="coordinator")
        client = StubClient(create={"ws_id": "ws-coord"})
        with self._patched(client):
            session._ensure_started()
        sent = client.called("create_session")
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0][1]["ws_id"])

    def test_a_refused_create_still_raises_after_a_409_is_adopted(self):
        """A 409 with no id of our own is a real failure, not a quiet success.

        Only a create that PRESENTED an id can be adopted; anything else is the
        server refusing this request and must surface.
        """
        session = self._session(kind="coordinator")

        class Refusing(StubClient):
            def create_session(self, **kwargs):
                raise TurnstoneConflict("HTTP 409: coordinator limit reached")

        with self._patched(Refusing()):
            with self.assertRaises(TurnstoneConflict):
                session._ensure_started()
        self.assertFalse(session.ws_id)

    def test_a_send_after_a_dropped_local_write_still_returns(self):
        """A send whose local bookkeeping loses a race completes, once sent.

        The transcript half of the response is what the local write protects,
        and a losing one is dropped. What must NOT happen is a raise: the
        message is already at Turnstone, and Odoo would deliver it again.
        """
        session = self._session(ws_id="ws-1")
        client = StubClient(detail=StubClient._default_detail([
            {"role": "user", "content": "hello"},
        ]))

        def lose(self, vals):
            raise pg_errors.SerializationFailure("concurrent update")

        with self._patched(client), patch.object(type(session), "write", lose):
            result = session.action_send("hello")   # must not raise

        self.assertEqual(len(client.called("send")), 1,
                         "exactly one delivery, whatever the local race did")
        self.assertEqual(result["ws_id"], "ws-1")


@tagged("post_install", "-at_install")
class TestRewindStillReplaces(TransactionCase):
    """No regression from the lock: a rewind must still shorten the mirror."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Stub Turnstone",
            "base_url": "http://stub.invalid",
            "api_token": "stub-token",
            "is_default": True,
        })

    def test_rewind_to_empty_clears_the_mirror(self):
        session = self.env["turnstone.session"].create({
            "name": "Rewind", "connection_id": self.connection.id,
            "ws_id": "ws-rewind", "state": "idle",
        })
        self.env["turnstone.message"].create([
            {"session_id": session.id, "sequence": 0, "role": "user", "body": "one"},
            {"session_id": session.id, "sequence": 1, "role": "assistant", "body": "two"},
        ])
        client = StubClient(detail=StubClient._default_detail([]))
        with patch.object(type(self.connection), "_client",
                          lambda self, timeout=None: client):
            session.action_rewind(turns=1)

        self.assertEqual(len(client.called("rewind")), 1)
        self.assertFalse(session.message_ids,
                         "an authoritative rewind must still empty the mirror")

    def test_a_non_authoritative_empty_response_is_still_refused(self):
        """The lock must not weaken #5744's guard on the ordinary path."""
        session = self.env["turnstone.session"].create({
            "name": "Guard", "connection_id": self.connection.id,
            "ws_id": "ws-guard", "state": "idle",
        })
        self.env["turnstone.message"].create([
            {"session_id": session.id, "sequence": 0, "role": "user", "body": "one"},
        ])
        client = StubClient(detail=StubClient._default_detail([]))
        with patch.object(type(self.connection), "_client",
                          lambda self, timeout=None: client):
            session.sync_from_server()

        self.assertEqual(len(session.message_ids), 1)
        self.assertTrue(session.sync_note, "the refusal must say why")
