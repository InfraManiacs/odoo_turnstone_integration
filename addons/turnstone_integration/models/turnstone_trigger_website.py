"""The website adapter: a form submission on the public site may want a rule to run.

WHY THIS FILE EXISTS — AND WHY IT IS NOT A SECOND HOOK

`turnstone.trigger.record` (#5752) already notices every `create` and every
`write` in the database, so a contact form that creates a `res.partner` or a
`crm.lead` **already** queues a run today. A second ORM hook that also fired on
those same records would queue a *second*, differently-keyed run for one form
submission — two agent sessions for one enquiry, and two runs in the history
that look like two events. That is worse than having no website adapter at all.

So this file adds no hook. It **extends `turnstone.trigger.record` in place**
(`_inherit` with no `_name`, Odoo's in-place extension) and overrides the two
methods that turn an ORM event into a decision. Everything downstream is
unchanged: the same rules search, the same domain and company filters, the same
per-transaction budget, and the same `create:<model>,<id>` idempotency key — so
one form submission is one run, whether or not the website module is installed.

What the override adds is the one thing a website submission needs and a backend
save does not: **consent**. A backend user creating a partner is acting on a
record they can already see. A stranger filling in a form on the public site is
a different thing, and the research #5731 asked for
(`discoveries/odoo19-agent-automation-triggers.md`, §5) records that 19.0's
website consent gates were **not** verified and "must be validated before any
adapter reads `website.visitor` or browsing history".

NOTHING HERE READS VISITOR OR PAGE-VIEW DATA. That is deliberate. The research
deferred page-view behaviour outright — "deferred unless the research proves a
reliable, privacy-acceptable signal" — and named `website.visitor` /
`website.track` as exactly the surface whose consent gates are unvalidated. A
form submission is a record the submitter chose to create; a page view is
surveillance of someone who chose nothing. There is no query against
`website.track`, `website.visitor` or browsing history anywhere in this adapter.

The website module's own cookies-bar answer is not used either.
`website._allConsentsGranted()` keys off `ir.http._is_allowed_cookie('optional')`,
i.e. a cookie value on the *browser* request. A `create` dispatched by an import,
a cron or a second request has no such cookie, and a gate whose answer depends on
which thread is asking is not a gate. It is also the wrong question: the cookies
bar governs optional tracking cookies, not whether we may act on a named person's
enquiry.

HOW A FORM SUBMISSION IS RECOGNISED

Two signals, both required, both from 19.0 source in the pinned image
(`odoo:19.0-20260926`):

* a live HTTP request of type `http` — a public frontend request, not a backend
  JSON-RPC call — and
* `website_id` in the context of the environment that wrote the record.

`website_id` is put there by `website/models/ir_http.py::_frontend_pre_dispatch`
on every website frontend request (`request.update_context(website_id=website.id)`)
and by nothing else for business models, so together the two mean "the record was
written while serving a page on website X". `website/controllers/form.py::
insert_record` then creates the record with
`request.env[model].with_user(SUPERUSER_ID).with_context(mail_create_nosubscribe=
True)`, which keeps that context — so the adapter, whose environment is derived
from that one, sees both the marker and the record.

Why not match the route path instead? `WebsiteForm.website_form` is declared with
`website=True`, so on a multi-language site the same form posts to
`/fr/website/form/<model>`; a prefix test on `request.httprequest.path` would
silently stop detecting submissions in every language but the default one.

Why not `mail_create_nosubscribe`? The form controller sets it, but so do
`project.task`, `res.partner`'s own contact-sync and half a dozen core modules.
It means "do not subscribe the author", not "a stranger sent this".

The gate covers `write` as well as `create`. A form submission that updates an
existing record rather than creating one is the same person's request, and a gate
that only covered creates would have a door beside it.

CONSENT IS ODOO'S OWN MARKETING-CONSENT RECORD, NOT A NEW FIELD

The submitter's address is read from the record the form wrote — `email`, then
`email_from`, then `partner_id.email` — which covers every form target 19.0
ships: `crm.lead` (`email_from`, filled by `website_crm`), `hr.applicant`
(`email_from`), `project.task` (`partner_id`, filled by `website_project` from the
visitor), and any `res.partner` an administrator whitelists for forms (`email`).
It is then checked against `mail.blacklist`, which is core `mail`, searchable,
normalised on both sides (`email_normalize` in `_search`, so the comparison is
exact), and documented as "the contact won't receive mass mailing anymore, from
any list" — Odoo's own answer to "has this person consented to being marketed
at". Inventing a parallel consent field would make an administrator maintain the
same fact twice, and the copy that mattered would be the one an unsubscribe link
does not update.

Two consequences, both intentional:

* **An address on the blacklist is skipped.** No run, no agent, and one info line
  naming the record, the rule-free reason and the website, so an administrator
  can see the decision.
* **A submission with no address at all is skipped too**, with its own reason.
  Consent cannot be established for someone who cannot be identified, and the
  honest default for "may we act on this person's data" is no. The two reasons
  are logged as distinct codes because "the rule did not fire" and "the rule was
  refused by consent" are different facts about the same configuration.

The address itself is never logged: the line names `model,id` and a reason code.

The gate does not leak onto backend saves. A record created or edited by an
employee produces no `website_id` context, so `_admit()` hands it back untouched
— including when its email is blacklisted. Marketing consent is a condition on
strangers; imposing it on a clerk editing a contact they can already open would
be a silent, unexplainable outage of every automation on that model.

WHAT IS DELIBERATELY NOT IN THE MANIFEST

The ticket lists `__manifest__.py`, but adding `website` to `depends` would make
the website module mandatory, which is the one thing the acceptance criteria
forbid — and nothing else in a manifest governs an adapter that decides at
runtime whether the website module is loaded. The shape for website-specific
*views*, menus or ACLs is a separate bridge addon (`depends:
turnstone_integration, website`); that is a bigger change than this ticket and
none of its three criteria need it. `turnstone_trigger_website.py` is how Odoo
modules name one concern per file, and the concern here is a gate, not a screen.

No new model either. The adapter has no configuration and no state: it is a gate
and an origin test hung on two existing methods, so a `turnstone.trigger.website`
model would create a record nothing ever reads.
"""

