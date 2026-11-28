"""A chat's history survives every way a user comes back to it.

WHY THIS FILE EXISTS

"My chat history disappeared" was reported (OTI-5729) as one bug and is really
several, each reachable from a different way of returning to a conversation. The
fixes that landed are unit-tested where they live — the sync rules in
``test_transcript_fetch_limit``, the create boundary in ``test_session_create`` —
but nothing drove the **journey** that produced the report: leave the chat, come
back; reload; log out and in; start the same agent twice; a workstream deleted
on the server; a server error.

So each class below is one of those journeys, run through the JSON-RPC routes the
OWL client actually calls (``/turnstone/bootstrap``, ``/turnstone/session/get``,
``/turnstone/session/send``) as a real logged-in user, against the mock Turnstone
server. Transcripts are planted on the mock with its seed helper **before**
anything opens the session, so what the user sees is compared with what the
server holds rather than with another Odoo read.

WHAT BREAKS EACH JOURNEY, PRE-FIX

A regression test is only worth its runtime if it fails against the code as it
was. These are written to (see the ticket's completion record for the run that
confirms it):

* **reopen / relogin** — a detail response carrying no usable transcript (a node
  that has not loaded the session's history yet, or a console answering HTTP 200
  with ``{"error": …}``) used to be read as "the conversation is now empty", so
  every mirrored row was unlinked and the user came back to a blank chat with no
  error anywhere (OTI-5729 shapes 2 and 3, fixed by OTI-5744).
* **refresh** — a conversation longer than ``message_limit`` was replaced by the
  page the server serves, which is a **tail**: the earliest turns rolled off
  silently on every poll and nothing said so (OTI-5763 measured it; OTI-5770 and
  OTI-5772 are the fix).
* **deleted remotely** — a 404 raised out of the sync, so the chat showed a raw
  error on every poll and the Odoo row never said why it was empty; it is now
  recorded (``remote_gone``, ``sync_note``, one audit row) and the transcript is
  cleared *with a reason* (OTI-5744).
* **two sessions of one agent** and **a 500 on detail** were already right, and
  are here as guards: a server error is not evidence about a conversation, and
  one session's sync must never touch another's.

The fixture's own safety is asserted here too: every case is pinned to the mock
server's host and port, so a test cannot read a real console's sessions or steer
a real agent.

NOT COVERED HERE, AND WHY

* The **browser**. No Chrome in the test image, so the OWL client's localStorage
  restore (OTI-5743) is the frontend's own concern. What is covered here is the
  server half of the same claim: the same session id, reopened, returns the same
  transcript.
* The sidebar's treatment of a **closed** session (OTI-5742). These cases keep
  their sessions open on purpose — they test the transcript, not the filter.
* The mock's switchable failure modes (OTI-5740). Until they exist, the two
  remote failures below are induced at ``urllib.request.urlopen``, which is the
  same seam ``TurnstoneClient`` reads a 500 and a 200-error-body off. When
  OTI-5740 lands, ``_fault`` / ``_detail_body`` become one-line switches and
  these cases keep their assertions unchanged.
"""

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from odoo.tests import HttpCase, new_test_user, tagged

from .common import MOCK_BASE_URL, MOCK_TOKEN, MockTurnstone, _mock_only_urlopen

#: How long a planted conversation is when the point of the case is that a long
#: one survives. Comfortably past ``SESSION_DETAIL_MESSAGE_LIMIT`` (200), because
#: that is the only length at which the fetch can truncate at all.
LONG = 205

#: Distinct logins from `common.py`'s fixture, so the two suites can share a
#: database without a unique-constraint surprise if their transactions ever
#: overlap.
PERSIST_USER = "persist-user"
PERSIST_OTHER = "persist-user-two"


def _transcript(start, count):
    """`count` distinct turns beginning at `start`, alternating speaker.

    Distinct text per row on purpose: every guard in the sync path compares the
    stored rows with the page it just received, so a fixture where each assistant
    turn read "ok" could not tell a shifted window from an unchanged one.
    """
    return [
        {"role": "user" if n % 2 == 0 else "assistant", "content": "turn %d" % n}
        for n in range(start, start + count)
    ]


def _bodies(payload):
    """The transcript of a `session/get` payload, as the chat renders it."""
    return [message["body"] for message in payload["session"]["messages"]]


