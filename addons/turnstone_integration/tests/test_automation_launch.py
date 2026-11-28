"""The launch service: one session and one prompt per event, and a record either way.

`turnstone.automation._launch_for_record()` is the single entry point other
modules call to start a configured agent with a prompt (OTI-5749). It reuses the
chat's session create, so the prompt travels as `initial_message` and one HTTP
request makes one workstream carrying one prompt.

These cases are the ticket's three acceptance criteria, plus the shapes a launch
must not get wrong along the way:

1. **One session, one prompt, per idempotency key.** The same key twice sends
   nothing the second time and answers with the first run — asserted against the
   MOCK SERVER's own state, not against Odoo's bookkeeping, because "we recorded
   one run" would be true even if two workstreams existed on the server.
2. **A user without the agent's permission gets a recorded failed run and no
   remote call.** "No remote call" is asserted on the mock's request log: a
   check that only reads Odoo cannot tell a refused launch from one that
   happened to fail afterwards.
3. **A server 500 produces a failed run carrying the error and no secret.**
   The mock has no injectable failure modes yet (its README reserves them for
   OTI-5740), so only the socket is replaced: `urllib.request.urlopen` raises
   the same `HTTPError` a real server would, and the real `TurnstoneClient`
   turns it into the real message. The response body contains the token, which
   is what makes the "no secret" assertion meaningful — the text being redacted
   actually holds the secret. The real-HTTP transport failure is covered
   separately by an unreachable base URL, which needs no patch at all.

**Since OTI-5786 the launch is two-phase, and these cases drive phase two.**
`_launch_for_record()` commits the *intent* (this run, and a `draft` session
carrying no `ws_id`) in the caller's transaction and POSTs nothing; the create
runs in a post-commit hook, on a cursor of its own. So a successful launch
returns the run `queued`, and every case that asserts `done` — or counts
workstreams at the mock — calls `self._deliver()` first. That is not a case
testing less: `_deliver()` runs the same body the hook runs, and the hook
cannot fire here because `TestCursor.commit()` **clears** `postcommit` instead
of running it and a test may not commit at all. What the split buys is the two
cases OTI-5800 adds at the bottom: a rollback after phase one that used to
leave a live workstream behind with no row pointing at it, and a delivery whose
transaction dies *after* the POST, where the next delivery has to adopt the
workstream that already exists rather than make a second one.

The rest are the invariants the code would be easy to get wrong on: a failed
run retries rather than being swallowed as a duplicate, a window that
suppressed an event is recorded as skipped rather than silently dropped, a
prompt renders from a record and fails cleanly when it cannot, the prompt never
becomes the session name, and a failure leaves no half-written session behind.
"""

import io
import json
import os
import urllib.error
import urllib.request
from contextlib import closing
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.turnstone_integration.models.redaction import REDACTED
from odoo.addons.turnstone_integration.models.turnstone_automation_run import (
    MAX_ERROR_CHARS,
)
from odoo.addons.turnstone_integration.models.turnstone_client import (
    BODY_EXCERPT_CHARS,
)

# The mock server's address inside the compose test network
# (docker-compose.test.yml service `mock-turnstone`, port 8099). A test never
# reaches a real Turnstone: scripts/test-odoo.sh starts this container, and
# MOCKTOKEN is the bearer token the compose file gives it — read from the
# environment rather than hardcoded, because the script generates a fresh one
# per run and a stale literal would fail every case with an unexplained 401.
MOCK_URL = "http://mock-turnstone:8099"
MOCK_TOKEN = os.environ.get("MOCK_TURNSTONE_TOKEN", "mock-token")

# A base URL nothing listens on. Connection refused, not a DNS timeout, so the
# case finishes immediately instead of waiting for a resolver.
UNREACHABLE = "http://127.0.0.1:1"


def _mock(method, path, body=None, token=MOCK_TOKEN):
    """Call the mock server's control plane. Returns ``(status, payload)``.

    Its own helper rather than the addon's `TurnstoneClient`, deliberately: the
    point of these assertions is what a third party can observe about the
    launch, not what the module under test believes it sent.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        MOCK_URL + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw.strip() else {})


def _mock_workstreams():
    """Every workstream the mock currently holds."""
    _status, payload = _mock("GET", "/v1/api/admin/mock/state")
    return payload.get("workstreams") or []


def _mock_requests(path_prefix=None):
    """The mock's request log — what Odoo actually sent, in order."""
    path = "/v1/api/admin/mock/requests?limit=500"
    if path_prefix:
        path += "&path_prefix=%s" % path_prefix
    _status, payload = _mock("GET", path)
    return payload.get("requests") or []


def _creates():
    """How many workstream-create calls the mock received."""
    return [r for r in _mock_requests() if r["path"].endswith("/workstreams/new")]


class FailingTransport:
    """`urllib.request.urlopen` replaced by one that answers with an error.

    Deliberately NOT a stub of `TurnstoneClient`: the real client builds the
    error message, the real `_request` classifies the status, and the launch
    service cleans what comes out. Only the socket is replaced, so the case
    proves the chain rather than proving a mock agreed with itself.

    The body can carry the token because that is exactly how a bearer header
    reaches an error message in the wild: a proxy echoing the request, or a
    framework printing its headers on an exception page.

    The status is a parameter because the leak is not a 500-only shape: a 401
    from a server that echoes the request, or a 4xx from a console that prints
    the offending URL, puts the credential in the same body and reaches the same
    log line.
    """

    def __init__(self, body, code=500):
        self.body = body
        self.code = code

    def __call__(self, request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, self.code, "Internal Server Error", {},
            io.BytesIO(self.body.encode()),
        )


