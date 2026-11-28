"""The core of automation launching, end to end against the mock Turnstone.

Five behaviours a rule has to get right before anything is built on top of it,
and this file is their acceptance:

1. a successful launch creates a session **and** a run, and the session on the
   server is the one the run names;
2. a re-delivered idempotency key does not launch a second time;
3. a user who may not use the agent creates no remote session at all;
4. a server that refuses is recorded as a failed run rather than raised into a
   caller whose rollback would erase the evidence;
5. a prompt renders with a record and without one (and refuses cleanly when it
   needs a record it was not given).

Everything here goes over real HTTP to the mock server that
`scripts/test-odoo.sh` starts — and only to it. This class extends
`TurnstoneTransactionCase`, whose `setUpClass` patches
`urllib.request.urlopen` to refuse every authority but the mock's, so "no test
reaches a real Turnstone" is a property of the fixture rather than a promise in
a comment. `test_a_real_turnstone_host_is_refused_here` proves that pin is in
force for these cases; `test_common.py` proves the pin itself behaves.

Assertions about the server are made on the MOCK's own state and request log,
not on Odoo's bookkeeping: "we recorded one run" would be true even if two
workstreams existed on the server, and that is precisely the failure this suite
exists to catch. "Session count" therefore means the mock's workstream list, and
"no remote call" means an empty create entry in the mock's request log.

Related: `test_automation_launch.py` (OTI-5749) is the launch service's own
suite — 25 cases on its edge paths (window suppression, races, error bounds),
some of which stub the socket. This file is the story's acceptance for the core
path and uses the shared fixture throughout; where the two overlap this is a
second, independent statement of the contract written from the server's side,
not a copy of it.
"""

import io
import urllib.error
import urllib.request
from unittest.mock import patch

from odoo.addons.turnstone_integration.models.turnstone_client import TurnstoneClient
from odoo.tests import tagged

from .common import MOCK_TOKEN, TurnstoneTransactionCase

# Where an interactive session is created (the routing proxy, which is the only
# create surface that accepts `kind`). Used to count remote launches from the
# mock's request log — the server's own record, not Odoo's.
CREATE_PATH = "/v1/api/route/workstreams/new"

# The same path as `TurnstoneClient` reports it in an error: it builds its URLs
# from `base_url + "/v1/api"`, so the message it produces names the request
# relative to that root. Asserting the full path here would be asserting
# something the client never says.
CREATE_PATH_IN_CLIENT_API = "/route/workstreams/new"


class ServerErrorTransport:
    """`urllib.request.urlopen` replaced by one that answers a 500 with a body.

    Deliberately not a stub of `TurnstoneClient`: the real client builds the
    message, the real `_request` classifies the status, and the launch service
    redacts what comes back. Only the socket is replaced.

    The mock has no switchable failure modes in this checkout (OTI-5740 adds
    them, and is not merged yet); when it is, `?fail=500` replaces this class
    and the rest of the case stays as it is. The body can carry the token
    because that is exactly how a bearer header reaches an error message in the
    wild: a proxy echoing the request, or a framework printing its headers on
    an exception page.
    """

    def __init__(self, body):
        self.body = body

    def __call__(self, request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 500, "Internal Server Error", {},
            io.BytesIO(self.body.encode()),
        )