import logging

from odoo import api, models
from odoo.http import request
from odoo.tools import email_normalize

_logger = logging.getLogger(__name__)

#: The optional module everything here is conditional on. Not a dependency: the
#: name is in the registry only when the module is installed, so this is the one
#: check that answers "is the website module loaded in this database".
WEBSITE_MODEL = "website"

#: Where a form submission's address lands, most specific first. `email` is
#: `res.partner`'s; `email_from` is what `crm.lead` and `hr.applicant` use (and
#: what `website_crm` fills from the form); `partner_id` covers the form targets
#: that store a visitor partner instead of a bare address.
CONSENT_EMAIL_FIELDS = ("email", "email_from")
CONSENT_PARTNER_FIELD = "partner_id"

#: Why a submission was refused. Short codes rather than sentences: they go to
#: the log beside `model,id`, and the address must never be written there.
REFUSED_NO_EMAIL = "no-email"
REFUSED_OPT_OUT = "marketing-opt-out"


class TurnstoneTriggerWebsite(models.AbstractModel):
    """The consent gate and the origin test for website form submissions.

    An in-place extension of `turnstone.trigger.record` — no `_name`, so it adds
    behaviour to the model that already exists instead of describing a second
    one. The module docstring explains why a second hook would double every
    submission.
    """

    _inherit = "turnstone.trigger.record"

    # -- the entry points the record adapter calls --------------------------

    @api.model
    def _on_create(self, records):
        """Queue runs for a create, once a website submission has consented.

        With the website module absent, or with no website request in flight,
        `_admit()` hands the records straight back and this is exactly #5752's
        behaviour. The whole cost on that path is one registry lookup and one
        `bool()` per event.
        """
        return super()._on_create(self._admit(records))

    @api.model
    def _on_write(self, records, vals):
        """The same gate for writes, so a submission cannot update past it."""
        return super()._on_write(self._admit(records), vals)

    # -- the gate -----------------------------------------------------------

    @api.model
    def _admit(self, records):
        """`records` narrowed to the ones a website submission may fire a rule for.

        Returns `records` unchanged whenever this is not a website form
        submission — see "the gate does not leak onto backend saves" above.
        """
        if not records:
            return records
        website = self._submitting_website()
        if not website:
            return records

        refusals = {}
        for record in records:
            reason = self._consent_refusal(record)
            if reason:
                refusals[record.id] = reason
        if not refusals:
            return records

        for record in records:
            if record.id in refusals:
                _logger.info(
                    "Turnstone automation: %s,%s was submitted through website %s "
                    "but is not admitted (%s), so no rule fires for it. Consent is "
                    "checked against mail.blacklist; a submission with no address "
                    "is refused because consent cannot be established for someone "
                    "who cannot be identified.",
                    record._name, record.id, website.id, refusals[record.id],
                )
        return records.filtered(lambda record: record.id not in refusals)

    @api.model
    def _submitting_website(self):
        """The website this request is being served on, or a false-empty value.

        Falsey means "not a website form submission": the module is not
        installed, this is not an HTTP frontend request, or the writing
        environment carries no `website_id`. Each of those is an ordinary case
        and none of them may raise — this runs inside every `create` and `write`
        of every model a rule watches.
        """
        if WEBSITE_MODEL not in self.env:
            # `env` is a Mapping of model name to model, so this is a registry
            # lookup: False exactly when the website addon is not installed.
            return None
        if not request or getattr(request, "type", None) != "http":
            # No request bound (a cron, an import, a test) or a backend JSON-RPC
            # call. `bool(request)` is False when nothing is bound — the idiom
            # core uses in a dozen models.
            return self.env[WEBSITE_MODEL]
        website_id = self.env.context.get("website_id")
        try:
            website_id = int(website_id)
        except (TypeError, ValueError):
            # A missing marker, or one some other module left as a non-id.
            # Either way: not a submission this adapter can vouch for.
            return self.env[WEBSITE_MODEL]
        # `sudo()` because the caller may be the public user, who cannot read
        # `website`; `exists()` because a deleted website is not a context.
        return self.env[WEBSITE_MODEL].sudo().browse(website_id).exists()

    # -- consent ------------------------------------------------------------

    @api.model
    def _consent_refusal(self, record):
        """Why `record` may not fire a rule, or `None` when it may.

        A short code rather than a message: it is logged beside `model,id` and
        must never carry the address itself.
        """
        email = self._consent_email(record)
        if not email:
            return REFUSED_NO_EMAIL
        # `sudo()` for the same reason `_rules_for()` uses it: whether an address
        # is blacklisted is configuration, and the public user whose submission
        # triggered the check has no right to read `mail.blacklist`. `_search`
        # normalises the value passed in, and `email` above is already
        # normalised, so the match is exact rather than roughly similar.
        blacklisted = self.env["mail.blacklist"].sudo().search(
            [("email", "=", email)], limit=1,
        )
        return REFUSED_OPT_OUT if blacklisted else None

    @api.model
    def _consent_email(self, record):
        """The submitter's normalised address, or `None` if the record has none.

        Read under `sudo()`: the form controller creates as the superuser, but a
        write dispatched from a portal session arrives as a much smaller user,
        and a rule must not fire for a manager and be refused for a customer
        because of a field-read permission.
        """
        record = record.sudo()
        for field_name in CONSENT_EMAIL_FIELDS:
            if field_name in record._fields:
                email = email_normalize(record[field_name] or "") or None
                if email:
                    return email
        if CONSENT_PARTNER_FIELD in record._fields:
            partner = record[CONSENT_PARTNER_FIELD]
            if partner._name == "res.partner" and "email" in partner._fields:
                return email_normalize(partner.email or "") or None
        return None
