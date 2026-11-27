"""The fixtures themselves, tested.

This module exists because the fixtures are the thing every other test in this
addon stands on: if the base case silently builds an unusable connection, or
the network pin does not hold, every later test is either lying or flaky. So
the guarantees `common.py` claims are asserted here rather than trusted:

* Odoo's runner really does discover this package (asserted through the
  runner's own loader, from inside a test the runner is running);
* the base case produces an admin user, an ordinary user, an outsider, a
  connection and an agent that the models actually accept;
* the connection reaches the **mock**, not a real console;
* a request to any other host fails immediately, in the fixture, before a
  socket is opened — for the addon's own client as much as for raw `urlopen`;
* the mock is empty at the start of every test, and the seeding helper plants a
  session and a transcript the addon can read back.

Tagged `post_install` with `-at_install`: `TransactionCase` carries
`{'standard', 'at_install'}` by default and `@tagged("post_install")` *adds* to
the set rather than replacing it, which runs every test twice and double-counts
the report (see `testing/odoo-test-runner-behaviour.md`).
"""

import urllib.parse
import urllib.request

from odoo.exceptions import AccessError, UserError
from odoo.tests import loader, tagged

from ..models.turnstone_client import TurnstoneError
from .common import (
    MOCK_BASE_URL,
    MOCK_HOST,
    MOCK_PORT,
    MOCK_TOKEN,
    MockTurnstone,
    TurnstoneTransactionCase,
    is_mock_url,
)

TEST_MODULE = "odoo.addons.turnstone_integration.tests.test_common"