@tagged("standard", "-at_install", "post_install")
class AutomationCoreCase(TurnstoneTransactionCase):
    """A rule that launches a configured agent, and the helpers to read the mock.

    The fixture supplies the users (`user` — an ordinary Turnstone User,
    `outsider` — no Turnstone group), the connection pinned to the mock, the
    agent, and `mock` — the control-plane client. A `setUp` reset means every
    case starts from an empty server, so "exactly one workstream" never depends
    on the order the cases run in.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A manual rule needs no target model and its prompt uses no fields, so
        # the cases about idempotency and refusal do not have to build a
        # business record to reach the part they are testing.
        cls.rule = cls.env["turnstone.automation"].create({
            "name": "Core: daily summary",
            "agent_id": cls.agent.id,
            "trigger_type": "manual",
            "prompt_template": "Summarise today's queue.",
            "run_as": "fixed_user",
            "run_as_user_id": cls.user.id,
        })

    # -- reading the mock --------------------------------------------------

    def _workstreams(self):
        """The workstreams the mock holds — the server's own answer."""
        return self.mock.workstreams()

    def _creates(self):
        """Create calls the mock received. Empty means nothing was launched."""
        return self.mock.request_log(
            path_prefix=CREATE_PATH, method="POST", limit=200,
        )

    def _user_turns(self, workstream):
        """The prompt(s) the server stored, in order."""
        return [
            message["content"]
            for message in workstream["messages"]
            if message.get("role") == "user"
        ]

    def _sessions_for(self, agent):
        return self.env["turnstone.session"].search([("agent_id", "=", agent.id)])

    # -- fixtures ----------------------------------------------------------

    def _rule_for_record(self, suffix="a"):
        """A rule that watches `res.partner`, with a prompt that reads it."""
        return self.env["turnstone.automation"].create({
            "name": "Core record rule %s" % suffix,
            "agent_id": self.agent.id,
            "trigger_type": "record_create",
            "target_model_id": self.env["ir.model"]._get("res.partner").id,
            "prompt_template": "Greet {{ name }} at {{ email }}.",
            "run_as": "fixed_user",
            "run_as_user_id": self.user.id,
        })

    def _rule_as(self, user, name, agent=None):
        return self.env["turnstone.automation"].create({
            "name": name,
            "agent_id": (agent or self.agent).id,
            "trigger_type": "manual",
            "prompt_template": "Do the thing.",
            "run_as": "fixed_user",
            "run_as_user_id": user.id,
        })

    def _user(self, login, groups):
        company = self.env.company
        return self.env["res.users"].create({
            "name": login,
            "login": login,
            "company_id": company.id,
            "company_ids": [(6, 0, [company.id])],
            "group_ids": [(6, 0, [group.id for group in groups])],
        })


@tagged("standard", "-at_install", "post_install")
class TestSuccessfulLaunch(AutomationCoreCase):
    """Scenario 1: a launch creates a session and a run, and they are the same."""

    def test_a_launch_creates_one_session_and_one_run(self):
        run = self.rule._launch_for_record(idempotency_key="core-evt-1")

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(run.attempt_count, 1, "one attempt, and it landed")
        self.assertTrue(run.session_id, "the run must name the session it started")
        self.assertEqual(run.session_id.user_id, self.user,
                         "the session belongs to the run-as user, not to root")

        # The Odoo session and the server's workstream are the same one: the id
        # on the row is the id the server handed back, not a local guess.
        workstreams = self._workstreams()
        self.assertEqual(len(workstreams), 1, "one workstream on the server")
        self.assertEqual(run.session_id.ws_id, workstreams[0]["id"])

        # One call, not a create followed by a send: the prompt rides in the
        # create as `initial_message`.
        self.assertEqual(len(self._creates()), 1)
        self.assertEqual(self._user_turns(workstreams[0]),
                         ["Summarise today's queue."])

    def test_the_rule_counter_moves_once_per_launch(self):
        before = self.rule.run_count
        self.rule._launch_for_record(idempotency_key="core-count")

        self.assertEqual(self.rule.run_count, before + 1)
        self.assertTrue(self.rule.last_run_at)

    def test_the_same_prompt_arrives_verbatim_when_the_rule_has_no_record(self):
        """Scenario 5, second half: a prompt with no fields needs no record."""
        run = self.rule._launch_for_record(idempotency_key="core-norecord")

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(
            self._user_turns(self._workstreams()[0]),
            ["Summarise today's queue."],
            "a template with no {{ }} renders to itself",
        )