@tagged("post_install", "-at_install")
class AutomationLaunchCase(TransactionCase):
    """Shared fixtures: a connection, an agent, a rule, and a clean mock."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # These tests are about the launch service's contract with Turnstone,
        # which means real HTTP to the mock. A database without the mock
        # reachable cannot prove any of it, so it is skipped rather than failed:
        # `odoo -u turnstone_integration --test-enable` in a plain dev stack
        # would otherwise report red for a missing container.
        try:
            status, _payload = _mock("GET", "/v1/api/admin/mock/state")
        except (urllib.error.URLError, OSError) as exc:
            cls.skipTest("the mock Turnstone server is not reachable at %s: %s"
                         % (MOCK_URL, exc))
        if status != 200:
            cls.skipTest("the mock Turnstone server answered %s at %s"
                         % (status, MOCK_URL))

        cls.company = cls.env.company
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Mock Turnstone (launch)",
            "base_url": MOCK_URL,
            "api_token": MOCK_TOKEN,
            "is_default": True,
        })
        cls.agent = cls.env["turnstone.agent"].create({
            "name": "Launch Test Agent",
            "connection_id": cls.connection.id,
            "kind": "interactive",
        })
        cls.user_group = cls.env.ref("turnstone_integration.group_turnstone_user")

        # A rule whose prompt needs no record, so a case about idempotency does
        # not have to build a business record to get to the part it is testing.
        cls.rule = cls.env["turnstone.automation"].create({
            "name": "Launch test rule",
            "company_id": cls.company.id,
            "agent_id": cls.agent.id,
            "trigger_type": "manual",
            "prompt_template": "Summarise today's queue.",
            "run_as": "fixed_user",
            "run_as_user_id": cls.env.user.id,
        })

    def setUp(self):
        super().setUp()
        # Every case starts from an empty mock: "exactly one workstream" would
        # otherwise depend on the order the cases run in.
        _mock("POST", "/v1/api/admin/mock/reset")

    # -- fixtures ----------------------------------------------------------

    def _rule_for_record(self, **vals):
        """A rule that watches `res.partner`, with a prompt that reads it."""
        suffix = vals.pop("suffix", "a")
        defaults = {
            "name": "Record rule %s" % suffix,
            "company_id": self.company.id,
            "agent_id": self.agent.id,
            "trigger_type": "record_create",
            "target_model_id": self.env["ir.model"]._get("res.partner").id,
            "prompt_template": "Greet {{ name }} at {{ email }}.",
            "run_as": "fixed_user",
            "run_as_user_id": self.env.user.id,
        }
        defaults.update(vals)
        return self.env["turnstone.automation"].create(defaults)

    def _rule_as(self, user, name, agent=None):
        return self.env["turnstone.automation"].create({
            "name": name,
            "company_id": self.company.id,
            "agent_id": (agent or self.agent).id,
            "trigger_type": "manual",
            "prompt_template": "Do the thing.",
            "run_as": "fixed_user",
            "run_as_user_id": user.id,
        })

    def _user(self, login, groups, company=None):
        company = company or self.company
        return self.env["res.users"].create({
            "name": login,
            "login": login,
            "company_id": company.id,
            "company_ids": [(6, 0, [company.id])],
            "group_ids": [(6, 0, [g.id for g in groups])],
        })

    def _patched_transport(self, body, code=500):
        """Replace the socket, not the client: the real error path runs."""
        return patch("urllib.request.urlopen", FailingTransport(body, code))

    def _deliver(self):
        """Run the phase-two delivery this transaction armed, as the hook would.

        `TestCursor.commit()` clears `postcommit` rather than running it
        (`odoo/tests/test_cursor.py`), and a `TransactionCase` may not commit at
        all, so the post-commit hook that delivers a launch can never fire in
        here. `_deliver_pending()` is the same body the hook runs — it pops the
        jobs *this* transaction armed and drives `_deliver_queued_run()` for each
        — so a case that asserts on the delivery drives it explicitly instead of
        asserting on a `queued` run and calling that success.

        Popping the queue is what makes it safe to call twice: the second call
        finds nothing, and the hook a later commit would run finds every run
        already `done` and leaves it alone.
        """
        return self.env["turnstone.automation"]._deliver_pending()


@tagged("post_install", "-at_install")
class TestOneSessionPerKey(AutomationLaunchCase):
    """Criterion 1: one session and one prompt per idempotency key."""

    def test_one_launch_makes_one_workstream_carrying_the_prompt(self):
        run = self.rule._launch_for_record(idempotency_key="evt-1")

        # Phase one only: the intent is committed, nothing has been sent, and
        # that is the contract now — see the module docstring.
        self.assertEqual(run.state, "queued", run.error)
        self.assertEqual(run.attempt_count, 0,
                         "nothing was tried until the delivery runs")
        self.assertTrue(run.session_id, "the run must name the session it will use")
        self.assertFalse(run.session_id.ws_id,
                         "the draft session has no workstream until it is delivered")
        self.assertEqual(_creates(), [], "phase one must make no remote call")

        self._deliver()

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(run.attempt_count, 1)
        self.assertTrue(run.session_id.ws_id, "the session must have a workstream")

        workstreams = _mock_workstreams()
        self.assertEqual(len(workstreams), 1, "exactly one workstream on the server")
        self.assertEqual(
            [m["content"] for m in workstreams[0]["messages"] if m["role"] == "user"],
            ["Summarise today's queue."],
            "the prompt must travel in the create, exactly once",
        )
        self.assertEqual(len(_creates()), 1,
                         "one create call — not a create followed by a send")

    def test_the_same_key_twice_sends_nothing_the_second_time(self):
        first = self.rule._launch_for_record(idempotency_key="evt-2")
        self._deliver()
        creates_after_first = len(_creates())
        self.assertEqual(creates_after_first, 1, "the first event must be delivered")

        second = self.rule._launch_for_record(idempotency_key="evt-2")
        # Driven too, deliberately: the second call must arm nothing, and a
        # delivery that had something to do would show up as a second create.
        self._deliver()

        self.assertEqual(second.id, first.id,
                         "the duplicate must answer with the run the first call made")
        self.assertEqual(second.state, "done")
        self.assertEqual(len(_creates()), creates_after_first,
                         "a duplicate key must make no remote call at all")
        self.assertEqual(len(_mock_workstreams()), 1, "one workstream, still")
        self.assertEqual(
            self.env["turnstone.automation.run"].search_count([
                ("automation_id", "=", self.rule.id),
                ("idempotency_key", "=", "evt-2"),
            ]),
            1,
            "one run per key — the constraint observed, not assumed",
        )

    def test_a_third_call_after_a_successful_one_is_still_a_duplicate(self):
        """Dedupe holds however many times the event is re-delivered."""
        run = self.rule._launch_for_record(idempotency_key="evt-3")
        self._deliver()
        for _attempt in range(3):
            self.assertEqual(
                self.rule._launch_for_record(idempotency_key="evt-3").id, run.id,
            )
            self._deliver()
        self.assertEqual(len(_creates()), 1)
        self.assertEqual(len(_mock_workstreams()), 1)

    def test_different_keys_make_different_sessions(self):
        """The dedupe is per event, not a global "only ever launch once"."""
        first = self.rule._launch_for_record(idempotency_key="evt-a")
        second = self.rule._launch_for_record(idempotency_key="evt-b")
        self._deliver()

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(len(_mock_workstreams()), 2)

    def test_no_key_generates_one_per_call(self):
        """A manual launch with no natural event id still launches each time."""
        first = self.rule._launch_for_record()
        second = self.rule._launch_for_record()
        self._deliver()

        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual(len(_mock_workstreams()), 2)

    def test_a_failed_run_retries_rather_than_being_swallowed(self):
        """`failed` means the event never landed, so the same key tries again.

        The alternative — answering a retry with the failed run and doing
        nothing — turns a transient transport error into a permanently dropped
        event. It is also why `attempt_count` counts attempts at one event.
        """
        # The connection is unreachable, so phase two's POST fails — phase one
        # still succeeds, because it makes no call to fail.
        self.connection.base_url = UNREACHABLE
        refused = self.rule._launch_for_record(idempotency_key="evt-retry")
        self.assertEqual(refused.state, "queued",
                         "a launch that has not been delivered yet is not failed")
        self._deliver()
        self.assertEqual(refused.state, "failed", refused.error)
        self.assertEqual(refused.attempt_count, 1)

        # The server is reachable again; the same event is retried.
        self.connection.base_url = MOCK_URL
        retried = self.rule._launch_for_record(idempotency_key="evt-retry")
        self._deliver()

        self.assertEqual(retried.id, refused.id, "a retry reuses the run for the event")
        self.assertEqual(retried.state, "done", "the retry must actually deliver")
        self.assertEqual(retried.attempt_count, 2, "both attempts are counted")
        self.assertEqual(len(_mock_workstreams()), 1, "one workstream from two attempts")

    def test_the_run_records_the_session_and_the_rule_counter_moves(self):
        before = self.rule.run_count
        run = self.rule._launch_for_record(idempotency_key="evt-count")
        self._deliver()

        self.assertEqual(self.rule.run_count, before + 1)
        self.assertTrue(self.rule.last_run_at)
        self.assertEqual(run.session_id.user_id, self.env.user,
                         "the session belongs to the user the run is attributed to")


@tagged("post_install", "-at_install")
class TestPermissionRefusal(AutomationLaunchCase):
    """Criterion 2: no permission — a recorded failed run and no remote call."""

    def test_a_user_outside_the_agents_groups_is_refused_before_any_call(self):
        # A user who may use Turnstone, but is not in the agent's own group.
        outsider = self._user("launch-outsider", [
            self.env.ref("base.group_user"), self.user_group,
        ])
        agent = self.env["turnstone.agent"].create({
            "name": "Restricted Agent",
            "connection_id": self.connection.id,
            "kind": "interactive",
            "group_ids": [(6, 0, [self.env.ref("base.group_system").id])],
        })
        rule = self._rule_as(outsider, "Restricted rule", agent=agent)

        run = rule._launch_for_record(idempotency_key="evt-perm")

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 0,
                         "a refusal is not an attempt — nothing was tried")
        self.assertIn("not permitted", run.error)
        self.assertFalse(run.session_id)
        self.assertEqual(_creates(), [], "a refused launch must make no remote call")
        self.assertEqual(_mock_workstreams(), [])
        self.assertFalse(self.env["turnstone.session"].search([
            ("agent_id", "=", agent.id),
        ]), "a refused launch must not leave a session behind")

    def test_a_user_without_the_turnstone_group_is_refused(self):
        nobody = self._user("launch-no-turnstone", [self.env.ref("base.group_user")])
        rule = self._rule_as(nobody, "No Turnstone rule")

        run = rule._launch_for_record(idempotency_key="evt-no-group")

        self.assertEqual(run.state, "failed")
        self.assertIn("do not have access to Turnstone", run.error)
        self.assertEqual(_creates(), [])

    def test_a_run_as_user_outside_the_rules_company_is_refused(self):
        """The company wall, reached the way it is actually reachable.

        `run_as_user_id` is `check_company=True`, so Odoo refuses a rule that
        pairs a company-A rule with a company-B run-as user at create time.
        That leaves one real path to this check: a rule that runs as whoever
        triggered it, where the triggering user is selected at launch and cannot
        have been checked when the rule was saved. Someone from another company
        must not be able to make a company-A rule start a session in it.
        """
        other = self.env["res.company"].create({"name": "Other Co (launch)"})
        stranger = self._user(
            "launch-other-company",
            [self.env.ref("base.group_user"), self.user_group],
            company=other,
        )
        rule = self.env["turnstone.automation"].create({
            "name": "Company wall rule",
            "company_id": self.company.id,
            "agent_id": self.agent.id,
            "trigger_type": "record_create",
            "target_model_id": self.env["ir.model"]._get("res.partner").id,
            "prompt_template": "Do the thing for {{ name }}.",
            "run_as": "triggering_user",
        })
        partner = self.env["res.partner"].create({"name": "Gated Co"})

        run = rule._launch_for_record(
            partner, triggering_user=stranger, idempotency_key="evt-company",
        )

        self.assertEqual(run.state, "failed")
        self.assertIn("does not work in", run.error)
        self.assertEqual(_creates(), [], "the company wall must be hit before any HTTP")

    def test_an_unconfigured_run_as_user_is_refused_not_defaulted_to_root(self):
        rule = self.env["turnstone.automation"].create({
            "name": "Triggering-user rule",
            "company_id": self.company.id,
            "agent_id": self.agent.id,
            "trigger_type": "webhook",
            "prompt_template": "Do the thing.",
            "run_as": "triggering_user",
        })

        # A webhook has no user. It must refuse, not quietly run as the
        # superuser and pass every check.
        run = rule._launch_for_record(idempotency_key="evt-nouser")

        self.assertEqual(run.state, "failed")
        self.assertIn("has no user", run.error)
        self.assertEqual(_creates(), [])

    def test_a_refusal_is_retried_with_the_same_key_once_permission_is_granted(self):
        """The refusal is not final: fixing the permission and re-firing works."""
        late = self._user("launch-late-approval", [self.env.ref("base.group_user")])
        rule = self._rule_as(late, "Late approval rule")

        refused = rule._launch_for_record(idempotency_key="evt-late")
        self.assertEqual(refused.state, "failed")

        late.group_ids = [(4, self.user_group.id)]
        delivered = rule._launch_for_record(idempotency_key="evt-late")
        self._deliver()

        self.assertEqual(delivered.id, refused.id)
        self.assertEqual(delivered.state, "done", delivered.error)
        self.assertEqual(len(_mock_workstreams()), 1)


@tagged("post_install", "-at_install")
class TestRecordCompanyWall(AutomationLaunchCase):
    """A trigger record of another company launches nothing, and says so.

    The adapters filter a record through `turnstone.trigger.record._company_ok()`
    before queueing a run, so nothing in-tree reaches this today. That is exactly
    why it is tested at the service: the adapters are the first line of defence,
    and a service that trusts its caller is one caller away from putting company
    B's field values into company A's prompt. `_launch_for_record()` is the
    documented entry point for any module, so the wall belongs here too.

    The two permissive cases are pinned as hard as the refusal: an over-eager
    check would break every rule watching a model with no `company_id` (a
    country, a currency, a partner tag) and every rule on a shared record.
    """

    def _other_company(self, name="Other Co (record wall)"):
        return self.env["res.company"].create({"name": name})

    def test_a_record_of_another_company_is_refused_before_any_call(self):
        other = self._other_company()
        # A company-B partner, named so the assertion can prove the name did not
        # travel: the run row is company A's, and its error is read by company
        # A's managers.
        partner = self.env["res.partner"].create({
            "name": "Company B Secret Co",
            "email": "secret@b.example",
            "company_id": other.id,
        })
        rule = self._rule_for_record(suffix="company-wall")

        run = rule._launch_for_record(partner, idempotency_key="evt-company-wall")

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 0, "nothing was tried")
        self.assertIn(other.display_name, run.error,
                      "the refusal must name the record's company")
        self.assertIn(self.company.display_name, run.error,
                      "and the rule's, so the mismatch is legible")
        self.assertNotIn("Company B Secret Co", run.error,
                         "the other company's business data must not cross the "
                         "wall in the explanation either")
        self.assertFalse(run.session_id, "a refused launch creates no session")
        self.assertEqual(_creates(), [], "the wall must be hit before any HTTP")
        self.assertEqual(_mock_workstreams(), [], "nothing on the server")

    def test_a_record_of_the_rules_own_company_still_launches(self):
        """The control: the check refuses a mismatch, not a stamped record."""
        partner = self.env["res.partner"].create({
            "name": "Company A Co",
            "company_id": self.company.id,
        })
        rule = self._rule_for_record(suffix="company-same")

        run = rule._launch_for_record(partner, idempotency_key="evt-company-same")

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(len(_creates()), 1)

    def test_a_model_with_no_company_field_is_unaffected(self):
        """A model that carries no `company_id` has no company to compare."""
        rule = self._rule_for_record(
            suffix="no-company-field",
            target_model_id=self.env["ir.model"]._get("res.country").id,
            prompt_template="Write a note about {{ name }} ({{ code }}).",
        )
        country = self.env["res.country"].create({
            "name": "Turnstonia", "code": "TS",
        })
        self.assertNotIn("company_id", country._fields,
                         "the fixture is only meaningful on a model with no company")

        run = rule._launch_for_record(country, idempotency_key="evt-no-company")

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(
            _mock_workstreams()[0]["messages"][0]["content"],
            "Write a note about Turnstonia (TS).",
        )

    def test_a_shared_record_is_allowed(self):
        """An empty company on the record means shared, which Odoo allows."""
        partner = self.env["res.partner"].create({"name": "Shared Contact"})
        self.assertFalse(partner.company_id, "the fixture's partner is shared")
        rule = self._rule_for_record(suffix="company-shared")

        run = rule._launch_for_record(partner, idempotency_key="evt-company-shared")

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(len(_creates()), 1)

    def test_the_drain_is_covered_too(self):
        """The check lives in the one attempt path, not only the caller's.

        `_attempt_launch()` is what #5754's drain calls, so a check written into
        `_launch_for_record()` alone would leave the cron on the old path. This
        drives the attempt directly, with no launch service in front of it.
        """
        other = self._other_company("Other Co (drain wall)")
        partner = self.env["res.partner"].create({
            "name": "Drained B Co", "company_id": other.id,
        })
        rule = self._rule_for_record(suffix="company-drain")
        run = self.env["turnstone.automation.run"].sudo()._open_for_event(
            rule, "evt-drain-company", record=partner,
        )[0]

        rule._attempt_launch(run, partner, None, "Greet Drained B Co.")

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 0)
        self.assertIn(other.display_name, run.error)
        self.assertEqual(_creates(), [])


@tagged("post_install", "-at_install")
class TestTransportFailure(AutomationLaunchCase):
    """Criterion 3: a 500 becomes a failed run with the error and no secret."""

    def test_a_server_500_is_recorded_and_the_token_is_redacted(self):
        # The body echoes the request the way a proxy or an exception page does.
        body = 'upstream said: Authorization: Bearer %s' % MOCK_TOKEN
        with self._patched_transport(body):
            run = self.rule._launch_for_record(idempotency_key="evt-500")
            self._deliver()

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 1, "an attempt was made, so it is counted")
        self.assertIn("HTTP 500", run.error,
                      "the failure has to say what happened, not just that it failed")
        self.assertIn("/route/workstreams/new", run.error,
                      "the error keeps the rest of the message; only the secret goes")
        self.assertNotIn(MOCK_TOKEN, run.error,
                         "the API token must never reach a run history")
        self.assertIn("[redacted]", run.error,
                      "the secret is replaced, not the whole message thrown away")
        # The draft session SURVIVES a failed delivery, deliberately (OTI-5786):
        # its id is the workstream id, so keeping it is what lets the retry of
        # this event present the same id instead of minting a second workstream.
        # What must not survive is a claim that anything was started.
        self.assertTrue(run.session_id, "the draft must survive so the retry can reuse it")
        self.assertFalse(run.session_id.ws_id,
                         "a failed delivery started no workstream")
        self.assertEqual(_mock_workstreams(), [],
                         "the mock must hold nothing for a launch that never landed")

    def test_a_500_that_does_not_mention_the_token_is_not_over_redacted(self):
        """Redaction takes the secret out; it does not blank the reason."""
        with self._patched_transport('{"error": "queue is full"}'):
            run = self.rule._launch_for_record(idempotency_key="evt-500-clean")
            self._deliver()

        self.assertEqual(run.state, "failed")
        self.assertIn("queue is full", run.error)
        self.assertNotIn("[redacted]", run.error,
                         "nothing secret was there, so nothing is replaced")

    def test_an_oversized_error_is_bounded(self):
        """A 500 with an HTML error page must not become the record.

        Two bounds apply and this proves the combination, not an assumption:
        `TurnstoneClient._request` caps the response body at 500 characters
        before it ever reaches a message, and `_clean_text` caps the whole error
        at `MAX_ERROR_CHARS`. A 5000-character body therefore cannot widen the
        run row. (The `… [truncated]` marker comes from the second bound and is
        exercised directly below, because a 500 never gets that far.)
        """
        with self._patched_transport("x" * 5000):
            run = self.rule._launch_for_record(idempotency_key="evt-huge")
            self._deliver()

        self.assertEqual(run.state, "failed")
        self.assertLessEqual(
            len(run.error), MAX_ERROR_CHARS + 50,
            "a runaway response must not become the error record",
        )

    def test_an_error_over_the_ceiling_is_truncated_with_a_visible_marker(self):
        """The second bound, tested where it is actually the one that acts.

        An exception raised by something other than the HTTP client (a driver
        error, a very long path, a chained message) never passes through the
        client's 500-character cap, so `_clean_text` is what has to hold.
        """
        rule = self.env["turnstone.automation"].create({
            "name": "Long error rule",
            "company_id": self.company.id,
            "agent_id": self.agent.id,
            "trigger_type": "manual",
            "prompt_template": "Do the thing.",
            "run_as": "fixed_user",
            "run_as_user_id": self.env.user.id,
        })
        run = rule._launch_for_record(idempotency_key="evt-long")
        self._deliver()
        self.assertEqual(run.state, "done", run.error)

        with patch.object(
            type(rule), "_create_launch_session",
            side_effect=RuntimeError("y" * 5000),
        ):
            failed = rule._launch_for_record(idempotency_key="evt-long-2")

        self.assertEqual(failed.state, "failed")
        self.assertLessEqual(
            len(failed.error), MAX_ERROR_CHARS + 50,
        )
        self.assertIn("truncated", failed.error, "and says so, visibly")

    def test_an_unreachable_server_is_recorded_without_the_token(self):
        self.connection.base_url = UNREACHABLE
        run = self.rule._launch_for_record(idempotency_key="evt-down")
        self._deliver()

        self.assertEqual(run.state, "failed")
        self.assertIn("Cannot reach Turnstone", run.error)
        self.assertNotIn(MOCK_TOKEN, run.error)

    def test_a_failed_delivery_keeps_the_draft_so_the_retry_reuses_its_id(self):
        """What a failed delivery leaves behind, and why it is a draft on purpose.

        This used to assert no session at all. OTI-5786 inverted it deliberately:
        the session row *is* the idempotency token (its id derives the
        workstream id), so throwing it away on a transport failure is what would
        let the retry of the same event mint a different id and start a second
        workstream at the server. A `draft` with no `ws_id` is the honest record
        of "prepared, sent, not confirmed", and `_launch_session()` reuses it.
        """
        self.connection.base_url = UNREACHABLE
        run = self.rule._launch_for_record(idempotency_key="evt-nohalf")
        self._deliver()

        self.assertEqual(run.state, "failed")
        sessions = self.env["turnstone.session"].search([
            ("agent_id", "=", self.agent.id),
        ])
        self.assertEqual(len(sessions), 1, "exactly one draft, and it is the run's")
        self.assertEqual(sessions, run.session_id)
        self.assertFalse(sessions.ws_id, "no workstream was confirmed")
        self.assertEqual(sessions.state, "draft", "so it is still not started")
        self.assertEqual(_mock_workstreams(), [],
                         "nothing was created at the server, either")

    def test_a_failure_is_visible_not_swallowed(self):
        """The launch never raises at a caller whose rollback would erase it."""
        self.connection.base_url = UNREACHABLE
        run = self.rule._launch_for_record(idempotency_key="evt-visible")
        self._deliver()

        self.assertEqual(run.state, "failed")
        self.assertTrue(run.error, "a failed run always says why")
        self.assertTrue(run.finished_at)
        self.assertIn(run.id, self.env["turnstone.automation.run"].search([]).ids,
                      "the failed run is committed and findable, not just in memory")


@tagged("post_install", "-at_install")
class TestPromptAndWindow(AutomationLaunchCase):
    """The shapes around the launch that must not silently change behaviour."""

    def test_a_record_prompt_renders_and_the_name_stays_trusted(self):
        partner = self.env["res.partner"].create({
            "name": "Acme Widgets",
            "email": "ops@acme.example",
        })
        rule = self._rule_for_record(suffix="render")

        run = rule._launch_for_record(partner, idempotency_key="evt-record")
        self._deliver()

        self.assertEqual(run.state, "done", run.error)
        messages = _mock_workstreams()[0]["messages"]
        self.assertEqual(messages[0]["content"],
                         "Greet Acme Widgets at ops@acme.example.")
        # The prompt is untrusted content built from business records: it goes
        # in the body and nowhere else. The session name is trusted config plus
        # identifiers, so a customer name can never reach a title.
        self.assertEqual(run.session_id.name,
                         "Launch Test Agent: res.partner,%s" % partner.id)
        self.assertNotIn("Acme Widgets", run.session_id.name)

    def test_the_run_remembers_what_it_was_about(self):
        partner = self.env["res.partner"].create({"name": "Tracked Co"})
        rule = self._rule_for_record(suffix="tracked")

        run = rule._launch_for_record(partner, idempotency_key="evt-track")

        self.assertEqual(run.trigger_res_model, "res.partner")
        self.assertEqual(run.trigger_res_id, partner.id)
        self.assertEqual(run.dedupe_token, "res.partner,%s" % partner.id,
                         "the token defaults to the record, so the window can match")

    def test_a_record_prompt_with_no_record_fails_cleanly(self):
        rule = self._rule_for_record(suffix="needs-record")

        run = rule._launch_for_record(None, idempotency_key="evt-norecord")

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.attempt_count, 0, "nothing was tried")
        self.assertIn("needs the record", run.error)
        self.assertEqual(_creates(), [])

    def test_the_window_suppresses_a_repeat_on_the_same_record_and_says_so(self):
        partner = self.env["res.partner"].create({"name": "Chatty Co"})
        rule = self._rule_for_record(suffix="window", dedupe_window_minutes=60)

        first = rule._launch_for_record(partner, idempotency_key="evt-w1")
        self._deliver()
        second = rule._launch_for_record(partner, idempotency_key="evt-w2")

        self.assertEqual(first.state, "done")
        self.assertEqual(second.state, "skipped",
                         "a repeat inside the window is recorded, not dropped")
        self.assertIn("suppresses repeats", second.error)
        self.assertIn(str(first.id), second.error,
                      "the skip names the run that suppressed it")
        self.assertEqual(len(_mock_workstreams()), 1,
                         "the window must keep the second launch off the server")

    def test_the_window_does_not_suppress_a_different_record(self):
        one = self.env["res.partner"].create({"name": "One Co"})
        two = self.env["res.partner"].create({"name": "Two Co"})
        rule = self._rule_for_record(suffix="window2", dedupe_window_minutes=60)

        first = rule._launch_for_record(one, idempotency_key="evt-d1")
        second = rule._launch_for_record(two, idempotency_key="evt-d2")
        self._deliver()

        self.assertEqual(first.state, "done")
        self.assertEqual(second.state, "done", second.error)
        self.assertEqual(len(_mock_workstreams()), 2)

    def test_the_window_does_not_block_the_retry_of_a_failed_event(self):
        """A failed run is not "handled", so the window must not eat its retry."""
        partner = self.env["res.partner"].create({"name": "Flaky Co"})
        rule = self._rule_for_record(suffix="window3", dedupe_window_minutes=60)

        self.connection.base_url = UNREACHABLE
        failed = rule._launch_for_record(partner, idempotency_key="evt-f1")
        self._deliver()
        self.assertEqual(failed.state, "failed")

        self.connection.base_url = MOCK_URL
        retried = rule._launch_for_record(partner, idempotency_key="evt-f1")
        self._deliver()

        self.assertEqual(retried.state, "done", retried.error)
        self.assertEqual(len(_mock_workstreams()), 1)

    def test_the_prompt_is_kept_only_when_the_rule_asks(self):
        rule = self._rule_for_record(suffix="nokeep")
        run = rule._launch_for_record(
            self.env["res.partner"].create({"name": "Quiet Co"}),
            idempotency_key="evt-nokeep",
        )
        self._deliver()
        self.assertEqual(run.state, "done", run.error)
        self.assertFalse(run.prompt, "retention is off by default")

        rule.store_prompt = True
        kept = rule._launch_for_record(
            self.env["res.partner"].create({"name": "Loud Co"}),
            idempotency_key="evt-keep",
        )
        self._deliver()
        self.assertEqual(kept.state, "done", kept.error)
        self.assertIn("Loud Co", kept.prompt)

    def test_a_multi_record_launch_is_refused_with_an_explanation(self):
        partner = self.env["res.partner"].create({"name": "Multi Co"})
        rule = self._rule_for_record(suffix="multi")
        with self.assertRaises(UserError) as caught:
            rule._launch_for_record(partner | partner.copy())
        self.assertIn("one event", str(caught.exception))
        self.assertEqual(_creates(), [])

    def test_the_audit_row_names_the_owner_not_the_launching_process(self):
        run = self.rule._launch_for_record(idempotency_key="evt-audit")
        self._deliver()
        self.assertEqual(run.state, "done", run.error)

        entry = self.env["turnstone.audit.log"].search([
            ("session_id", "=", run.session_id.id),
            ("action", "=", "session_create"),
        ], limit=1)
        self.assertTrue(entry, "a launch must be in the audit trail")
        self.assertEqual(entry.user_id, self.env.user)
        self.assertIn(str(run.id), entry.detail)


@tagged("post_install", "-at_install")
class TestConcurrentClaim(AutomationLaunchCase):
    """The paths the dedupe guarantee actually rests on.

    A TransactionCase cannot run two transactions at once, so these drive the two
    helpers directly. The point is that neither of them can raise into a caller:
    a duplicate delivery arriving in the same instant as the first must be
    answered with the winner's run, not an `IntegrityError`.
    """

    def test_opening_an_event_twice_returns_the_first_run_instead_of_raising(self):
        runs = self.env["turnstone.automation.run"].sudo()
        first, created_first = runs._open_for_event(self.rule, "evt-race")
        self.assertTrue(created_first)

        # The second caller walks the same code: it has no run when it looks,
        # and by the time it inserts, the first one's row is there.
        second, created_second = runs._open_for_event(self.rule, "evt-race")

        self.assertFalse(created_second,
                         "losing the race means someone else is launching")
        self.assertEqual(second.id, first.id, "and the answer is the winner's run")
        self.assertEqual(
            self.env["turnstone.automation.run"].search_count([
                ("automation_id", "=", self.rule.id),
                ("idempotency_key", "=", "evt-race"),
            ]),
            1,
        )

    def test_a_lost_launch_race_makes_no_remote_call(self):
        """The rule's half of it: `mine=False` must mean "do nothing"."""
        runs = self.env["turnstone.automation.run"].sudo()
        winner, _created = runs._open_for_event(self.rule, "evt-race2")
        winner._mark_running()

        # A concurrent delivery of the same event arrives while the winner is
        # mid-launch. It must answer with that run and send nothing.
        run = self.rule._launch_for_record(idempotency_key="evt-race2")
        self._deliver()

        self.assertEqual(run.id, winner.id)
        self.assertEqual(run.state, "running",
                         "a launch in flight is left alone, not raced")
        self.assertEqual(_creates(), [], "the loser must not start a second session")

    def test_the_retry_lock_refuses_a_run_that_is_not_failed(self):
        """Only a failed run may be claimed for a retry."""
        runs = self.env["turnstone.automation.run"].sudo()
        run, _created = runs._open_for_event(self.rule, "evt-lock")
        self.assertFalse(runs._lock_for_retry(run),
                         "a queued run is not a retry candidate")

        run._mark_running()
        self.assertFalse(runs._lock_for_retry(run))

        run._mark_failed("boom")
        claimed = runs._lock_for_retry(run)
        self.assertEqual(claimed.id, run.id, "a failed run is")

    def test_an_unexpected_exception_still_lands_on_the_run(self):
        """A bug in the launch must not leave a run silently stuck in queued.

        The count is 0, and that is the two-phase split being honest rather than
        a weaker assertion: `_create_launch_session` runs in phase *one*, before
        anything has been sent, so nothing was tried. `_mark_running()` moved to
        phase two for exactly this reason — a run that counted an attempt before
        the request that makes it would inflate the count on a path a rollback
        can still erase.
        """
        with patch.object(
            type(self.rule), "_create_launch_session",
            side_effect=RuntimeError("a bug, not a transport error"),
        ):
            run = self.rule._launch_for_record(idempotency_key="evt-bug")

        self.assertEqual(run.state, "failed")
        self.assertIn("a bug, not a transport error", run.error)
        self.assertEqual(run.attempt_count, 0, "nothing was sent, so nothing is counted")
        self.assertNotIn("running", run.state, "nothing is left mid-flight")
        self.assertEqual(_creates(), [], "a failure in phase one sends nothing")


