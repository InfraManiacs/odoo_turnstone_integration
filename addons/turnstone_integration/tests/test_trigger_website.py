"""The website adapter: a form submission may fire a rule, and consent may stop it.

Three criteria, and the first one pulls against the other two:

1. **The module installs and its tests pass with `website` absent.** Not a
   formality: this file is imported and its model registered on every install,
   so a stray `import` of anything website-shaped would make the whole addon
   uninstallable on the many databases that have no public site. The evidence is
   a run of this module on a database where `website` was never installed
   (`website` is not a dependency, so it is absent unless somebody asks for it).
2. **With `website` present, a form submission firing a matching automation
   creates one run.** Driven through the form controller's own `insert_record()`
   inside a `MockRequest`, so the record is written by the same code that serves
   the public form, in a request that carries `website_id` the way
   `ir_http._frontend_pre_dispatch` sets it. **One** run, not two: the whole
   reason this adapter extends `turnstone.trigger.record` instead of adding a
   second hook is that a second hook would double every submission.
3. **Consent-excluded records create no run.** A submitter whose address is on
   `mail.blacklist`, and a submission carrying no address at all. Each has its
   own control, because "nothing happened" is the weakest form of evidence: the
   same submission with a consenting address must create a run, or a gate that
   refused everything would pass this criterion and be useless.

WHY THE WEBSITE CASES SKIP RATHER THAN INSTALL `website`

Installing a module inside a test reloads the registry underneath a running
transaction, and the environment the test holds stops being the registry's. So
the website-present cases run only when the module happens to be installed, and
say so in the log when it is not. The run that proves criteria 2 and 3 is
recorded on the ticket: it is a database where `website` **is** installed.

WHERE THE FORM WRITE HAPPENS

`MockRequest` (from `odoo.addons.http_routing.tests.common`, which core's own
website tests use) binds a fake `http.request` whose `type` is `'http'`, and its
`context={'website_id': ...}` argument puts the marker in the environment the
record is written with — the two signals `_submitting_website()` requires, from
the same places the real request handler puts them. `MockRequest` also patches
`odoo.http._request_stack`, so the adapter's `from odoo.http import request`
resolves to it exactly as it would under a real dispatch.

WHY EVERY CLASS IS TAGGED `post_install` WITH `-at_install`

`TransactionCase` carries `{'standard', 'at_install'}`, and `@tagged` *adds* to
that set, so a class that is only tagged `post_install` keeps `at_install` and
runs twice. `-at_install` is what removes it (`testing/odoo-test-runner-
behaviour.md`, and the same reason `test_automation_ui.py` does it). It matters
more here than usual: `at_install` tests run while the registry is still being
loaded, and on the database that installs `website` in the same run, that is
*before* `website` is loaded — `"website" in env` would be False and every case
below would skip. `post_install` is what puts them after the last module.
"""

import logging

from odoo.tests import TransactionCase, tagged

WEBSITE = "website"
PARTNER = "res.partner"
RULE = "turnstone.automation"
RUN = "turnstone.automation.run"
TRIGGER = "turnstone.trigger.record"

_logger = logging.getLogger(__name__)