@tagged("standard", "-at_install", "post_install")
class TestDuplicateKey(AutomationCoreCase):
    """Scenario 2: one event, one session, however often it is re-delivered."""

    def test_a_duplicate_key_does_not_launch_a_second_session(self):
        first = self.rule._launch_for_record(idempotency_key="core-dup")
        creates_after_first = len(self._creates())
        self.assertEqual(first.state, "done", first.error)

        for _delivery in range(2):
            again = self.rule._launch_for_record(idempotency_key="core-dup")
            self.assertEqual(again.id, first.id,
                             "a duplicate answers with the first run")
            self.assertEqual(again.state, "done")

        # The criterion's own words: the SESSION COUNT is what proves it.
        self.assertEqual(len(self._workstreams()), 1,
                         "one session on the server after three deliveries")
        self.assertEqual(len(self._creates()), creates_after_first,
                         "and the server was asked to create only once")
        self.assertEqual(len(self._sessions_for(self.agent)), 1,
                         "one session row, so Odoo and the server agree")
        self.assertEqual(
            self.env["turnstone.automation.run"].search_count([
                ("automation_id", "=", self.rule.id),
                ("idempotency_key", "=", "core-dup"),
            ]),
            1,
            "one run per event",
        )

    def test_a_different_key_launches_a_different_session(self):
        """The dedupe is per event, not a global "launch once, ever"."""
        first = self.rule._launch_for_record(idempotency_key="core-key-a")
        second = self.rule._launch_for_record(idempotency_key="core-key-b")

        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(len(self._workstreams()), 2)


@tagged("standard", "-at_install", "post_install")
class TestUnauthorizedUser(AutomationCoreCase):
    """Scenario 3: no permission — a recorded refusal and nothing remote."""

    def _assert_nothing_was_launched(self, run, agent=None):
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 0,
                         "a refusal is not an attempt: nothing was tried")
        self.assertFalse(run.session_id)
        self.assertEqual(self._creates(), [],
                         "the server was never asked to create anything")
        self.assertEqual(self._workstreams(), [], "and holds no session")
        self.assertFalse(self._sessions_for(agent or self.agent),
                         "and no session row was left behind")

    def test_a_user_with_no_turnstone_access_starts_nothing(self):
        rule = self._rule_as(self.outsider, "Core: no Turnstone access")

        run = rule._launch_for_record(idempotency_key="core-perm-1")

        self.assertEqual(run.state, "failed")
        self.assertIn("do not have access to Turnstone", run.error)
        self._assert_nothing_was_launched(run)

    def test_a_turnstone_user_outside_the_agents_group_starts_nothing(self):
        restricted = self.env["turnstone.agent"].create({
            "name": "Core restricted agent",
            "connection_id": self.connection.id,
            "kind": "interactive",
            "group_ids": [(6, 0, [self.env.ref("base.group_system").id])],
        })
        # A Turnstone User — allowed to use Turnstone in general — but not in
        # the group this agent is restricted to.
        rule = self._rule_as(self.user, "Core: restricted agent rule",
                             agent=restricted)

        run = rule._launch_for_record(idempotency_key="core-perm-2")

        self.assertEqual(run.state, "failed")
        self.assertIn("not permitted to use the agent", run.error)
        self._assert_nothing_was_launched(run, agent=restricted)


@tagged("standard", "-at_install", "post_install")
class TestServerFailure(AutomationCoreCase):
    """Scenario 4: the server says no, and the run says why — without secrets."""

    def test_a_refused_token_is_recorded_on_the_run(self):
        """The mock really refuses (401), over real HTTP, with no stubbing.

        Not a simulation: `check_auth` on the mock compares the bearer token and
        answers 401, which is the same shape a revoked token produces against a
        live console. The launch must record it rather than raise.
        """
        self.connection.api_token = "not-the-token-the-mock-was-started-with"

        run = self.rule._launch_for_record(idempotency_key="core-401")

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 1, "an attempt was made, so it counts")
        self.assertIn("HTTP 401", run.error,
                      "the failure names what happened, not just that it failed")
        self.assertFalse(run.session_id)
        self.assertFalse(self._sessions_for(self.agent),
                         "a failed launch leaves no half-written session")
        self.assertEqual(self._workstreams(), [], "and no workstream on the server")
        self.assertNotIn(MOCK_TOKEN, run.error)

    def test_a_server_500_is_recorded_and_the_token_stays_out_of_the_run(self):
        """A 500 whose body echoes the request is where the bearer header goes."""
        body = "upstream said: Authorization: Bearer %s" % MOCK_TOKEN
        with patch("urllib.request.urlopen", ServerErrorTransport(body)):
            run = self.rule._launch_for_record(idempotency_key="core-500")

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 1)
        self.assertIn("HTTP 500", run.error)
        self.assertIn(CREATE_PATH_IN_CLIENT_API, run.error,
                      "only the secret is removed, not the reason")
        self.assertNotIn(MOCK_TOKEN, run.error,
                         "the token must never reach a run history")
        self.assertIn("[redacted]", run.error)
        self.assertFalse(run.session_id)

    def test_a_failed_run_is_visible_and_not_swallowed(self):
        """The launch never raises at a caller whose rollback would erase it."""
        self.connection.api_token = "not-the-token-the-mock-was-started-with"

        run = self.rule._launch_for_record(idempotency_key="core-visible")

        self.assertTrue(run.exists(), "the failed run is committed, not in memory")
        self.assertTrue(run.error)
        self.assertTrue(run.finished_at)
        self.assertIn(run.id, self.env["turnstone.automation.run"].search([]).ids)