@tagged("post_install", "-at_install")
class TestLaunchWhileSudoed(AutomationLaunchCase):
    """A server action or a cron calls this with more rights than a user has.

    The launch has to keep attribute to the run-as user rather than to whoever
    the process happens to be, or a permission refusal would silently pass and
    the audit trail would name nobody.
    """

    def test_sudo_does_not_widen_permission(self):
        outsider = self._user("launch-sudo-outsider",
                              [self.env.ref("base.group_user")])
        rule = self._rule_as(outsider, "Sudo refusal rule")

        # A cron running as the superuser: the caller's rights must not leak
        # into the check, which is made against the run-as user.
        run = rule.sudo()._launch_for_record(idempotency_key="evt-sudo")

        self.assertEqual(run.state, "failed")
        self.assertIn("do not have access to Turnstone", run.error)
        self.assertEqual(_creates(), [])

    def test_a_sudoed_launch_still_attributes_the_session_to_the_owner(self):
        run = self.rule.sudo()._launch_for_record(idempotency_key="evt-sudo-ok")
        self._deliver()

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(run.session_id.user_id, self.env.user,
                         "sudo() must not make the session root's")


@tagged("post_install", "-at_install")
class TestClientErrorLogging(AutomationLaunchCase):
    """The client's own log line: a failing call must not print the token.

    `TurnstoneClient._request` runs before any caller exists, so it cannot rely
    on its callers to clean anything: whatever it writes is already in the Odoo
    log by the time the launch service, or a chat controller, sees the exception.
    A 500 that echoes the request is the real shape of the leak — a proxy
    repeating the request, or a framework printing headers on an exception page
    — and that body carries `Authorization: Bearer <token>`.

    These cases capture the client logger directly with `assertLogs`, rather
    than inferring from a run's `error` column: the column is cleaned by a
    different piece of code (`_clean_text`), so a green assertion there would
    say nothing about the log.
    """

    #: The logger `_request` warns on. Named, not `""`/root: capturing the root
    #: logger would also pick up Odoo's own request logging, and a passing
    #: assertion would no longer be about this line.
    LOGGER = "odoo.addons.turnstone_integration.models.turnstone_client"

    def _client_log(self, body, code=500):
        """The client's log output for one failing request, as one string."""
        with self.assertLogs(self.LOGGER, level="DEBUG") as captured:
            with self._patched_transport(body, code):
                with self.assertRaises(UserError):
                    self.connection._client().whoami()
        return "\n".join(captured.output)

    def _assert_token_free(self, logged, token=MOCK_TOKEN):
        self.assertNotIn(token, logged,
                         "the API token reached the Odoo log through a client "
                         "error; a log aggregator is read by more people than a "
                         "connection is")
        self.assertNotIn("Bearer " + token, logged)
        self.assertNotIn(token[:12], logged,
                         "no fragment of the token either — a truncated body must "
                         "not leave a prefix behind")

    # -- criterion 1: the token cannot reach the log ------------------------

    def test_an_echoed_bearer_header_is_not_logged(self):
        """The exact shape the ticket names: a 500 whose body echoes the request."""
        body = 'upstream said: Authorization: Bearer %s' % MOCK_TOKEN
        logged = self._client_log(body)

        self._assert_token_free(logged)
        self.assertIn(REDACTED, logged,
                      "the secret is replaced, not the whole diagnostic dropped")
        self.assertIn("HTTP 500", logged, "the status still has to be logged")

    def test_the_token_is_not_logged_even_in_a_shape_no_pattern_knows(self):
        """Explicit secrets are the backstop: the client knows its own token.

        A server that prints the token as a bare fragment — no `Bearer`, no
        header name, no parameter — defeats every pattern. It does not defeat
        the literal, which is why the client passes it.
        """
        body = "the value %s was rejected by node-3" % MOCK_TOKEN
        logged = self._client_log(body)

        self._assert_token_free(logged)
        self.assertIn("node-3", logged, "the rest of the reason survives")

    def test_a_json_body_quoting_the_request_headers_is_not_logged(self):
        """A framework's exception page is JSON, not a sentence."""
        body = json.dumps({
            "error": "boom",
            "request": {"headers": {"Authorization": "Bearer " + MOCK_TOKEN}},
        })
        logged = self._client_log(body)

        self._assert_token_free(logged)
        self.assertIn("boom", logged)

    def test_a_4xx_body_carrying_the_token_is_not_logged(self):
        """The leak is not 500-only: a 4xx echo reaches the same line."""
        body = '{"detail": "unauthorized for Authorization: Bearer %s"}' % MOCK_TOKEN
        logged = self._client_log(body, code=422)

        self._assert_token_free(logged)
        self.assertIn("HTTP 422", logged)

    def test_a_token_in_a_query_parameter_is_not_logged(self):
        """A console that prints the URL it refused, credential and all."""
        body = "GET /v1/api/cluster/ws?token=%s&page=2 -> denied" % MOCK_TOKEN
        logged = self._client_log(body)

        self._assert_token_free(logged)
        self.assertIn("token=", logged,
                      "the parameter's name stays: a redacted line is still "
                      "diagnosable, a blanked one is not")

    def test_the_raised_error_is_clean_too(self):
        """The other door into the log: Odoo logs a `UserError`'s message.

        `TurnstoneError` subclasses `UserError`, and `odoo/http.py` answers one
        with `_logger.warning(exc)`. The exception's text is built from the same
        response body, so it has to be clean at the source — redacting only our
        own warning line would leave the token in the log whenever the request
        came from a controller rather than the launch service.
        """
        body = 'Authorization: Bearer %s' % MOCK_TOKEN
        with self._patched_transport(body):
            with self.assertRaises(UserError) as raised:
                self.connection._client().whoami()

        self._assert_token_free(str(raised.exception))
        self.assertIn("HTTP 500", str(raised.exception),
                      "the reason still has to reach the caller")

    # -- criterion 2: the reason is still logged, redacted and bounded -------

    def test_the_log_line_still_names_the_method_the_path_and_the_status(self):
        """Criterion 2: redaction removes the secret, not the diagnostic."""
        logged = self._client_log(
            'Authorization: Bearer %s' % MOCK_TOKEN
        )

        self.assertIn("GET", logged)
        self.assertIn("/auth/whoami", logged,
                      "the path is how a person knows which call failed")
        self.assertIn("HTTP 500", logged)

    def test_a_body_with_no_secret_is_logged_whole(self):
        """And nothing is over-redacted into uselessness."""
        logged = self._client_log('{"error": "queue is full"}')

        self.assertIn("queue is full", logged)
        self.assertNotIn(REDACTED, logged,
                         "nothing secret was there, so nothing is replaced")

    def test_the_logged_body_is_still_bounded(self):
        """A proxy's HTML error page must not become a log line.

        The bound is asserted on the log output itself, not on the exception:
        this is the line the ticket is about.
        """
        logged = self._client_log("x" * 5000)

        self.assertLessEqual(
            len(logged), BODY_EXCERPT_CHARS + 200,
            "the logged excerpt must stay near the 500-character bound; the "
            "rest of the line is the method, path and status",
        )

    def test_a_long_body_with_a_secret_at_the_end_is_still_redacted(self):
        """Bounding before redacting must not let the tail through.

        The excerpt is cut first, so a secret past the cut is not logged at all
        — and one just inside it is still redacted. Both outcomes are safe; this
        pins the second, where the two bounds interact.
        """
        body = "x" * 400 + " Authorization: Bearer %s" % MOCK_TOKEN
        logged = self._client_log(body)

        self._assert_token_free(logged)

    # -- no other client behaviour moved ------------------------------------

    def test_a_classified_status_still_raises_its_own_error(self):
        """Redaction happens at the read; classification is untouched.

        401 and 404 have their own exception types and their own messages, and
        a fix that rewrote the whole handler could quietly collapse them.
        """
        from odoo.addons.turnstone_integration.models.turnstone_client import (
            TurnstoneError,
            TurnstoneNotFound,
        )

        with self._patched_transport("no", code=401):
            with self.assertRaises(TurnstoneError) as unauthorized:
                self.connection._client().whoami()
        self.assertIn("rejected the API token", str(unauthorized.exception))

        with self._patched_transport("no", code=404):
            with self.assertRaises(TurnstoneNotFound):
                self.connection._client().whoami()


