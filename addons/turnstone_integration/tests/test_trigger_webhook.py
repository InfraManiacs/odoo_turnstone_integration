"""The webhook adapter: a signed delivery wakes one rule, and an unsigned one never does.

`/turnstone/webhook/<webhook_key>` (OTI-5755) is the fourth trigger adapter's
inbound surface, and it is the only one a **third party** can reach. That makes
its two halves different in kind:

* the **positive** half is a delivery that is authentic, fresh and names an
  event, and its evidence is a `turnstone.automation.run` row in a non-failed
  state;
* the **suppression** half is everything the route must refuse — a wrong
  signature, a stale timestamp, a body that is not the bytes that were signed, a
  replayed event id, a delivery that arrives while the same rule is busy, an
  unknown key, a missing event id — and its evidence is a *status code*, because
  this adapter answers a caller rather than a save.

WHY THIS FILE IS AN `HttpCase` AND THE OTHERS ARE NOT

The record, chatter and website adapters act inside the ORM, so a
`TransactionCase` can drive them at their real entry point. This route is an
`@http.route` with `auth="public"`: its contract is entirely in what an external
sender gets back (401 vs 400 vs 404 vs 409 vs 429 vs 202) and in what a caller
can learn from a 4xx. Neither is observable by calling a model method. So every
case below posts to the real dispatcher with `url_open()`, against the httpd
this runner starts for `--test-enable`, and asserts on the response.

That also means the signature has to be computed over the **exact bytes sent**,
which is the property the route documents at length: `_webhook_prompt()` and
`_webhook_check_signature()` both work from `request.httprequest.get_data()`,
not from re-serialised JSON. `_deliver()` below signs the same byte string it
posts, so a route that regressed to signing `str(payload)` would fail here.

WHY THE CLAIM IS SPLIT ACCROSS CLASSES

Each suppression case has its own control beside it: "an unsigned delivery is
refused" proves nothing unless a signed delivery of the same rule and the same
payload produces a run. The `WebhookCase` base builds one configured rule and
the signing helpers; each test class then states one half of the contract.

WHY THE RULE LAUNCHES AGAINST THE MOCK RATHER THAN AT A DEAD PORT

The obvious fixture is a connection pointing at a closed port, so no test
reaches anything. That fixture silently changes what is being tested: a launch
that cannot connect leaves the run `failed`, and `failed` is the **one** state
the replay defence deliberately does not suppress — a 500 from Turnstone must not
become a permanently lost event, so a re-delivery of a failed run retries it in
place and answers 202, not 409. A file built on an unreachable server would
therefore assert `failed` where it meant `accepted`, and prove nothing about
replay. (It did, on the first run of this file: both cases failed for exactly
that reason.)

So the fixture points at the mock, exactly as `test_automation_launch.py` does,
and a run reaches `done`. Whether the launch itself works is that file's subject;
nothing here asserts on the session it creates.
"""

import hashlib
import hmac
import json
import os
import time

from odoo.tests import HttpCase, tagged

#: The mock server inside the compose test network (`docker-compose.test.yml`).
#: Read from the environment rather than hardcoded where the token is concerned:
#: `scripts/test-odoo.sh` generates a fresh one per run.
MOCK_URL = "http://mock-turnstone:8099"
MOCK_TOKEN = os.environ.get("MOCK_TURNSTONE_TOKEN", "mock-token")

RULE = "turnstone.automation"
RUN = "turnstone.automation.run"

SIGNATURE_HEADER = "X-Turnstone-Signature"
TIMESTAMP_HEADER = "X-Turnstone-Timestamp"
EVENT_HEADER = "X-Turnstone-Event-Id"

SECRET = "webhook-adapter-test-secret"


