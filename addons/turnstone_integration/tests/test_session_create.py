"""A session's first message survives a create that made no workstream.

The one failure shape in this module that destroys something the user typed:
Turnstone answers a create with HTTP 200 and no usable ``ws_id`` (an unroutable
node, a half-initialised workstream), Odoo used to file the session as
``thinking`` anyway, and ``action_send`` then short-circuited on the empty id —
so the message existed in neither Odoo nor Turnstone, with no error anywhere.
Reported as OTI-5729 shape 1, root cause RC-1, fixed by OTI-5765.

These cases are the investigation's regression cases T1 and T2, plus the
``approve()`` no-op the fix must not disturb. They stub the seam the
investigation named — ``turnstone_connection._client()`` — so they need no
network and no mock server: every case is about how Odoo READS a response, and
a canned response is the whole input.

T2 asserts on a stub too, deliberately. It is here as the guard that the fix did
not break the happy path, which is a claim about this file's own logic; the
end-to-end path through a real HTTP stack belongs to the mock-server suite.
"""

from unittest.mock import patch

from odoo.exceptions import UserError

from odoo.addons.turnstone_integration.models.turnstone_client import (
    TurnstoneClient,
    TurnstoneError,
)
from odoo.tests import TransactionCase, tagged


class StubClient(TurnstoneClient):
    """A TurnstoneClient whose every answer is canned, and which records calls."""

    def __init__(self, create=None, detail=None, send=None, approve=None):
        # Deliberately not super().__init__({}): the real one raises on an empty
        # base_url, and none of these cases reach the transport.
        self._create = create if create is not None else {"ws_id": "ws-1"}
        self._detail = detail if detail is not None else self._default_detail()
        self._send = send
        self._approve = approve if approve is not None else {}
        self.calls = []

    @staticmethod
    def _default_detail(messages=None):
        return {
            "persisted": {"ws_id": "ws-1", "state": "idle", "kind": "interactive"},
            "live": {"state": "idle", "tool_calls": 0, "tokens": 0, "context_ratio": 0.0},
            "messages": messages if messages is not None else [],
        }

    def create_session(self, **kwargs):
        self.calls.append(("create_session", kwargs))
        return self._create

    def session_detail(self, ws_id):
        self.calls.append(("session_detail", ws_id))
        return self._detail

    def send(self, ws_id, message, kind=None):
        self.calls.append(("send", ws_id, message))
        return self._send if self._send is not None else {}

    def approve(self, ws_id, approved, feedback=None, cycle_id=None, kind=None):
        self.calls.append(("approve", ws_id, approved))
        return self._approve

    def cluster_workstreams(self, state=None, search=None, page=1, per_page=100):
        # _sync_subagents runs on every sync_from_server; no children is the
        # normal answer for a session nothing has spawned from.
        return {"workstreams": []}

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