@tagged("post_install", "-at_install")
class TestRedactionHelper(AutomationLaunchCase):
    """The shared helper and `_clean_text`, tested where they actually run.

    Two layers take credentials out of text, and this ticket touched both: the
    client cleans a response body before it logs or raises, and the run model
    cleans every message it *stores* (`_clean_text`). A test that only exercises
    the client would leave the second layer unpinned — it is still the one that
    covers an exception the client never built (a `_launch_secrets()` literal in
    a message from anywhere else), so it gets its own assertions here.
    """

    def test_clean_text_still_redacts_after_the_shared_extraction(self):
        """The run model's own layer, asserted directly.

        Before this ticket the patterns lived in `turnstone_automation_run`.
        They now live in `models/redaction.py` and `_clean_text` calls that; this
        case is what proves the extraction did not quietly drop the behaviour
        while the client's own cleaning made the end-to-end case pass anyway.
        """
        run = self.env["turnstone.automation.run"]
        cleaned = run._clean_text(
            "Authorization: Bearer %s" % MOCK_TOKEN, [MOCK_TOKEN],
        )

        self.assertNotIn(MOCK_TOKEN, cleaned)
        self.assertIn(REDACTED, cleaned)

    def test_clean_text_takes_an_explicit_secret_in_any_shape(self):
        """The literal layer: a secret no pattern could recognise."""
        run = self.env["turnstone.automation.run"]
        cleaned = run._clean_text("node-3 said %s" % MOCK_TOKEN, [MOCK_TOKEN])

        self.assertNotIn(MOCK_TOKEN, cleaned)
        self.assertIn("node-3 said", cleaned)

    def test_clean_text_is_still_bounded(self):
        """And the cap the run history depends on survived the extraction."""
        run = self.env["turnstone.automation.run"]
        cleaned = run._clean_text("z" * (MAX_ERROR_CHARS + 500))

        self.assertLessEqual(len(cleaned), MAX_ERROR_CHARS + 50)
        self.assertIn("truncated", cleaned)