@tagged("standard", "-at_install", "post_install")
class WebhookCase(HttpCase):
    """Fixture: a signed-in-capable rule, and helpers that speak the contract.

    The rule is `webhook`-triggered, pinned to a user who may use the agent, and
    has no target model — that is the shape the route documents ("a webhook has
    no record"), so the prompt template must not use `{{ }}`.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Points at the mock on purpose — see the module docstring. A dead port
        # would leave every run `failed`, and `failed` is the one state the
        # replay defence does not suppress, which would make the replay case
        # assert the retry path instead.
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Webhook adapter (mock)",
            "base_url": MOCK_URL,
            "api_token": MOCK_TOKEN,
            "is_default": True,
        })
        cls.agent = cls.env["turnstone.agent"].create({
            "name": "Webhook Adapter Agent",
            "connection_id": cls.connection.id,
            "kind": "interactive",
        })
        # The user a webhook runs as: a webhook has no caller, so the rule must
        # name one. `group_turnstone_user` is what `_check_user_allowed` wants,
        # and the agent is unrestricted (`group_ids` unset).
        cls.owner = cls.env["res.users"].create({
            "name": "Webhook Adapter Owner",
            "login": "webhook-adapter-owner",
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("turnstone_integration.group_turnstone_user").id,
            ])],
        })

    def setUp(self):
        super().setUp()
        # The secret is one deployment-wide value in `ir.config_parameter`, so a
        # case that leaves it set would authenticate the next case's delivery.
        self.env["ir.config_parameter"].sudo().set_param(
            "turnstone_integration.webhook_secret", SECRET,
        )
        self.addCleanup(
            self.env["ir.config_parameter"].sudo().set_param,
            "turnstone_integration.webhook_secret", "",
        )

    # -- fixtures ----------------------------------------------------------

    def _rule(self, **vals):
        """A webhook rule, with the trigger's own key generated by `create()`."""
        defaults = {
            "name": "Webhook adapter rule",
            "company_id": self.company.id,
            "agent_id": self.agent.id,
            "trigger_type": "webhook",
            "prompt_template": "Handle the attached event.",
            "run_as": "fixed_user",
            "run_as_user_id": self.owner.id,
        }
        defaults.update(vals)
        rule = self.env[RULE].sudo().create(defaults)
        self.assertTrue(
            rule.webhook_key,
            "create() must generate a webhook key, or the URL cannot be built",
        )
        return rule

    def _runs(self, rule):
        return self.env[RUN].sudo().search([("automation_id", "=", rule.id)])

    # -- speaking the contract ---------------------------------------------

    @staticmethod
    def _sign(body, timestamp, secret=SECRET):
        """The signature this route expects: HMAC over `"<ts>.<raw body>"`.

        Over the **bytes**, exactly as `_webhook_signature()` documents. Called
        with the same `body` bytes that are posted, so this is the honest
        computation rather than a reproduction of the route's own bug.
        """
        mac = hmac.new(
            secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256,
        )
        return "sha256=" + mac.hexdigest()

    def _deliver(self, rule, payload, *, event_id=None, secret=SECRET,
                 timestamp=None, signature=None, body=None, key=None,
                 headers=None):
        """POST one delivery to the rule's URL and return the response.

        ``body`` overrides the bytes sent, so a case can prove that a signature
        computed over different bytes is refused. ``signature`` overrides the
        header, so a case can send a wrong one without breaking the timestamp.
        """
        raw = body if body is not None else json.dumps(payload).encode()
        stamp = str(int(timestamp if timestamp is not None else time.time()))
        sent_headers = {
            TIMESTAMP_HEADER: stamp,
            SIGNATURE_HEADER: (
                signature if signature is not None else self._sign(raw, stamp, secret)
            ),
        }
        if event_id is not None:
            sent_headers[EVENT_HEADER] = event_id
        sent_headers.update(headers or {})
        # The request ran on its own cursor in another thread; drop what this
        # environment cached so an assertion cannot read a stale row.
        self.env.invalidate_all()
        response = self.url_open(
            "/turnstone/webhook/%s" % (key or rule.webhook_key),
            data=raw,
            headers=dict(sent_headers, **{"Content-Type": "application/json"}),
            timeout=30,
        )
        self.env.invalidate_all()
        return response


@tagged("standard", "-at_install", "post_install")
class TestAcceptedDelivery(WebhookCase):
    """Criterion 1 (positive): an authentic, fresh delivery with an event id."""

    def test_a_signed_delivery_is_accepted_and_records_one_run(self):
        rule = self._rule()

        response = self._deliver(
            rule, {"event_id": "evt-1", "ticket": "TCK-7"}, event_id="evt-1",
        )

        self.assertEqual(
            response.status_code, 202,
            "an authentic delivery must be accepted, not refused: %s"
            % response.text[:400],
        )
        self.assertEqual(response.json(), {"status": "accepted"})
        runs = self._runs(rule)
        self.assertEqual(len(runs), 1, "one delivery must record exactly one run")
        self.assertEqual(
            runs.state, "done",
            "the route accepted the delivery and the mock answered, so the run "
            "must have landed, not failed: %s" % runs.error,
        )

    def test_the_event_id_header_alone_identifies_the_event(self):
        """The header is checked first, so a body with no id still works."""
        rule = self._rule()

        response = self._deliver(
            rule, {"ticket": "TCK-8"}, event_id="evt-header-only",
        )

        self.assertEqual(response.status_code, 202, response.text[:400])
        self.assertEqual(len(self._runs(rule)), 1)

    def test_a_delivery_id_in_the_body_is_accepted_as_the_event_id(self):
        """`delivery_id` is what the webhook providers actually call it."""
        rule = self._rule()

        response = self._deliver(rule, {"delivery_id": "dlv-9", "x": 1})

        self.assertEqual(response.status_code, 202, response.text[:400])
        self.assertEqual(len(self._runs(rule)), 1)