@tagged("standard", "-at_install", "post_install")
class TestPromptRendering(AutomationCoreCase):
    """Scenario 5, first half: a prompt renders from the record that fired it."""

    def test_a_record_prompt_renders_into_the_message_body(self):
        partner = self.env["res.partner"].create({
            "name": "Core Test Co",
            "email": "ops@core.example",
        })
        rule = self._rule_for_record(suffix="render")

        run = rule._launch_for_record(partner, idempotency_key="core-record")

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(
            self._user_turns(self._workstreams()[0]),
            ["Greet Core Test Co at ops@core.example."],
            "the values come from the record the event was about",
        )
        self.assertEqual(run.trigger_res_model, "res.partner")
        self.assertEqual(run.trigger_res_id, partner.id)

    def test_the_rendered_prompt_never_becomes_the_session_name(self):
        """The prompt is untrusted content; the name is trusted config + ids."""
        partner = self.env["res.partner"].create({"name": "Nameable Co"})
        rule = self._rule_for_record(suffix="name")

        run = rule._launch_for_record(partner, idempotency_key="core-name")

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(run.session_id.name,
                         "%s: res.partner,%s" % (self.agent.name, partner.id))
        self.assertNotIn("Nameable Co", run.session_id.name)

    def test_a_record_prompt_with_no_record_fails_without_any_call(self):
        """The other "without": a prompt that needs a record it was not given.

        It is a configuration error, and it is refused before anything is tried
        — `attempt_count == 0` distinguishes "we never asked" from "we asked and
        the server refused", which is the difference a person debugging a rule
        needs first.
        """
        rule = self._rule_for_record(suffix="needs-record")

        run = rule._launch_for_record(None, idempotency_key="core-none")

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 0, "nothing was tried")
        self.assertIn("needs the record", run.error)
        self.assertEqual(self._creates(), [])
        self.assertEqual(self._workstreams(), [])


@tagged("standard", "-at_install", "post_install")
class TestMockOnlyNetwork(AutomationCoreCase):
    """Criterion 2 for this file: nothing here can reach a real Turnstone.

    `TurnstoneTransactionCase.setUpClass` patches `urllib.request.urlopen`, which
    is what both the addon's client and the fixture's control-plane calls go
    through. That makes the guarantee structural; this case proves the pin is in
    force for *these* cases rather than trusting that a base class applies.
    """

    def test_a_real_turnstone_host_is_refused_here(self):
        with self.assertRaises(AssertionError) as caught:
            urllib.request.urlopen(
                "https://console.turnstone.example.com/v1/api/auth/whoami", timeout=1,
            )
        self.assertIn("Refusing a real network request", str(caught.exception))

    def test_the_addons_own_client_is_refused_too(self):
        """The path a launch actually uses, not only a raw urlopen."""
        client = TurnstoneClient("https://console.turnstone.example.com", "some-token")

        with self.assertRaises(AssertionError) as caught:
            client.whoami()
        self.assertIn("Refusing a real network request", str(caught.exception))

    def test_the_mock_is_the_one_this_run_started(self):
        """The positive half: the address the cases use really is the mock."""
        health = self.mock.health()

        self.assertTrue(health.get("mock"),
                        "the pinned server identifies itself as the mock")