@tagged("post_install", "-at_install")
class TestSessionCreateWithoutWorkstream(TransactionCase):
    """T1: a create that returns no ws_id raises instead of swallowing the message."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Stub Turnstone",
            "base_url": "http://stub.invalid",
            "api_token": "stub-token",
            "is_default": True,
        })
        cls.session = cls.env["turnstone.session"].create({
            "name": "Ghost session",
            "connection_id": cls.connection.id,
        })

    def _patched(self, client):
        return patch.object(type(self.connection), "_client", lambda self, timeout=None: client)

    def _assert_untouched(self, session):
        """Everything the destroyed-message shape used to corrupt, asserted together."""
        self.assertFalse(session.ws_id, "no workstream id may be written")
        self.assertEqual(session.state, "draft", "a session with no workstream is a draft")
        self.assertFalse(session.message_ids, "no message may be mirrored")
        self.assertFalse(session.node_id)

    def test_create_with_error_body_raises_and_leaves_draft(self):
        """T1, error shape: HTTP 200 with {"error": "no node available"}."""
        client = StubClient(create={"error": "no node available for routing"})
        with self._patched(client):
            with self.assertRaises(TurnstoneError) as caught:
                self.session.action_send("the first question")
        message = str(caught.exception)
        self.assertIn("no node available for routing", message,
                      "the raise must name what the server said")
        self.assertNotIn("stub-token", message, "the raise must not leak the API token")
        self._assert_untouched(self.session)
        self.assertFalse(client.called("send"),
                         "a create that made no workstream must not be followed by a send")

    def test_create_with_empty_body_raises_and_leaves_draft(self):
        """T1, bare shape: HTTP 200 with {} — no error key, no ws_id either."""
        client = StubClient(create={})
        with self._patched(client):
            with self.assertRaises(TurnstoneError) as caught:
                self.session.action_send("the first question")
        self.assertIn("no usable workstream id", str(caught.exception))
        self._assert_untouched(self.session)

    def test_create_with_error_and_ws_id_still_raises(self):
        """An error body is an error even when a ws_id rides along.

        Same reading `_fetch_detail` applies to a detail response (OTI-5744):
        the console says an error when it means one, and a half-built
        workstream it flagged is not one to start writing into.
        """
        client = StubClient(create={"ws_id": "ws-half", "error": "workstream not ready"})
        with self._patched(client):
            with self.assertRaises(TurnstoneError):
                self.session.action_send("the first question")
        self._assert_untouched(self.session)

    def test_retry_after_a_refused_create_still_sends(self):
        """The refusal leaves the session usable: the next send starts clean.

        This is what makes the raise safe rather than merely louder — the user
        re-sends, the server is healthy this time, and the conversation starts.
        """
        refusing = StubClient(create={"error": "no node available for routing"})
        with self._patched(refusing):
            with self.assertRaises(TurnstoneError):
                self.session.action_send("the first question")
        self._assert_untouched(self.session)

        working = StubClient(
            create={"ws_id": "ws-2"},
            detail=StubClient._default_detail([
                {"role": "user", "content": "the first question"},
                {"role": "assistant", "content": "an answer"},
            ]),
        )
        with self._patched(working):
            result = self.session.action_send("the first question")
        self.assertEqual(self.session.ws_id, "ws-2")
        self.assertEqual(result["ws_id"], "ws-2")
        self.assertEqual(len(working.called("send")), 0,
                         "the first message travels in the create, not a separate send")

    def test_attachment_on_a_refused_create_raises_too(self):
        """The other _ensure_started caller gets the same refusal, not a ghost."""
        self.session.kind = "coordinator"
        client = StubClient(create={"error": "no node available"})
        with self._patched(client):
            with self.assertRaises(TurnstoneError):
                self.session.action_add_attachment("a.txt", "aGVsbG8=", "text/plain")
        self._assert_untouched(self.session)


@tagged("post_install", "-at_install")
class TestSessionCreateHappyPath(TransactionCase):
    """T2: a create that does make a workstream behaves exactly as before."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Stub Turnstone",
            "base_url": "http://stub.invalid",
            "api_token": "stub-token",
            "is_default": True,
        })
        cls.session = cls.env["turnstone.session"].create({
            "name": "Real session",
            "connection_id": cls.connection.id,
        })

    def _patched(self, client):
        return patch.object(type(self.connection), "_client", lambda self, timeout=None: client)

    def test_first_send_records_ws_id_thinking_and_the_transcript(self):
        client = StubClient(
            create={"ws_id": "ws-1", "node_id": "node-a"},
            detail=StubClient._default_detail([
                {"role": "user", "content": "the first question"},
                {"role": "assistant", "content": "an answer"},
            ]),
        )
        with self._patched(client):
            result = self.session.action_send("the first question")

        # The create carried the message; no separate send is made for it.
        create = client.called("create_session")[0][1]
        self.assertEqual(create["initial_message"], "the first question")
        self.assertEqual(len(client.called("send")), 0)

        self.assertEqual(self.session.ws_id, "ws-1")
        self.assertEqual(self.session.node_id, "node-a")
        # The final state comes from the detail, which here says idle — the
        # point of the assertion is that a ws_id exists and the mirror synced.
        self.assertEqual(result["state"], "idle")
        self.assertEqual([m["body"] for m in result["messages"]],
                         ["the first question", "an answer"])

    def test_send_to_a_started_session_uses_send_not_create(self):
        """A second message must not re-create the workstream."""
        self.session.sudo().write({"ws_id": "ws-1", "state": "idle"})
        client = StubClient(detail=StubClient._default_detail([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]))
        with self._patched(client):
            self.session.action_send("a second question")
        self.assertEqual(len(client.called("create_session")), 0)
        sent = client.called("send")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1:], ("ws-1", "a second question"))


@tagged("post_install", "-at_install")
class TestApprovalNoOpUnaffected(TransactionCase):
    """approve()'s deliberate {"already_resolved": True} return keeps working.

    It has no ``error`` key, so neither the detail path's error-body reading
    (OTI-5744) nor this ticket's create guard touches it. Without a ws_id
    however approve() still refuses with UserError, which is its own documented
    behaviour and is kept.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Stub Turnstone",
            "base_url": "http://stub.invalid",
            "api_token": "stub-token",
            "is_default": True,
        })

    def _patched(self, client):
        return patch.object(type(self.connection), "_client", lambda self, timeout=None: client)

    def test_already_resolved_is_a_noop_not_an_error(self):
        session = self.env["turnstone.session"].create({
            "name": "Approval session",
            "connection_id": self.connection.id,
            "ws_id": "ws-1",
            "state": "attention",
        })
        client = StubClient(
            approve={"already_resolved": True},
            detail=StubClient._default_detail([]),
        )
        with self._patched(client):
            result = session.action_approve(True)

        self.assertTrue(client.called("approve"), "the decision must still be sent")
        self.assertEqual(result["ws_id"], "ws-1")
        # The no-op branch returns whatever sync reports and logs nothing:
        # one approval must not produce two audit rows.
        self.assertFalse(self.env["turnstone.audit.log"].search([
            ("session_id", "=", session.id), ("action", "=", "tool_approve"),
        ]))

    def test_approve_without_a_workstream_is_still_refused(self):
        session = self.env["turnstone.session"].create({
            "name": "Unstarted",
            "connection_id": self.connection.id,
        })
        client = StubClient()
        with self._patched(client):
            with self.assertRaises(UserError) as caught:
                session.action_approve(True)
        self.assertIn("has not started", str(caught.exception))
        self.assertFalse(client.called("approve"))