class WebsiteTriggerCase(TransactionCase):
    """Fixtures: a rule that watches partners, and helpers to submit a form.

    No mock Turnstone server: this adapter's whole job is whether a run row is
    written, and #5752's own suite already covers the launch that follows. A
    `TurnstoneTransactionCase` here would add a network health check to a claim
    that makes no request.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # A connection and agent no test reaches: every run must stay `queued`.
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Website adapter (no network)",
            "base_url": "http://127.0.0.1:1",
            "api_token": "irrelevant-this-ticket-makes-no-calls",
            "is_default": True,
        })
        cls.agent = cls.env["turnstone.agent"].create({
            "name": "Website Adapter Agent",
            "connection_id": cls.connection.id,
            "kind": "interactive",
        })
        cls.partner_model = cls.env["ir.model"]._get(PARTNER)

    def setUp(self):
        super().setUp()
        # `TransactionCase` runs every test in ONE transaction, so `cr.cache`
        # survives from test to test — production's does not. Clear the adapter's
        # bookkeeping so "this transaction's budget" means one test, and rebuild
        # the registry's watched set, because a rule from a previous test is gone
        # when that test's savepoint rolled back but the Python attribute is not.
        self.env[TRIGGER]._forget_transaction_state()
        self.env[TRIGGER]._refresh_watch_list()

    # -- fixtures ----------------------------------------------------------

    def _rule(self, **vals):
        suffix = vals.pop("suffix", "a")
        defaults = {
            "name": "Website adapter rule %s" % suffix,
            "company_id": self.company.id,
            "agent_id": self.agent.id,
            "trigger_type": "record_create",
            "target_model_id": self.partner_model.id,
            "prompt_template": "Greet {{ name }}.",
            "run_as": "fixed_user",
            "run_as_user_id": self.env.user.id,
        }
        defaults.update(vals)
        rule = self.env[RULE].create(defaults)
        self.env[TRIGGER]._refresh_watch_list()
        return rule

    def _runs(self, rule=None):
        domain = [("automation_id", "=", rule.id)] if rule else []
        return self.env[RUN].sudo().search(domain)

    def _blacklist(self, email):
        """Withhold marketing consent for `email`, the way an unsubscribe does."""
        return self.env["mail.blacklist"].sudo().create({"email": email})


@tagged("post_install", "-at_install")
class TestWithoutWebsite(WebsiteTriggerCase):
    """Criterion 1: the addon is fully functional with the website module absent.

    The mirror of `TestWithWebsite`: each class asserts its own premise and
    skips on the database where the premise does not hold, so the suite is green
    on both and neither can pass by accident on the wrong one. Every assertion
    here runs on a database that has never installed `website` (it is not a
    dependency, and `scripts/test-odoo.sh` installs only this addon). If any of
    them fails there, the addon does not install on a database without a public
    site — which is most of them.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if WEBSITE in cls.env:
            cls.skipTest(
                cls,
                "website is installed in this database, so this class's premise "
                "(a site-less database) does not hold; TestWithWebsite covers "
                "the same code with the module present",
            )
            return

    def test_the_adapter_model_loaded(self):
        """The file is imported and its model is in the registry, nothing more."""
        self.assertIn(
            TRIGGER, self.env,
            "importing turnstone_trigger_website must not remove or replace the "
            "record adapter: it is an in-place extension of it",
        )
        self.assertTrue(
            hasattr(self.env[TRIGGER], "_admit"),
            "the website adapter's gate must be installed even with website "
            "absent, or its absence would only be discovered by a form post on "
            "a database that has a site",
        )

    def test_the_module_is_absent(self):
        """The premise of this class, asserted rather than assumed."""
        self.assertNotIn(
            WEBSITE, self.env,
            "this class proves behaviour without the website module; if it is "
            "installed here, see the TestWithWebsite cases",
        )

    def test_the_gate_is_inert_without_the_module(self):
        """`_admit()` returns exactly what it was given, whatever the email."""
        record = self.env[PARTNER].create({
            "name": "Blacklisted But Backend", "email": "unsubscribed@example.com",
        })
        self._blacklist("unsubscribed@example.com")

        adapter = self.env[TRIGGER]
        self.assertFalse(
            adapter._submitting_website(),
            "with no website module there is no website request to recognise",
        )
        self.assertEqual(
            adapter._admit(record), record,
            "the gate must be a no-op without website: a form-submission check "
            "that refused backend records would be an outage, not a feature",
        )

    def test_a_backend_partner_create_still_queues_its_run(self):
        """The record adapter's behaviour is unchanged on a site-less database."""
        rule = self._rule()
        self.env[PARTNER].create({
            "name": "Ada Lovelace", "email": "ada@example.com",
        })
        runs = self._runs(rule)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs.state, "queued")


@tagged("post_install", "-at_install")
class TestTheGateDoesNotLeakOntoBackendSaves(WebsiteTriggerCase):
    """A blacklisted address must not silence an employee's own work.

    Marketing consent is a condition on strangers sending forms, not on a clerk
    editing a contact they can already open. This class is the control for
    criterion 3: the same blacklist entry that refuses a submission in
    `TestWithWebsite` is asserted to have no effect on a backend save.
    """

    def test_a_backend_create_of_a_blacklisted_contact_still_queues(self):
        rule = self._rule()
        self._blacklist("unsubscribed@example.com")

        self.env[PARTNER].create({
            "name": "Unsubscribed But A Customer",
            "email": "unsubscribed@example.com",
        })

        self.assertEqual(
            len(self._runs(rule)), 1,
            "a backend save has no website request and no website_id; consent "
            "must not be applied to it",
        )

    def test_a_live_request_without_a_website_id_is_not_a_submission(self):
        """`website_id` is the marker; `type='http'` alone must not be enough.

        Runs on a database without `website` too, where the module check alone
        refuses — so the case that actually matters (website present, no marker)
        is asserted in `TestWithWebsite` instead.
        """
        from unittest.mock import MagicMock, patch

        from odoo.addons.turnstone_integration.models import (
            turnstone_trigger_website as adapter_module,
        )

        adapter = self.env[TRIGGER]
        with patch.object(
            adapter_module, "request", MagicMock(type="http"),
            create=True,
        ):
            self.assertFalse(
                adapter._submitting_website(),
                "a frontend request without website_id in the writing context "
                "is not a website form submission",
            )