@tagged("standard", "-at_install", "post_install")
class TestFixture(TurnstoneTransactionCase):
    """Every guarantee the fixture makes, checked."""

    # -- discovery ---------------------------------------------------------

    def test_package_is_discovered_by_the_odoo_runner(self):
        """The runner's own loader finds this module and its cases.

        Asserting "the test ran" would be circular — a test cannot run if it
        was not discovered. This asks `odoo.tests.loader` the same questions
        Odoo asks when building the suite, so a `tests/__init__.py` that forgets
        to import a `test_*` module fails here instead of silently dropping
        a whole file's worth of tests.
        """
        found = loader.get_test_modules("turnstone_integration")
        self.assertTrue(found, "the loader found no test module for the addon")
        names = [mod.__name__ for mod in found]
        self.assertIn(TEST_MODULE, names)

        case_ids = [
            case.id()
            for mod in found
            for case in loader.get_module_test_cases(mod)
        ]
        self.assertIn(
            "%s.%s" % (TEST_MODULE, "TestFixture.test_fixture_builds_users_connection_and_agent"),
            case_ids,
        )

    def test_tagged_to_run_once(self):
        """post_install, and NOT at_install — otherwise the suite runs twice."""
        self.assertIn("standard", self.test_tags)
        self.assertIn("post_install", self.test_tags)
        self.assertNotIn("at_install", self.test_tags)

    # -- the records the fixture builds ------------------------------------

    def test_fixture_builds_users_connection_and_agent(self):
        """Admin, ordinary user, outsider, connection and agent all exist and
        carry the groups the addon's guards check."""
        admin, user, outsider = self.admin_user, self.user, self.outsider
        self.assertTrue(
            admin.has_group("turnstone_integration.group_turnstone_manager")
        )
        self.assertTrue(
            admin.has_group("turnstone_integration.group_turnstone_user"),
            "a manager must imply Turnstone User, or managers cannot chat",
        )
        self.assertTrue(user.has_group("turnstone_integration.group_turnstone_user"))
        self.assertFalse(user.has_group("turnstone_integration.group_turnstone_manager"))
        self.assertFalse(outsider.has_group("turnstone_integration.group_turnstone_user"))

        self.assertEqual(self.connection.base_url, MOCK_BASE_URL)
        self.assertTrue(self.connection.is_default)
        self.assertEqual(self.agent.connection_id, self.connection)
        self.assertEqual(self.agent.kind, "interactive")

    def test_connection_token_readable_only_by_a_manager(self):
        """The fixture's token is real, and the field's group restriction holds.

        Worth asserting because `_client()` relies on it: it sudo()s precisely
        because an ordinary user cannot read the token.
        """
        self.assertEqual(self.connection.api_token, MOCK_TOKEN)
        with self.assertRaises(AccessError):
            self.connection.with_user(self.user).read(["api_token"])
        # A manager can, which is what makes action_test_connection possible.
        self.assertEqual(
            self.connection.with_user(self.admin_user).read(["api_token"])[0]["api_token"],
            MOCK_TOKEN,
        )

    def test_agent_is_usable_by_a_turnstone_user(self):
        """`_check_user_allowed` passes for the ordinary user, not the outsider.

        This is the gate every session-start path goes through, so if the
        fixture's records do not satisfy it, every test that starts a session
        would fail with an authorization error that has nothing to do with
        what it is testing.
        """
        self.connection.with_user(self.user)._check_user_allowed()
        self.agent.with_user(self.user)._check_user_allowed()
        with self.assertRaises(UserError):
            self.connection.with_user(self.outsider)._check_user_allowed()
        with self.assertRaises(UserError):
            self.agent.with_user(self.outsider)._check_user_allowed()

    def test_session_can_be_created_by_its_owner(self):
        """`make_session` produces a row the ordinary user owns and may send on."""
        session = self.make_session()
        self.assertEqual(session.user_id, self.user)
        self.assertEqual(session.state, "draft")
        session.action_open_chat()  # returns a client action, no network

    # -- the network pin ---------------------------------------------------

    def test_connection_reaches_the_mock(self):
        """The client built from the fixture really talks to the mock server."""
        who = self.connection._client().whoami()
        self.assertEqual(who.get("username"), "mock-user")
        self.assertEqual(who.get("node_id"), "node-mock-1")
        self.assertIn("permissions", who)

    def test_urlopen_to_another_host_is_refused(self):
        """A raw request off the mock's host fails before any socket is opened."""
        with self.assertRaises(AssertionError) as caught:
            urllib.request.urlopen("http://turnstone.cloud.example/api")
        self.assertIn("Refusing a real network request", str(caught.exception))

        # A different port on the right host is a different server.
        for url in (
            "http://%s:8069/" % MOCK_HOST,
            "https://%s/" % MOCK_HOST,
            "http://127.0.0.1:8099/",
        ):
            self.assertFalse(is_mock_url(url), "%s must not count as the mock" % url)

    def test_the_addons_own_client_is_pinned_too(self):
        """A connection pointed at a real console cannot make its call.

        The addon's client is what matters: it is the code that would otherwise
        read somebody's live sessions and could steer a real agent. Two entry
        points, two shapes of the same guarantee:

        * `whoami()` goes through `_request`, which only catches urllib's own
          errors — so the fixture's `AssertionError` propagates and the test
          fails loudly, which is what a broken test should do;
        * `health()` catches every exception on purpose ("health is
          best-effort") and wraps it, so the refusal shows up in the message
          instead. Asserting on the message is what proves the guard fired
          rather than a real server answering.
        """
        elsewhere = self.make_connection(
            name="Not the mock", base_url="https://turnstone.cloud.example",
        )
        with self.assertRaises(AssertionError) as caught:
            elsewhere._client().whoami()
        self.assertIn("Refusing a real network request", str(caught.exception))

        with self.assertRaises(TurnstoneError) as wrapped:
            elsewhere._client().health()
        self.assertIn("Refusing a real network request", str(wrapped.exception))

    def test_mock_client_refuses_a_non_mock_base_url(self):
        """The seeding helper cannot be aimed at another server either."""
        with self.assertRaises(AssertionError):
            MockTurnstone(base_url="https://turnstone.cloud.example")
        self.assertTrue(is_mock_url(MOCK_BASE_URL))
        self.assertEqual((MOCK_HOST, MOCK_PORT), (urllib.parse.urlsplit(MOCK_BASE_URL).hostname,
                                                  urllib.parse.urlsplit(MOCK_BASE_URL).port))

    # -- the mock's control plane ------------------------------------------

    def test_mock_starts_empty_and_reset_empties_it(self):
        """No test inherits another's workstreams, and reset() really clears."""
        self.assertEqual(self.mock.workstreams(), [], "the mock was not reset before this test")
        self.mock.seed(kind="coordinator", ws_id="ws-fixture-reset")
        self.assertEqual(self.mock.workstream_ids(), ["ws-fixture-reset"])
        self.mock.reset()
        self.assertEqual(self.mock.workstreams(), [])

    def test_seed_plants_a_session_and_a_transcript(self):
        """The seeding helper produces something the addon can read back.

        Seeded through the admin control plane, then read through the **real
        client** — so this asserts the fixture's shapes match what the addon
        parses, not merely that an HTTP call succeeded.
        """
        planted = self.mock.seed_transcript(
            "ws-fixture-seeded",
            [
                {"role": "user", "content": "what is in pod x?"},
                {"role": "assistant", "content": "three restarts"},
            ],
            name="Seeded session",
        )
        self.assertEqual(planted["id"], "ws-fixture-seeded")
        self.assertEqual(planted["kind"], "interactive")

        detail = self.connection._client().session_detail("ws-fixture-seeded")
        self.assertEqual(detail["persisted"]["ws_id"], "ws-fixture-seeded")
        self.assertEqual(
            [m["content"] for m in detail["messages"]],
            ["what is in pod x?", "three restarts"],
        )

        # And the row it plants can be adopted, which is what a transcript test
        # needs: a session in the mock, before anything opened it in Odoo.
        session = self.env["turnstone.session"].with_user(self.admin_user).adopt(
            self.connection.id, "ws-fixture-seeded"
        )
        self.assertTrue(session.adopted)
        self.assertEqual(session.message_ids.mapped("body"),
                         ["what is in pod x?", "three restarts"])

    def test_mock_request_log_records_what_the_addon_sent(self):
        """The request log is how "did it call once or five times" is asserted."""
        # The fixture's own reset is in the log already, so count rather than
        # expect emptiness: what matters is that ONE client call adds ONE entry.
        before = self.mock.request_log(limit=500)
        self.assertTrue(
            all(row["path"].startswith("/v1/api/admin/mock/") for row in before),
            "only the fixture's own control-plane calls should precede a test",
        )
        self.connection._client().whoami()
        entries = self.mock.request_log(path_prefix="/v1/api/auth/whoami")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["method"], "GET")
        self.assertEqual(entries[0]["status"], 200)
        self.assertNotIn(MOCK_TOKEN, repr(self.mock.request_log(limit=500)),
                         "the log must never record the token")