class MockTurnstoneHttpCase(HttpCase):
    """The `common.py` fixture, wired for the routes a user actually calls.

    ``common.TurnstoneTransactionCase`` is a ``TransactionCase``: it can drive the
    models but not the JSON-RPC routes, and the journeys this file covers are
    route-shaped — a page load is ``bootstrap``, an open is ``session/get``, a
    relogin is a new session cookie. ``HttpCase`` gives exactly those, in-process
    and with no browser: ``make_jsonrpc_request`` posts to the real dispatcher, so
    the ``auth="user"`` check and the record rules run as they do for the OWL
    client.

    The rest is the same three guarantees ``common.py`` makes, restated rather
    than refactored out of it: two fixtures that share a base class are coupled,
    and the 45 tests already passing against that one should not change behaviour
    because a route-level suite arrived.

    * **only the mock's host:port may be reached.** The pin is a module attribute,
      so it also covers the addon's client running inside a request thread;
    * the fixture refuses a server that does not identify as the mock, rather than
      testing whatever is listening on that port;
    * the mock is reset before every test, so no case inherits another's
      workstreams.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Before anything makes a request, so a mistake is caught by the fixture
        # itself rather than by a real server answering.
        cls.startClassPatcher(patch("urllib.request.urlopen", _mock_only_urlopen))

        cls.mock = MockTurnstone()
        health = cls.mock.health()
        if not health.get("mock"):
            raise AssertionError(
                "%s answered, but it does not identify as the mock Turnstone "
                "server (no `mock` marker). Refusing to run tests against it."
                % MOCK_BASE_URL
            )

        cls.user = new_test_user(
            cls.env,
            login=PERSIST_USER,
            name="Turnstone User",
            groups="base.group_user,turnstone_integration.group_turnstone_user",
        )
        #: A second Turnstone User, so "another user cannot see it" is a real
        #: member of the same group rather than an outsider refused earlier.
        cls.other_user = new_test_user(
            cls.env,
            login=PERSIST_OTHER,
            name="Second Turnstone User",
            groups="base.group_user,turnstone_integration.group_turnstone_user",
        )
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Mock Turnstone",
            "base_url": MOCK_BASE_URL,
            "api_token": MOCK_TOKEN,
            "is_default": True,
        })
        cls.agent = cls.env["turnstone.agent"].create({
            "name": "Mock Agent",
            "connection_id": cls.connection.id,
            "kind": "interactive",
        })

    def setUp(self):
        super().setUp()
        self.mock.reset()

    # -- acting as a user ---------------------------------------------------

    def login(self, user=None):
        """Sign in and leave an HTTP session cookie in the opener.

        The real mechanism, not ``with_user``: ``bootstrap`` and every
        ``session/*`` route is ``auth="user"``, so the request has to carry a
        session — that is what "logged in" means here, and what a relogin has to
        rebuild.
        """
        user = user or self.user
        self.authenticate(user.login, user.login)

    def rpc(self, route, **params):
        """One JSON-RPC call, exactly as the OWL client makes it.

        Returns the route's ``result``, which for this module is either a payload
        or ``{"error": "..."}``: the controller turns every ``UserError`` and
        ``AccessError`` into the latter rather than failing the request, so a
        case reads ``payload.get("error")`` instead of expecting an exception.
        """
        result = self.make_jsonrpc_request(route, params)
        # The request ran on its own cursor in another thread; drop what this
        # environment cached so an assertion cannot read a stale row.
        self.env.invalidate_all()
        return result

    def bootstrap(self, current_session_id=None):
        return self.rpc("/turnstone/bootstrap", current_session_id=current_session_id)

    def open_session(self, session_id):
        """Open a session in the chat: `/turnstone/session/get` with sync on."""
        return self.rpc("/turnstone/session/get", session_id=session_id, sync=True)

    @staticmethod
    def listed_ids(payload):
        return [row["id"] for row in payload.get("sessions") or []]

    # -- arranging state ----------------------------------------------------

    def make_chat(self, ws_id=None, user=None, **vals):
        """An Odoo session owned by `user`, pointing at `ws_id` if given.

        ``ws_id`` is set at creation because these cases are about opening a
        conversation that **already exists on the server** — the only way to
        plant a transcript before anything opens the session, which is the rule
        this ticket set.
        """
        user = user or self.user
        values = {
            "name": "Fixture session",
            "connection_id": self.connection.id,
            "agent_id": self.agent.id,
        }
        if ws_id:
            values["ws_id"] = ws_id
        values.update(vals)
        return self.env["turnstone.session"].with_user(user).create(values)

    def seed_transcript(self, ws_id, messages, **fields):
        """Plant a workstream with a transcript, before anything opens it."""
        return self.mock.seed_transcript(ws_id, messages, **fields)

    # -- arranging a remote failure ----------------------------------------
    #
    # Both helpers wrap the fixture's pinned opener rather than replacing it, so
    # every call the case does not deliberately break is still the mock-only one
    # and a request to any other host still fails loudly.

    @contextmanager
    def _detail_body(self, rewrite):
        """Answer every call normally, but rewrite a detail response's body.

        Used for the shapes a *live* console produces with a 200 status: a node
        that has not loaded the history yet (``messages: []``), a console
        answering an error body instead of a session, and the bounded tail the
        real endpoint always serves. Going through the mock first keeps the
        `persisted` block honest — only the field under test is changed, and
        nothing else about the response is invented.

        `rewrite` receives the decoded payload and the request's parsed URL, so a
        helper can honour a query parameter (`message_limit`) the way the real
        endpoint does.
        """
        honest = urllib.request.urlopen

        def opener(request, *args, **kwargs):
            target = request.full_url if isinstance(request, Request) else str(request)
            url = urlsplit(target)
            response = honest(request, *args, **kwargs)
            if not url.path.endswith("/detail"):
                # Everything the case did not ask to break stays the real mock
                # response, headers and all.
                return response
            with response:
                payload = rewrite(json.loads(response.read().decode() or "{}"), url)
            return BytesIO(json.dumps(payload).encode())

        with patch("urllib.request.urlopen", opener):
            yield

    def withdraw_transcript(self):
        """The workstream is there, but the node answers with no history.

        OTI-5729 shape 2 — observed on a real 1.8.3 as well: a normal
        ``persisted`` block and ``messages: []`` at HTTP 200, one query parameter
        away from the default. The mirror used to be unlinked on that answer.
        """
        return self._detail_body(
            lambda payload, url: dict(payload, messages=[]))

    #: The real endpoint's default and hard clamp for `message_limit`.
    DETAIL_DEFAULT_LIMIT = 20
    DETAIL_MAX_LIMIT = 200

    def clamp_detail(self):
        """Answer with only the rows the real endpoint would serve.

        ``detail`` serves a **tail** of at most ``message_limit`` rows — default
        **20**, clamped **200** — and says nothing about having truncated: no
        total, no ``has_more``, no offset. The mock serves its whole transcript
        instead, so that truncation is applied here, at the seam, to the mock's own
        answer.

        Honouring the parameter is the point, not a detail: a request that sends
        no ``message_limit`` must get 20 rows (which is what made every mirror
        born truncated, OTI-5763), and one that sends the clamp must get 200.
        Emulating only the second would let a client that forgot the parameter
        pass — the precise regression OTI-5770 exists to prevent.
        """
        def rewrite(payload, url):
            values = parse_qs(url.query)
            try:
                asked = int(values["message_limit"][0])
            except (KeyError, IndexError, ValueError):
                asked = self.DETAIL_DEFAULT_LIMIT
            rows = payload.get("messages") or []
            return dict(payload, messages=rows[-min(asked, self.DETAIL_MAX_LIMIT):])

        return self._detail_body(rewrite)

    def break_detail(self, message="node not found"):
        """A 200 that carries an ``error`` and none of the three blocks.

        OTI-5729 shape 3 — the console's answer for a session it cannot serve.
        Read as data it looks exactly like an empty conversation.
        """
        return self._detail_body(lambda payload, url: {"error": message})

    @contextmanager
    def detail_raises(self, error):
        """Make the detail call raise `error`; everything else is untouched.

        ``TurnstoneClient._request`` reads a 500 off ``urllib.request.urlopen``,
        so raising here is the identical exception a real console produces for
        that status.
        """
        honest = urllib.request.urlopen

        def opener(request, *args, **kwargs):
            target = request.full_url if isinstance(request, Request) else str(request)
            if urlsplit(target).path.endswith("/detail"):
                raise error
            return honest(request, *args, **kwargs)

        with patch("urllib.request.urlopen", opener):
            yield

    @staticmethod
    def http_500(path="/v1/api/cluster/ws/ws-1/detail"):
        return urllib.error.HTTPError(
            path, 500, "Internal Server Error", {},
            BytesIO(b'{"error": "node ws-1 is unavailable"}'),
        )


@tagged("post_install", "-at_install")
class TestPersistenceFixture(MockTurnstoneHttpCase):
    """The fixture's own promises, checked where the routes are."""

    def test_the_suite_is_pinned_to_the_mock_server(self):
        """AC 2: no case in this file can reach a real Turnstone.

        Asserted rather than assumed, and asserted from the addon's own client
        as well — that is the code which would otherwise read somebody's live
        sessions, and it runs in the request thread, not this one.
        """
        with self.assertRaises(AssertionError) as caught:
            urllib.request.urlopen("https://turnstone.cloud.example/v1/api/auth/whoami")
        self.assertIn("Refusing a real network request", str(caught.exception))

        elsewhere = self.env["turnstone.connection"].create({
            "name": "Not the mock",
            "base_url": "https://turnstone.cloud.example",
            "api_token": "not-a-real-token",
        })
        with self.assertRaises(AssertionError) as caught:
            elsewhere._client().whoami()
        self.assertIn("Refusing a real network request", str(caught.exception))

    def test_a_logged_in_user_can_read_the_bootstrap(self):
        """The fixture produces a session the routes accept.

        Without this, every case below would fail as an authorization error that
        says nothing about persistence.
        """
        self.login()
        payload = self.bootstrap()
        self.assertNotIn("error", payload, payload.get("error"))
        self.assertEqual([a["id"] for a in payload["agents"]], [self.agent.id])
        self.assertIn("sessions", payload)


@tagged("post_install", "-at_install")
class TestChatHistorySurvivesReopen(MockTurnstoneHttpCase):
    """Leave the chat and come back: the conversation is still there."""

    WS_ID = "ws-reopen"

    def test_reopen_after_navigating_away_returns_the_same_transcript(self):
        """AC 1 (reopen): the same session id, reopened, is the same chat.

        Navigation here is a real one — a second session is opened in between, so
        the client is not merely re-rendering the conversation it already had.
        The assertions are on the server's answer to the second open, because
        that is what the chat renders; and on the detail call really happening,
        so a cache masquerading as a reopen cannot pass this.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 4))
        chat = self.make_chat(self.WS_ID, name="Reopened chat")
        self.login()

        first = self.open_session(chat.id)
        self.assertNotIn("error", first)
        self.assertEqual(_bodies(first), ["turn 0", "turn 1", "turn 2", "turn 3"])

        other = self.make_chat(name="Something else")
        self.open_session(other.id)
        again = self.open_session(chat.id)

        self.assertNotIn("error", again)
        self.assertEqual(_bodies(again), _bodies(first),
                         "reopening must return the same conversation")
        self.assertEqual(again["session"]["ws_id"], self.WS_ID)
        details = self.mock.request_log(
            path_prefix="/v1/api/cluster/ws/%s/detail" % self.WS_ID,
        )
        self.assertGreaterEqual(
            len(details), 2,
            "a reopen must re-read the server rather than serve what was already "
            "on screen — one detail call per open is the observable proof",
        )

    def test_a_node_that_has_not_loaded_the_history_does_not_empty_the_chat(self):
        """AC 1 (reopen, the shape that actually lost history).

        The user walks away. The node that hosted the conversation is restarted,
        or the workstream is re-attached elsewhere, and it answers the next detail
        read with ``messages: []`` — a normal ``persisted`` block and no history.
        Odoo used to read that as "the conversation is now empty" and unlink every
        mirrored row, so the reopen showed a blank chat with no error, and the
        history was gone from the only copy the user had (OTI-5729 shape 2).

        Now the mirror is kept and the *reason* is recorded, so an empty chat is
        never indistinguishable from a lost one.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 4))
        chat = self.make_chat(self.WS_ID, name="Cold node chat")
        self.login()
        self.assertEqual(len(_bodies(self.open_session(chat.id))), 4)

        with self.withdraw_transcript():
            reopened = self.open_session(chat.id)

        self.assertNotIn("error", reopened, "a cold node is not an error to show")
        self.assertEqual(
            _bodies(reopened),
            ["turn 0", "turn 1", "turn 2", "turn 3"],
            "the stored transcript must survive a response that carries none",
        )
        self.assertTrue(
            reopened["session"]["sync_note"],
            "an empty response must leave a note saying why the mirror was kept",
        )
        self.assertFalse(reopened["session"]["remote_gone"],
                         "the workstream still exists; only its history is not "
                         "loaded yet")
        self.assertEqual(
            len(self.env["turnstone.session"].browse(chat.id).message_ids), 4,
            "and the rows are still in the database, not only in the payload",
        )


@tagged("post_install", "-at_install")
class TestChatHistorySurvivesRefresh(MockTurnstoneHttpCase):
    """Reload the page on a long conversation: nothing rolls off the top."""

    WS_ID = "ws-refresh"

    def test_a_refresh_never_silently_drops_the_earliest_turns(self):
        """AC 1 (refresh): what the fetch limit used to cost, on every reload.

        ``GET /cluster/ws/{id}/detail`` answers with a **tail** — at most
        ``message_limit`` rows (200, the server's clamp) — and carries no total,
        no offset and no ``has_more``. A refresh re-reads it. Once a conversation
        is longer than that, every read is a window over its end: the oldest turns
        roll off, and before OTI-5772 nothing anywhere recorded it (OTI-5763
        measured 20 rows mirrored while the server held 26, the user's first
        message gone, ``sync_note`` empty).

        Two halves, because the two failures look different:

        * the **first** read of an over-long conversation cannot be told from a
          complete one — the rows are all that will ever be served — so it is
          applied *and disclosed*;
        * a later read is a window that has slid forward, and the turns it slid
          past are kept in front of it, so the mirror grows instead of rolling.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, LONG))
        chat = self.make_chat(self.WS_ID, name="Long conversation")
        self.login()

        with self.clamp_detail():
            first = self.open_session(chat.id)
            self.assertNotIn("error", first)
            self.assertEqual(len(_bodies(first)), 200,
                             "the server serves its maximum page, and that is what "
                             "is mirrored")
            self.assertTrue(
                first["session"]["sync_note"],
                "an over-long conversation must never be presented as complete",
            )
            self.assertIn("200", first["session"]["sync_note"],
                          "the note must name the limit that was hit")

            # The conversation moves on, so the window the server serves slides
            # forward by three turns.
            self.mock.seed(append=True, ws_id=self.WS_ID, messages=_transcript(LONG, 3))
            refreshed = self.open_session(chat.id)

        bodies = _bodies(refreshed)
        self.assertEqual(bodies[-1], "turn 207",
                         "the refresh must show the newest turn")
        self.assertEqual(
            bodies[0], "turn 5",
            "the oldest turn the mirror already held must still be first: a "
            "sliding window may not drop turns it had already stored",
        )
        self.assertEqual(len(bodies), 203, "the mirror grows; it does not roll")
        self.assertIn("3 earlier", refreshed["session"]["sync_note"],
                      "the note must say how many stored turns were kept in front "
                      "of the page")

    def test_a_second_refresh_of_an_unchanged_transcript_writes_nothing(self):
        """A reload that changes nothing must not rewrite the transcript.

        Cheap on its own, load-bearing in a chat: the client polls every couple of
        seconds, and a sync that recreates the mirror each time is the write
        contention that made one send become five (OTI-5729 §J). The row ids are
        the assertion — same ids means nothing was unlinked and recreated.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 6))
        chat = self.make_chat(self.WS_ID, name="Quiet conversation")
        self.login()

        self.open_session(chat.id)
        Message = self.env["turnstone.message"]
        before_ids = Message.search(
            [("session_id", "=", chat.id)], order="sequence, id").ids
        self.assertTrue(before_ids)

        refreshed = self.open_session(chat.id)

        after = Message.search([("session_id", "=", chat.id)], order="sequence, id")
        self.assertEqual(after.ids, before_ids,
                         "an unchanged transcript must keep its rows")
        self.assertEqual([m.body for m in after], _bodies(refreshed))
        self.assertFalse(refreshed["session"]["sync_note"],
                         "a short, unchanged conversation needs no note")


@tagged("post_install", "-at_install")
class TestChatHistorySurvivesRelogin(MockTurnstoneHttpCase):
    """Log out and back in as the same person: the chat is where it was."""

    WS_ID = "ws-relogin"

    def test_relogin_as_the_same_user_restores_the_session_and_transcript(self):
        """AC 1 (relogin): a new HTTP session, the same conversation.

        The point of a relogin is that nothing the *browser* held can be trusted:
        the cookie is new, so everything the chat shows came back from the server.
        That is what makes this different from a reopen — if the transcript lived
        only in the old page's state, this case would find an empty chat.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 4))
        chat = self.make_chat(self.WS_ID, name="Relogin chat")
        self.login()
        first = self.open_session(chat.id)
        self.assertEqual(len(_bodies(first)), 4)

        old_session_id = self.session.sid
        self.logout()
        self.login()
        self.assertNotEqual(self.session.sid, old_session_id,
                            "a relogin must really be a new HTTP session")

        payload = self.bootstrap()
        self.assertIn(chat.id, self.listed_ids(payload),
                      "the session must be findable straight after a relogin")
        again = self.open_session(chat.id)

        self.assertNotIn("error", again)
        self.assertEqual(_bodies(again), _bodies(first))
        self.assertEqual(again["session"]["name"], "Relogin chat")
        self.assertEqual(again["session"]["agent_id"], self.agent.id,
                         "and it is still the configured agent's chat")

    def test_a_console_error_body_does_not_empty_the_chat(self):
        """AC 1 (relogin): the other way a returning user lost their history.

        A console that answers **HTTP 200** with ``{"error": …}`` — a node that
        cannot serve the workstream — used to be parsed as an empty conversation:
        state forced to ``idle``, counters zeroed, every mirrored row unlinked, and
        the user saw "Ready · 0 tool calls" over a blank transcript (OTI-5729
        shape 3). A broken body is not evidence about a conversation, so it is
        reported instead and the transcript stays exactly as it was.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 4))
        chat = self.make_chat(self.WS_ID, name="Broken body chat")
        self.login()
        self.assertEqual(len(_bodies(self.open_session(chat.id))), 4)

        self.logout()
        self.login()

        with self.break_detail("node for ws-relogin not found"):
            broken = self.open_session(chat.id)

        self.assertTrue(broken.get("error"),
                        "a broken body must be reported, not rendered as a chat")
        self.assertIn("not found", broken["error"])
        record = self.env["turnstone.session"].browse(chat.id)
        self.assertEqual([m.body for m in record.message_ids],
                         ["turn 0", "turn 1", "turn 2", "turn 3"],
                         "the stored transcript must be untouched by a response "
                         "that describes nothing")
        self.assertEqual(record.state, "idle",
                         "and its header must not be rewritten to a guess")

    def test_another_user_neither_lists_nor_opens_that_session(self):
        """AC 1 (relogin, the other half): no cross-user restore.

        Two Turnstone Users, each with their own chat. The record rules decide
        visibility, so a relogin as someone else must find neither the row nor its
        transcript — including through ``session/get`` with the id in hand, which
        is exactly the request a stale localStorage pointer would make.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 4))
        mine = self.make_chat(self.WS_ID, name="Mine")

        self.login(self.other_user)
        payload = self.bootstrap()
        self.assertNotIn("error", payload)
        self.assertNotIn(mine.id, self.listed_ids(payload),
                         "another user's session must not be in the sidebar")

        refused = self.open_session(mine.id)
        self.assertTrue(refused.get("error"),
                        "opening a session by id must not bypass the record rules")
        self.assertNotIn("session", refused,
                         "the refusal must carry no payload at all — a transcript "
                         "in the error branch would be the leak this guards")
        # The wording is the record rules' own (an ACL error) or `_session`'s
        # "no longer exists", depending on whether `exists()` filtered the row
        # first. Either is a refusal; asserting on one exact sentence would pin
        # a translation and break on the other path.
        self.assertTrue(
            "access" in refused["error"] or "no longer exists" in refused["error"],
            "expected a refusal, got: %s" % refused["error"],
        )
        self.assertNotIn("turn 0", str(refused),
                         "no turn of the other user's conversation may appear in "
                         "the response")


@tagged("post_install", "-at_install")
class TestTwoSessionsOfOneAgent(MockTurnstoneHttpCase):
    """Starting the same agent twice: each chat is its own conversation."""

    def test_each_session_opens_its_own_transcript(self):
        """AC 1: two rows, one agent, two transcripts — and no cross-wiring.

        This is the scenario behind "I started the agent again and my old chat is
        gone": every session of a configured agent carries the same *name*, so the
        only thing telling them apart is the id — and the only thing telling their
        conversations apart is that an open is keyed by session, never by agent.
        Both are created through ``/turnstone/session/new_from_agent``, the path a
        user actually takes.
        """
        self.login()
        first = self.rpc("/turnstone/session/new_from_agent", agent_id=self.agent.id)
        second = self.rpc("/turnstone/session/new_from_agent", agent_id=self.agent.id)
        self.assertNotIn("error", first)
        self.assertNotIn("error", second)
        a, b = first["session"], second["session"]
        self.assertNotEqual(a["id"], b["id"], "two sessions, two ids")
        self.assertEqual(a["name"], b["name"],
                         "and the same name — which is why the id is the key")

        # Each is used, so each gets its own workstream on the mock.
        self.rpc("/turnstone/session/send", session_id=a["id"], body="question A")
        self.rpc("/turnstone/session/send", session_id=b["id"], body="question B")
        a_ws = self.env["turnstone.session"].browse(a["id"]).ws_id
        b_ws = self.env["turnstone.session"].browse(b["id"]).ws_id
        self.assertTrue(a_ws and b_ws and a_ws != b_ws,
                        "each session must own a distinct workstream")

        # Give each workstream a marker turn, so a crossed transcript is obvious.
        self.mock.seed(append=True, ws_id=a_ws,
                       messages=[{"role": "assistant", "content": "MARKER A"}])
        self.mock.seed(append=True, ws_id=b_ws,
                       messages=[{"role": "assistant", "content": "MARKER B"}])

        opened_a = self.open_session(a["id"])
        opened_b = self.open_session(b["id"])

        self.assertIn("question A", _bodies(opened_a))
        self.assertIn("MARKER A", _bodies(opened_a))
        self.assertNotIn("question B", _bodies(opened_a))
        self.assertNotIn("MARKER B", _bodies(opened_a))

        self.assertIn("question B", _bodies(opened_b))
        self.assertIn("MARKER B", _bodies(opened_b))
        self.assertNotIn("question A", _bodies(opened_b))
        self.assertNotIn("MARKER A", _bodies(opened_b))

    def test_syncing_one_session_leaves_the_other_alone(self):
        """A sync is scoped to its session, rows and notes alike.

        The failure this guards is the one a "current session" shortcut would
        cause: one chat's poll writing another chat's transcript, or a note about
        one conversation appearing on both. One session is long enough for the
        fetch limit to apply, and only that one is ever opened.
        """
        self.seed_transcript("ws-two-a", _transcript(0, LONG))
        self.seed_transcript("ws-two-b", _transcript(1000, 4))
        a = self.make_chat("ws-two-a", name="Conversation A")
        b = self.make_chat("ws-two-b", name="Conversation B")
        self.login()

        opened_a = self.open_session(a.id)
        self.assertTrue(opened_a["session"]["sync_note"],
                        "A is over the limit, so A must carry the note")

        untouched = self.env["turnstone.session"].browse(b.id)
        self.assertEqual(len(untouched.message_ids), 0,
                         "opening A must not write B's transcript")
        self.assertFalse(untouched.sync_note)

        opened_b = self.open_session(b.id)
        self.assertEqual(_bodies(opened_b),
                         ["turn 1000", "turn 1001", "turn 1002", "turn 1003"])
        self.assertFalse(opened_b["session"]["sync_note"],
                         "B is short; A's note must not follow it")
        self.assertEqual(
            len(self.env["turnstone.session"].browse(a.id).message_ids), 200,
            "and A's mirror is still A's — untouched by B's sync",
        )


@tagged("post_install", "-at_install")
class TestSessionDeletedRemotely(MockTurnstoneHttpCase):
    """The workstream is gone from the server: say so, once, and keep the row."""

    WS_ID = "ws-deleted"

    def _forget(self):
        """Make the mock forget every workstream — the "deleted remotely" shape.

        A real console answers 404 for a workstream deleted or reaped, which is
        what the mock answers for an id it does not hold. Its reset is the control
        plane's way to forget one: there is no per-id delete endpoint, and reset
        leaves the connection and the Odoo row untouched, which is exactly the
        asymmetry under test.
        """
        self.mock.reset()

    def test_a_404_is_recorded_and_clears_the_transcript_with_a_reason(self):
        """AC 1 (deleted remotely): the one answer that may clear a transcript.

        404 is a statement about *this* session — it does not exist — so it is the
        only remote answer allowed to destroy local state, and OTI-5744 made it
        record that it did. Before that, the 404 raised out of the sync: the chat
        showed "Turnstone has no such resource" on every poll, the Odoo row never
        said why its chat was empty, and nothing distinguished a deleted
        workstream from a broken one.

        So: the route answers with a session rather than an error, the session says
        ``remote_gone``, the note names the workstream, the transcript is cleared,
        and the row is still there to be read.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 4))
        chat = self.make_chat(self.WS_ID, name="Deleted chat")
        self.login()
        self.assertEqual(len(_bodies(self.open_session(chat.id))), 4)

        self._forget()
        after = self.open_session(chat.id)

        self.assertNotIn("error", after,
                         "a deleted workstream is news about the session, not a "
                         "failure of the request")
        self.assertTrue(after["session"]["remote_gone"])
        self.assertEqual(after["session"]["messages"], [],
                         "the transcript cannot be read any more, so it is cleared "
                         "— and the flag above says why")
        self.assertIn(self.WS_ID, after["session"]["sync_note"])
        self.assertEqual(after["session"]["state"], "error")

        record = self.env["turnstone.session"].browse(chat.id)
        self.assertTrue(record.exists(),
                        "the Odoo row must survive its workstream")
        self.assertEqual(len(record.message_ids), 0)

    def test_the_deletion_is_recorded_once_however_often_the_chat_polls(self):
        """AC 1 (deleted remotely): one audit row, not one per poll.

        The chat refreshes every couple of seconds. An audit row per poll would
        bury the log that exists to answer "what happened to this session".
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 2))
        chat = self.make_chat(self.WS_ID, name="Deleted chat")
        self.login()
        self.open_session(chat.id)

        self._forget()
        for _poll in range(3):
            self.open_session(chat.id)

        rows = self.env["turnstone.audit.log"].search([
            ("session_id", "=", chat.id),
            ("action", "=", "sync_remote_gone"),
        ])
        self.assertEqual(len(rows), 1,
                         "the deletion is recorded once, not once per poll")
        self.assertIn(self.WS_ID, rows.detail or "")

    def test_a_session_gone_remotely_stays_findable(self):
        """AC 1 (deleted remotely): it is still in the sidebar, marked.

        Dropping the row would turn "the server lost this" into "Odoo lost this",
        and the Odoo row is where the reason lives. It stays, with its state and
        its note, which is what the client needs to show it greyed out rather than
        missing.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 2))
        chat = self.make_chat(self.WS_ID, name="Deleted chat")
        self.login()
        self.open_session(chat.id)
        self._forget()
        self.open_session(chat.id)

        payload = self.bootstrap(current_session_id=chat.id)
        self.assertIn(chat.id, self.listed_ids(payload))
        row = next(r for r in payload["sessions"] if r["id"] == chat.id)
        self.assertEqual(row["state"], "error")


@tagged("post_install", "-at_install")
class TestDetailServerError(MockTurnstoneHttpCase):
    """A 500 from the server is not evidence about the conversation."""

    WS_ID = "ws-1"

    def test_a_500_on_detail_keeps_the_local_history(self):
        """AC 1 (500): the transcript survives a server error, untouched.

        ``TurnstoneClient`` raises before anything is written, which is what makes
        the mirror safe here — and it is worth pinning precisely because the fixes
        above rewrote the sync path: a "clear on any failed read" shortcut would
        look reasonable and would turn every 500 into data loss. The header is
        asserted too: a failed read must not rewrite state or last-activity for a
        session it did not describe.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 4))
        chat = self.make_chat(self.WS_ID, name="Flaky server chat")
        self.login()
        self.open_session(chat.id)
        before = self.env["turnstone.session"].browse(chat.id)
        before_activity = before.last_activity
        before_ids = before.message_ids.ids
        self.assertTrue(before_ids)

        with self.detail_raises(self.http_500()):
            failed = self.open_session(chat.id)

        self.assertTrue(failed.get("error"), "a 500 must be reported to the chat")
        self.assertIn("500", failed["error"])
        record = self.env["turnstone.session"].browse(chat.id)
        self.assertEqual(record.message_ids.ids, before_ids,
                         "a failed read must not touch the transcript's rows")
        self.assertEqual([m.body for m in record.message_ids],
                         ["turn 0", "turn 1", "turn 2", "turn 3"])
        self.assertEqual(record.state, "idle")
        self.assertEqual(record.last_activity, before_activity,
                         "nothing was applied, so nothing may claim the session "
                         "was active")
        self.assertFalse(record.remote_gone, "a 500 is not a 404")

    def test_the_chat_recovers_when_the_server_answers_again(self):
        """AC 1 (500): and the next good read is applied normally.

        A failure must not leave a frozen mirror: once the server answers, the
        transcript it serves is what the user sees and the error is gone.
        """
        self.seed_transcript(self.WS_ID, _transcript(0, 4))
        chat = self.make_chat(self.WS_ID, name="Flaky server chat")
        self.login()
        self.open_session(chat.id)

        with self.detail_raises(self.http_500()):
            self.assertTrue(self.open_session(chat.id).get("error"))

        self.mock.seed(append=True, ws_id=self.WS_ID,
                       messages=[{"role": "assistant", "content": "turn 4"}])
        recovered = self.open_session(chat.id)

        self.assertNotIn("error", recovered)
        self.assertEqual(_bodies(recovered),
                         ["turn 0", "turn 1", "turn 2", "turn 3", "turn 4"])
        self.assertFalse(recovered["session"]["remote_gone"])
        self.assertFalse(recovered["session"]["sync_note"])

@tagged("post_install", "-at_install")
class TestSidebarListing(MockTurnstoneHttpCase):
    """The sidebar itself: what a row carries and which rows come back.

    The journeys above prove a transcript survives each way of returning to a
    chat. This class covers the *listing* half of the same complaint (OTI-5742):
    a closed chat must still be reachable, and a row must carry enough to tell
    two sessions of one agent apart. It also pins the access rule, because the
    listing is the one place a row id arrives from the client as a hint.
    """

    def _row(self, payload, session_id):
        rows = [r for r in payload.get("sessions") or [] if r["id"] == session_id]
        self.assertEqual(len(rows), 1,
                         "session %s should be listed exactly once" % session_id)
        return rows[0]

    def test_a_closed_session_stays_in_the_sidebar(self):
        """AC 2: closing a chat removes it from the live tier, not the list.

        ``action_close`` only closes the remote workstream and sets ``state``;
        the transcript and the row stay. Dropping it from the listing was the
        whole reason a closed chat looked deleted, so the row must come back
        with its state and its count intact.
        """
        self.seed_transcript("ws-closed", _transcript(0, 3))
        chat = self.make_chat("ws-closed", name="Closed chat")
        self.login()
        self.open_session(chat.id)

        closed = self.rpc("/turnstone/session/close", session_id=chat.id)
        self.assertNotIn("error", closed)
        row = self._row(closed, chat.id)
        self.assertEqual(row["state"], "closed")
        self.assertEqual(row["message_count"], 3,
                         "a closed chat's row still reports its history")

        # And it is still there on the next bootstrap, not only in the answer
        # to the close call itself.
        payload = self.bootstrap()
        self.assertEqual(self._row(payload, chat.id)["state"], "closed")

    def test_a_row_carries_the_agent_and_the_message_count(self):
        """AC 2: the two fields the client needs to tell same-named rows apart.

        ``agent_id`` is what ``fallbackSession`` matches on; without it the
        fallback degrades to "newest session at all" and reopens the wrong
        chat. ``message_count`` separates a never-started draft from a real
        conversation that happens to share the agent's name.
        """
        self.seed_transcript("ws-counted", _transcript(0, 4))
        used = self.make_chat("ws-counted", name="Used chat")
        draft = self.make_chat(name="Draft chat")
        self.login()
        self.open_session(used.id)

        payload = self.bootstrap()
        used_row = self._row(payload, used.id)
        draft_row = self._row(payload, draft.id)
        self.assertEqual(used_row["agent_id"], self.agent.id)
        self.assertEqual(used_row["agent_name"], self.agent.name)
        self.assertEqual(used_row["message_count"], 4)
        self.assertEqual(draft_row["agent_id"], self.agent.id)
        self.assertEqual(draft_row["message_count"], 0,
                         "a draft with no transcript must report zero, not be "
                         "confused with a used chat")
        self.assertIsNone(draft_row["ws_id"],
                          "a draft has no workstream id until its first message")

    def test_rows_are_newest_first(self):
        """The client's fallback and the sidebar both assume newest-first."""
        older = self.make_chat(name="Older")
        newer = self.make_chat(name="Newer")
        self.login()
        listed = self.listed_ids(self.bootstrap())
        self.assertLess(listed.index(newer.id), listed.index(older.id),
                        "the newest session must come first")

    def test_a_malformed_id_hint_does_not_cost_the_sidebar(self):
        """A bad ``current_session_id`` must not raise: the whole list is at stake.

        Before OTI-5742 ``int(include_id)`` on junk raised, and the client got
        an Odoo Server Error instead of a sidebar. The hint is a hint.
        """
        chat = self.make_chat(name="Still listed")
        self.login()
        for junk in ("not-an-id", None, [chat.id, "junk"], {"a": 1}):
            payload = self.bootstrap(current_session_id=junk)
            self.assertNotIn("error", payload,
                             "hint %r must not break the listing" % (junk,))
            self.assertIn(chat.id, self.listed_ids(payload))

    def test_another_users_session_cannot_be_pulled_in_by_id(self):
        """AC 2: the listing never returns another user's session.

        ``include_id`` names a row by id, which is the obvious way to widen
        visibility. It goes through ``search`` under the caller's record rules,
        so naming someone else's session returns nothing extra — the same
        guarantee the transcript cases pin, checked here on the listing path.
        """
        theirs = self.make_chat(name="Theirs", user=self.other_user)
        mine = self.make_chat(name="Mine")
        self.login()
        payload = self.bootstrap(current_session_id=theirs.id)
        self.assertNotIn("error", payload)
        listed = self.listed_ids(payload)
        self.assertIn(mine.id, listed)
        self.assertNotIn(theirs.id, listed,
                         "another user's session must not be in the sidebar")

    def test_a_closed_session_is_bounded_and_does_not_evict_live_ones(self):
        """AC 2: closed history is appended, bounded, and cannot evict live work.

        A single window over everything would let a long history of closed
        chats push the sessions in progress out of the sidebar, which is the
        original complaint arriving by another route. The closed tier is capped
        and appended after the open tier.
        """
        live = [self.make_chat(name="Live %d" % i) for i in range(3)]
        closed = [self.make_chat(name="Closed %d" % i) for i in range(25)]
        self.env["turnstone.session"].browse([c.id for c in closed]).write(
            {"state": "closed"})
        self.login()

        payload = self.bootstrap()
        listed = self.listed_ids(payload)
        for row in live:
            self.assertIn(row.id, listed,
                          "a live session must never be evicted by closed history")
        self.assertLessEqual(len([i for i in listed if i in [c.id for c in closed]]),
                             20, "the closed tier is bounded")
        self.assertTrue(set(listed) & {c.id for c in closed},
                        "closed sessions are still listed")