@tagged("post_install", "-at_install")
class TestRollbackRetryLaunchesOnce(AutomationLaunchCase):
    """OTI-5786's whole point, asserted where it can actually be seen.

    `_attempt_launch()` used to create the session and POST the create inside
    the caller's transaction. The workstream id presented to Turnstone derives
    from the session row's **id**, and that row was rolled back with everything
    else — so Odoo's handler retry (`service.model.retrying()`, up to five tries
    on a `SerializationFailure`; `ir.cron` the same) re-ran with a session that
    had never existed, minted a fresh id, and started **a second workstream
    carrying the same prompt**.

    Every assertion here reads the mock's own state (`/admin/mock/state`) and
    request log (`/admin/mock/requests`), never Odoo's bookkeeping. "We recorded
    one run" would be true even with two workstreams at the server, and that is
    exactly the failure that shipped.

    **Rolling back in a `TransactionCase`.** Odoo patches the test cursor's
    `commit` and `rollback` to raise, so a case cannot roll its own transaction
    back. The two shapes below stand in for the real one, and each says which it
    is: `test_a_rollback_…` discards the whole attempt before anything is sent
    (what a rollback means for a caller), and `test_a_delivery_that_died_…`
    lets the real create reach the mock and then discards only the local write
    (the remote half happened, the local half did not). Neither stubs the
    launch: the production code runs, and the mock is what answers.
    """

    def test_a_rollback_then_a_rerun_leaves_exactly_one_workstream(self):
        """Phase one armed, the attempt discarded, the handler re-runs.

        Under the old single-phase design the create had already reached the
        server by the time the caller rolled back, and the re-run — with a
        session row that no longer existed — asked for a *different* workstream
        id, so the proxy had nothing to answer 409 about and made a second
        workstream. The two assertions that catch it are the empty request log
        after the rollback and the single workstream at the end.
        """
        # The attempt, surrounded by the savepoint that will discard it. A
        # `rollback()` also clears `cr.postcommit`, which is why the armed job
        # is dropped here too: on a real rollback the hook could not fire,
        # because there is no commit for it to fire after.
        discarded = self.env.cr.savepoint()
        first = self.rule._launch_for_record(idempotency_key="evt-rollback")
        self.assertEqual(first.state, "queued",
                         "phase one leaves the run queued and sends nothing")
        discarded.rollback()
        self.env.cr.postcommit.clear()
        self.env.invalidate_all()

        self.assertEqual(_creates(), [],
                         "a rolled-back attempt must make no create call at all")
        self.assertEqual(_mock_workstreams(), [],
                         "and the server must hold no workstream for it")

        # The handler re-runs. Odoo does this up to five times on a serialisation
        # failure, and each re-run is a fresh attempt at the same event.
        run = self.rule._launch_for_record(idempotency_key="evt-rollback")
        self._deliver()

        self.assertEqual(run.state, "done", run.error)
        workstreams = _mock_workstreams()
        self.assertEqual(len(workstreams), 1,
                         "one workstream for the event at the server, not two")
        self.assertEqual(len(_creates()), 1,
                         "one create reached the server, not one per attempt")
        self.assertEqual(
            [m["content"] for m in workstreams[0]["messages"] if m["role"] == "user"],
            ["Summarise today's queue."],
            "and it carries the prompt exactly once",
        )

    def _post_then_die(self):
        """Make the real create land, then discard the local half of it.

        The real `_ensure_started()` runs — so the mock really receives the
        create and really makes the workstream — inside a savepoint that is
        rolled back, so Odoo's own `ws_id` write never reaches the database. The
        raise is what a dying transaction does: `_deliver_queued_run()` records
        it on the run instead of pretending the delivery happened.
        """
        session_model = type(self.env["turnstone.session"])
        original = session_model._ensure_started

        def post_then_die(session, first_message=None):
            with closing(self.env.cr.savepoint()) as half:
                original(session, first_message=first_message)
                half.rollback()
            raise RuntimeError("the delivery transaction died after the POST")

        return patch.object(session_model, "_ensure_started", post_then_die)

    def test_a_delivery_that_died_after_the_post_is_adopted_not_duplicated(self):
        """The shape the old design could not survive at all.

        The create reaches the mock, so the workstream exists; the transaction
        that carried it dies before `ws_id` is written back. The next delivery of
        the same event must present the **same** id — which it can only do
        because phase one committed the session — and adopt the 409. An id
        minted per attempt would make a second workstream, and the mock's request
        log would show a second 201 instead of the 409.
        """
        with self._post_then_die():
            died = self.rule._launch_for_record(idempotency_key="evt-after-post")
            self._deliver()

        workstreams = _mock_workstreams()
        self.assertEqual(len(workstreams), 1,
                         "the POST that succeeded really did create a workstream")
        remote_ws_id = workstreams[0]["id"]
        self.assertEqual(died.state, "failed", died.error)
        self.assertTrue(died.session_id, "the committed draft is what carries the id")
        self.assertFalse(died.session_id.ws_id,
                         "Odoo's write of the ws_id is the half that died")

        # The sender re-delivers the same event. A `failed` run is retried in
        # place — that is what `failed` means — and is never mistaken for a
        # duplicate, which would strand the event.
        retried = self.rule._launch_for_record(idempotency_key="evt-after-post")
        self._deliver()

        self.assertEqual(retried.id, died.id, "the retry reuses the run for the event")
        self.assertEqual(retried.state, "done", retried.error)
        self.assertEqual(retried.session_id.ws_id, remote_ws_id,
                         "the retry adopted the workstream that already existed")
        self.assertEqual(len(_mock_workstreams()), 1,
                         "one workstream for the event, not two")
        self.assertEqual([c["status"] for c in _creates()], [201, 409],
                         "two create attempts reached the server and the second was "
                         "answered 409 as a repeat. That 409 is the fix measured: a "
                         "fresh id would have been answered 201 and made a second "
                         "workstream beside the orphan.")

    def test_a_redelivery_while_the_run_is_still_queued_sends_nothing(self):
        """The new `queued` state must not read as "not handled yet".

        The split made a successful launch leave the run `queued`, which is the
        one state a dedupe path could plausibly mistake for "nothing has
        happened". Between phase one and the commit that delivers it, a second
        delivery of the same event arrives; it must be answered with the run
        that already exists and make no remote call.
        """
        in_flight = self.rule._launch_for_record(idempotency_key="evt-inflight")
        self.assertEqual(in_flight.state, "queued")

        again = self.rule._launch_for_record(idempotency_key="evt-inflight")

        self.assertEqual(again.id, in_flight.id,
                         "a queued run is in flight, not an event to launch again")
        self.assertEqual(_creates(), [],
                         "a re-delivery of an in-flight event sends nothing")

        # And the same answer once it has landed.
        self._deliver()
        self.assertEqual(in_flight.state, "done", in_flight.error)
        creates_after_delivery = len(_creates())
        after = self.rule._launch_for_record(idempotency_key="evt-inflight")
        self._deliver()

        self.assertEqual(after.id, in_flight.id)
        self.assertEqual(after.state, "done")
        self.assertEqual(len(_creates()), creates_after_delivery,
                         "a re-delivery of a delivered event sends nothing either")
        self.assertEqual(len(_mock_workstreams()), 1)

    def test_a_run_left_queued_by_a_dead_process_is_drained_not_duplicated(self):
        """The third shape the split creates, and the one the run worker owns.

        A process that dies between the commit and the hook leaves a `queued` run
        with a committed `draft` session behind it. The drain presents the same
        workstream id the hook would have, so the event stays one event.
        """
        run = self.rule._launch_for_record(idempotency_key="evt-drained")
        self.assertEqual(run.state, "queued")
        self.assertFalse(run.session_id.ws_id)
        # The process that armed the hook is gone, and the armed job with it. In
        # the real case the run's row was committed, so only the job is lost.
        self.env.cr.postcommit.data.pop("turnstone_integration.launch.jobs", None)
        self.assertEqual(_creates(), [], "the dead process sent nothing")

        drained = self.env["turnstone.automation.run"].sudo()._drain_queue()
        self.assertIn(run, drained, "the drain must claim the queued run")
        # The drain arms the delivery the same way a launch does — one code path,
        # `_attempt_launch()` — so the delivery is still phase two's job.
        self._deliver()

        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(len(_creates()), 1)
        self.assertEqual(len(_mock_workstreams()), 1,
                         "the drain delivered that workstream, not a second one")
        again = self.rule._launch_for_record(idempotency_key="evt-drained")
        self.assertEqual(again.id, run.id)
        self.assertEqual(len(_creates()), 1,
                         "and a later delivery of the event sends nothing")