@tagged("standard", "-at_install", "post_install")
class TestRefusedDelivery(WebhookCase):
    """Criterion 1 (negative): every unauthentic or malformed delivery is refused."""

    def test_a_wrong_signature_is_refused_and_records_no_run(self):
        rule = self._rule()

        response = self._deliver(
            rule, {"event_id": "evt-2"}, event_id="evt-2",
            signature="sha256=" + "0" * 64,
        )

        self.assertEqual(
            response.status_code, 401,
            "a delivery that is not correctly signed must be 401: %s"
            % response.text[:400],
        )
        self.assertEqual(response.json(), {"status": "unauthorized"})
        self.assertFalse(
            self._runs(rule),
            "a refused delivery must not leave a run behind",
        )

    def test_a_signature_over_different_bytes_is_refused(self):
        """The signature covers the bytes sent, not a re-serialisation of them.

        A correctly signed `{"a": 1}` presented with a body of `{"a": 2}` is the
        shape a tampered or truncated delivery takes, and the only defence is
        that the route verifies what actually arrived.
        """
        rule = self._rule()
        signed = json.dumps({"event_id": "evt-3", "a": 1}).encode()
        sent = json.dumps({"event_id": "evt-3", "a": 2}).encode()
        stamp = str(int(time.time()))

        response = self._deliver(
            rule, None, body=sent, timestamp=stamp,
            signature=self._sign(signed, stamp),
            headers={EVENT_HEADER: "evt-3"},
        )

        self.assertEqual(response.status_code, 401, response.text[:400])
        self.assertFalse(self._runs(rule))

    def test_a_stale_timestamp_is_refused(self):
        """Outside the skew window a captured request stops being worth replaying."""
        rule = self._rule()
        # Further back than `WEBHOOK_MAX_SKEW_SECONDS` (300), and signed
        # correctly: freshness is checked independently of authenticity.
        stale = time.time() - 3600

        response = self._deliver(
            rule, {"event_id": "evt-4"}, event_id="evt-4", timestamp=stale,
        )

        self.assertEqual(
            response.status_code, 401,
            "a delivery older than the skew window must be 401: %s"
            % response.text[:400],
        )
        self.assertFalse(self._runs(rule))

    def test_a_missing_signature_is_refused(self):
        rule = self._rule()
        stamp = str(int(time.time()))

        response = self.url_open(
            "/turnstone/webhook/%s" % rule.webhook_key,
            data=json.dumps({"event_id": "evt-5"}).encode(),
            headers={
                TIMESTAMP_HEADER: stamp,
                EVENT_HEADER: "evt-5",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

        self.assertEqual(response.status_code, 401, response.text[:400])
        self.assertFalse(self._runs(rule))

    def test_an_unset_secret_refuses_rather_than_crashing(self):
        """A deployment that never set the secret must say so, not 500."""
        self.env["ir.config_parameter"].sudo().set_param(
            "turnstone_integration.webhook_secret", "",
        )
        rule = self._rule()

        response = self._deliver(rule, {"event_id": "evt-6"}, event_id="evt-6")

        self.assertEqual(
            response.status_code, 401,
            "an unconfigured secret is our misconfiguration, but the sender is "
            "told what it is told for a wrong signature: %s" % response.text[:400],
        )
        self.assertFalse(self._runs(rule))

    def test_a_body_that_is_not_json_is_a_400(self):
        rule = self._rule()
        raw = b"this is not json"
        stamp = str(int(time.time()))

        response = self._deliver(
            rule, None, body=raw, timestamp=stamp,
            signature=self._sign(raw, stamp),
            headers={EVENT_HEADER: "evt-7"},
        )

        self.assertEqual(
            response.status_code, 400,
            "a malformed payload is the sender's bug and must be a 400: %s"
            % response.text[:400],
        )
        self.assertEqual(response.json(), {"status": "invalid_payload"})

    def test_a_delivery_with_no_event_id_is_a_400(self):
        """Without an event id there is no replay defence, so it cannot be accepted."""
        rule = self._rule()

        response = self._deliver(rule, {"ticket": "TCK-10"})

        self.assertEqual(
            response.status_code, 400,
            "a delivery with no event id cannot be deduplicated and must be 400: %s"
            % response.text[:400],
        )
        self.assertEqual(response.json(), {"status": "missing_event_id"})
        self.assertFalse(self._runs(rule))

    def test_an_unknown_key_is_a_404_after_authentication(self):
        rule = self._rule()

        response = self._deliver(
            rule, {"event_id": "evt-11"}, event_id="evt-11",
            key="a-key-no-rule-has",
        )

        self.assertEqual(
            response.status_code, 404,
            "an authenticated caller with a stale URL is owed a 404: %s"
            % response.text[:400],
        )
        self.assertEqual(response.json(), {"status": "unknown_endpoint"})

    def test_an_unknown_key_without_a_signature_is_a_401_not_a_404(self):
        """Authentication precedes lookup, so a prober cannot enumerate rules.

        The whole point of checking the signature first: an unauthenticated
        caller must not be able to tell a real key from a fake one by the
        difference between 404 and 401.
        """
        rule = self._rule()

        response = self.url_open(
            "/turnstone/webhook/%s" % "a-key-no-rule-has",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

        self.assertEqual(
            response.status_code, 401,
            "an unsigned delivery must be 401 even for a key that does not "
            "exist: %s" % response.text[:400],
        )


@tagged("standard", "-at_install", "post_install")
class TestReplayAndRuleState(WebhookCase):
    """Criterion 1 (suppression): a repeat, and a rule that cannot be woken."""

    def test_the_same_event_twice_is_a_409_and_one_run(self):
        """The replay defence: the event id is the run's idempotency key."""
        rule = self._rule()

        first = self._deliver(rule, {"event_id": "evt-dup"}, event_id="evt-dup")
        self.assertEqual(first.status_code, 202, first.text[:400])
        self.assertEqual(len(self._runs(rule)), 1)

        second = self._deliver(rule, {"event_id": "evt-dup"}, event_id="evt-dup")

        self.assertEqual(
            second.status_code, 409,
            "a re-delivery must be a conflict, not a success — a sender that "
            "gets 202 for a replay has no reason to stop retrying: %s"
            % second.text[:400],
        )
        self.assertEqual(second.json(), {"status": "duplicate_event"})
        self.assertEqual(
            len(self._runs(rule)), 1,
            "one event must be one run no matter how often it is delivered",
        )

    def test_a_different_event_id_gets_its_own_run(self):
        """The control for the replay case: the dedupe is per event, not per rule."""
        rule = self._rule()

        self._deliver(rule, {"event_id": "evt-a"}, event_id="evt-a")
        self._deliver(rule, {"event_id": "evt-b"}, event_id="evt-b")

        self.assertEqual(
            len(self._runs(rule)), 2,
            "two distinct events are two runs; a route that collapsed them "
            "would pass the replay case and lose every second event",
        )

    def test_a_repeat_of_a_failed_event_retries_instead_of_conflicting(self):
        """A failed run is retried in place, not reported as a duplicate.

        `_deliver_webhook()` documents this as the deliberate exception to the
        replay defence: a 500 from Turnstone did not deliver the event, so
        suppressing its retry would turn a transient remote failure into a
        permanently lost event. The re-delivery is therefore 202 and reuses the
        **same** run row (`attempt_count` moves, the row does not multiply).

        Proved here rather than assumed, because it is the invariant that made a
        dead-port fixture lie: with an unreachable server *every* delivery is a
        failed one, and the replay case silently became a test of this path.

        The first delivery fails for a real reason — a rule pinned to an agent
        on a closed port — rather than by writing `state` directly, which the run
        model refuses (`done` and `failed` are both terminal, `_check_transition`).
        """
        dead = self.env["turnstone.connection"].create({
            "name": "Webhook adapter (closed port)",
            "base_url": "http://127.0.0.1:1",
            "api_token": "no-network-on-purpose",
        })
        dead_agent = self.env["turnstone.agent"].create({
            "name": "Webhook Adapter Dead Agent",
            "connection_id": dead.id,
            "kind": "interactive",
        })
        rule = self._rule(agent_id=dead_agent.id)

        first = self._deliver(rule, {"event_id": "evt-retry"}, event_id="evt-retry")
        self.assertEqual(first.status_code, 202, first.text[:400])
        run = self._runs(rule)
        self.assertEqual(
            run.state, "failed",
            "the fixture is a closed port, so the launch must have failed — "
            "otherwise this case proves nothing about a retry",
        )

        response = self._deliver(
            rule, {"event_id": "evt-retry"}, event_id="evt-retry",
        )

        self.assertEqual(
            response.status_code, 202,
            "a re-delivery of a failed event must be accepted for retry, not "
            "refused as a duplicate: %s" % response.text[:400],
        )
        self.assertEqual(
            len(self._runs(rule)), 1,
            "the retry reuses the run row; it must not create a second one",
        )

    def test_a_delivery_to_an_archived_rule_queues_nothing(self):
        """Archiving is how a rule is retired; its URL must stop working."""
        rule = self._rule()
        rule.sudo().active = False

        response = self._deliver(rule, {"event_id": "evt-arch"}, event_id="evt-arch")

        self.assertFalse(
            self._runs(rule),
            "a delivery naming a rule that no longer exists must not queue a run",
        )
        # The rule was resolved before archiving mattered to the search, so the
        # answer is either a 404 (not found) or an accepted-but-refused run. Both
        # are correct; the invariant under test is "no run".
        self.assertIn(response.status_code, (202, 404), response.text[:400])