@tagged("post_install", "-at_install")
class TestWithWebsite(WebsiteTriggerCase):
    """Criteria 2 and 3, on a database where the website module is installed.

    Skips (with a message in the log, not silently) when `website` is absent —
    see the module docstring for why installing it inside a test is not an
    option. The run that proves these cases is recorded on the ticket.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if WEBSITE not in cls.env:
            # Odoo's `skipTest` is a classmethod taking (cls, reason) — and the
            # rest of `setUpClass` must NOT run on a database without the
            # module, so this raises (a `raise` after it is unreachable).
            cls.skipTest(
                cls,
                "website is not installed in this database; criteria 2 and 3 are "
                "proven by the run recorded on OTI-5756 that installs it",
            )
            return
        cls.website = cls.env[WEBSITE].search([], limit=1)
        if not cls.website:
            cls.website = cls.env[WEBSITE].create({
                "name": "Turnstone form site",
                "company_id": cls.company.id,
            })
        # The form builder's own whitelist: no field of `res.partner` is
        # form-writable until an administrator opts it in. Same call the form
        # builder's UI makes.
        cls.env["ir.model.fields"].sudo().formbuilder_whitelist(
            PARTNER, ["name", "email"],
        )

    def _submit_partner(self, **values):
        """Write a partner through the website form controller's own insert path.

        Returns the created record. The call goes through
        `WebsiteForm.insert_record()`, i.e. the exact method
        `_handle_website_form()` uses after parsing a public form post — so the
        context the record is created with (superuser, `mail_create_nosubscribe`,
        `website_id`) is the real one, not a reconstruction.
        """
        from odoo.addons.http_routing.tests.common import MockRequest
        from odoo.addons.website.controllers.form import WebsiteForm

        values.setdefault("name", "Website Submitter")
        # `insert_record` takes the `ir.model` record (`model_sudo.model` is what
        # it names the target with), and it must be `sudo()` — it refuses
        # otherwise, exactly as the controller requires.
        model_record = self.env["ir.model"].sudo()._get(PARTNER)
        with MockRequest(
            self.env, path="/website/form/%s" % PARTNER, website=self.website,
            context={"website_id": self.website.id},
        ) as mock:
            record_id = WebsiteForm().insert_record(
                mock, model_record, dict(values), custom="", meta=None,
            )
        return self.env[PARTNER].browse(record_id)

    # -- the marker itself -------------------------------------------------

    def test_a_website_request_carrying_website_id_is_recognised(self):
        """The origin test, on the request shape the form post actually has."""
        from odoo.addons.http_routing.tests.common import MockRequest

        adapter = self.env[TRIGGER]
        with MockRequest(
            self.env, path="/website/form/%s" % PARTNER, website=self.website,
            context={"website_id": self.website.id},
        ):
            recognised = self.env[TRIGGER].with_context(
                website_id=self.website.id,
            )._submitting_website()
        self.assertEqual(
            recognised, self.website,
            "a frontend request carrying website_id is a website form submission",
        )

    def test_a_frontend_request_without_the_marker_is_not_a_submission(self):
        """Website installed, live request, no `website_id`: a backend call."""
        from unittest.mock import MagicMock, patch

        from odoo.addons.turnstone_integration.models import (
            turnstone_trigger_website as adapter_module,
        )

        adapter = self.env[TRIGGER]
        with patch.object(
            adapter_module, "request", MagicMock(type="http"), create=True,
        ):
            self.assertFalse(
                adapter._submitting_website(),
                "without website_id there is no website to attribute the write "
                "to, so the gate must stay out of the way",
            )

    # -- criterion 2 -------------------------------------------------------

    def test_a_form_submission_creates_exactly_one_run(self):
        """One submission, one run — the point of extending rather than hooking."""
        rule = self._rule()

        partner = self._submit_partner(
            name="Form Submitter", email="submitter@example.com",
        )

        self.assertTrue(partner.exists(), "the form must have created the record")
        runs = self._runs(rule)
        self.assertEqual(
            len(runs), 1,
            "one submission is one event: a second website hook would have "
            "queued a second, differently-keyed run for the same record",
        )
        self.assertEqual(runs.state, "queued")
        self.assertEqual(runs.trigger_res_model, PARTNER)
        self.assertEqual(runs.trigger_res_id, partner.id)
        self.assertEqual(runs.attempt_count, 0, "the hook must not launch")
        self.assertFalse(runs.session_id, "the hook must not create a session")

    def test_the_run_key_is_still_the_record(self):
        """Nothing website-specific in the key, so a redelivery still collapses."""
        rule = self._rule()
        partner = self._submit_partner(email="dup@example.com")
        self.assertEqual(len(self._runs(rule)), 1)

        # The same event handed in again — what a retried form post looks like
        # once the record exists.
        self.env[TRIGGER]._on_create(partner)
        self.assertEqual(
            len(self._runs(rule)), 1,
            "the idempotency key must still be create:<model>,<id>; a "
            "website-specific key would let one submission queue twice",
        )

    def test_a_domain_excluding_form_target_creates_no_run(self):
        """The domain wall still applies to a submission."""
        rule = self._rule(target_domain="[('name', '=', 'Only This Name')]")
        self._submit_partner(name="Somebody Else", email="else@example.com")
        self.assertEqual(len(self._runs(rule)), 0)

    # -- criterion 3 -------------------------------------------------------

    def test_an_unsubscribed_submitter_creates_no_run(self):
        rule = self._rule()
        self._blacklist("unsubscribed@example.com")

        partner = self._submit_partner(
            name="Unsubscribed", email="unsubscribed@example.com",
        )

        self.assertTrue(
            partner.exists(),
            "consent stops the agent, not the form: the record the submitter "
            "sent must still exist, or this gate would lose enquiries",
        )
        self.assertEqual(
            len(self._runs(rule)), 0,
            "an address on mail.blacklist has withheld marketing consent and "
            "must create no run",
        )

    def test_a_submission_with_no_address_creates_no_run(self):
        rule = self._rule()
        self._submit_partner(name="No Email Given")
        self.assertEqual(
            len(self._runs(rule)), 0,
            "consent cannot be established for someone who cannot be "
            "identified; the honest default is not to act",
        )

    def test_a_consenting_submitter_creates_one_run(self):
        """The control for both refusals, with the same blacklist entry live."""
        rule = self._rule()
        self._blacklist("unsubscribed@example.com")

        self._submit_partner(name="Consenting", email="consenting@example.com")

        self.assertEqual(
            len(self._runs(rule)), 1,
            "a gate that refused every submission would pass the two cases "
            "above and be useless; the same form with a consenting address "
            "must still fire",
        )

    def test_a_blacklisted_backend_save_is_still_allowed(self):
        """The leak control, again, on the database where the module exists."""
        rule = self._rule()
        self._blacklist("unsubscribed@example.com")

        self.env[PARTNER].create({
            "name": "Backend Blacklisted", "email": "unsubscribed@example.com",
        })
        self.assertEqual(
            len(self._runs(rule)), 1,
            "with website installed the gate must still tell a submission from "
            "a backend save",
        )

    def test_a_written_submission_is_gated_too(self):
        """A form that updates an existing record is the same stranger.

        `website/form.py` only ever creates, but a custom form handler may
        browse-and-write, and a gate on creates alone would have a door beside
        it. Driven as a real write from an environment carrying `website_id`,
        which is the path `_dispatch` hands to `_on_write()`.
        """
        from odoo.addons.http_routing.tests.common import MockRequest

        rule = self._rule(trigger_type="record_write", suffix="write")
        partner = self.env[PARTNER].create({
            "name": "Existing Contact", "email": "unsubscribed@example.com",
        })
        self.assertEqual(
            len(self._runs(rule)), 0,
            "a create must not fire a rule that asked only about updates",
        )
        self._blacklist("unsubscribed@example.com")

        def write_from_a_form(values):
            with MockRequest(
                self.env, path="/website/form/%s" % PARTNER,
                website=self.website,
                context={"website_id": self.website.id},
            ):
                partner.with_context(website_id=self.website.id).write(values)

        write_from_a_form({"name": "Written By A Form"})
        self.assertEqual(
            len(self._runs(rule)), 0,
            "the consent gate must cover writes as well as creates",
        )

        # The control: the same write, from the same kind of request, for a
        # submitter who has consented. Without it the case above would also pass
        # if writes never fired a rule at all.
        consenter = self.env[PARTNER].create({
            "name": "Consenting Contact", "email": "consenting@example.com",
        })
        with MockRequest(
            self.env, path="/website/form/%s" % PARTNER, website=self.website,
            context={"website_id": self.website.id},
        ):
            consenter.with_context(website_id=self.website.id).write({
                "name": "Written By A Form, Consenting",
            })
        self.assertEqual(
            len(self._runs(rule)), 1,
            "a consenting submission's write must still fire the rule",
        )
