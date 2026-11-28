"""A saved rule: "when this happens, ask that agent, with this prompt".

WHY THIS EXISTS

A `turnstone.agent` row says *who* to ask and *how* it is configured. It says
nothing about *when* to ask or *what* to ask. Those two questions are the whole
of automation, and they are kept here rather than on the agent because one agent
is deliberately reusable: the same "Kube SRE" agent should be startable by a
human in the chat, by a nightly cron, and by a new helpdesk ticket — three
rules, one agent, one place where the agent's tools and approvals are decided.

WHAT THIS MODEL DOES NOT DO

It wires no triggers. Nothing here overrides `create`/`write`, registers a cron,
or opens an HTTP route: the adapters that notice a record changing, a customer
replying or a webhook arriving are separate, and the code that launches a
session is the launch service on top of `turnstone.automation.run`. This model
is the trusted configuration those pieces read. Keeping it inert is also why it
is safe to install first: a rule that exists but has no adapter behind it fires
nothing.

TRUSTED CONFIGURATION, UNTRUSTED CONTENT

Everything on this row is administrator-managed and therefore trusted: which
agent, which model, which domain, which user the runs are attributed to. The
prompt template is trusted too, in the sense that an administrator wrote it —
but what it *renders to* is not, because the values come from business records
(a customer's name, a ticket body, an email signature). The rendered prompt is
untrusted content: it must only ever travel as the message body, never as a
session name or any configuration field sent to Turnstone. `_render_prompt()`
is the one place that turns a template plus a record into that body.
"""

import difflib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import timedelta

from psycopg2 import errors as psycopg2_errors

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import ConcurrencyError, UserError, ValidationError
from odoo.fields import Domain
from odoo.tools import html2plaintext
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)

# `{{ partner_id.name }}`. Deliberately NOT a template language: this is a
# placeholder for a field value, resolved by looking the field up, never by
# evaluating the expression. Odoo's own mail templates compile to Mako/Python,
# which is acceptable for a template an administrator runs with their own
# rights — but here the rendered text goes to an agent that acts with a SERVER
# token, so "evaluate this string" would be handing that authority to whoever
# can write a field. See `_parse_template()`.
_PLACEHOLDER_RE = re.compile(r"\{\{(?P<expr>[^{}]*)\}\}")

# One dotted path of plain identifiers. Requiring a letter first rules out
# `__class__`, `_env`, `_fields` and every other private name in one line,
# before any field lookup happens.
_FIELD_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")

# A path longer than this is not a field reference anyone meant to write, and
# every extra segment is another model reached. Many2one chains in Odoo are
# rarely deeper than 3 (`company_id.parent_id.name`).
MAX_PATH_SEGMENTS = 4

# A single value is capped so one essay-length Text field cannot turn into a
# megabyte prompt. Truncation is marked in the rendered text, so neither the
# agent nor the administrator is misled about what was sent.
MAX_VALUE_CHARS = 10_000

# x2many values render as a comma-separated list of names, head first.
MAX_X2MANY_ITEMS = 20

# Triggers that watch records: without a target model there is nothing to watch
# and nothing to render the prompt from. Webhook and manual rules carry their
# own context instead.
TARGET_MODEL_REQUIRED_FOR = (
    "record_create", "record_write", "record_create_or_write",
    "chatter_message", "schedule",
)

# ---------------------------------------------------------------------------
# Inbound webhooks (#5755)
# ---------------------------------------------------------------------------

#: `ir.config_parameter` key holding the ONE shared secret every inbound
#: webhook is signed with. One secret rather than one per rule: it is a single
#: value to set, a single value to rotate, and it lives where every other
#: deployment secret in Odoo lives, so it can be pinned by a config file or an
#: environment instead of by hand in a UI.
WEBHOOK_SECRET_PARAM = "turnstone_integration.webhook_secret"

#: How far a delivery's timestamp may be from ours before it is refused. This is
#: not the replay defence on its own (the event id is: a replayed delivery names
#: an event that already has a run), it is what bounds how long a captured
#: request stays worth replaying at all — including one whose run has since
#: been deleted.
WEBHOOK_MAX_SKEW_SECONDS = 300

#: The header a sender may use to name the event. A body field is accepted too
#: (`event_id`, then `delivery_id`): see `_webhook_event_id()`.
WEBHOOK_EVENT_HEADER = "X-Turnstone-Event-Id"
WEBHOOK_TIMESTAMP_HEADER = "X-Turnstone-Timestamp"
WEBHOOK_SIGNATURE_HEADER = "X-Turnstone-Signature"

#: How much of the payload reaches the agent. A webhook is external input: one
#: delivery must not be able to spend a megabyte of the agent's context.
WEBHOOK_MAX_PAYLOAD_CHARS = 20_000

#: `idempotency_key` and `dedupe_token` are `Char`, and an event id is chosen by
#: a third party. Bounded so a delivery cannot fail on a database limit.
WEBHOOK_MAX_KEY_CHARS = 255


#: The errors that mean "another transaction won this write". Odoo re-runs a
#: whole handler on these, which is exactly what a launch must not do (the
#: remote call already happened), so the few writes that can lose this race drop
#: the value instead of raising. Same tuple as `turnstone.session`'s, kept local
#: because that module's names its own concern.
RETRYABLE_WRITE_ERRORS = (
    psycopg2_errors.SerializationFailure,
    psycopg2_errors.LockNotAvailable,
    psycopg2_errors.DeadlockDetected,
    ConcurrencyError,
)

#: The database name of `turnstone.automation.run`'s `(automation_id,
#: idempotency_key)` unique constraint — `_run_key_uniq` on that model. Written
#: out here because this module is what has to tell "this event is already
#: recorded" apart from any other unique violation, and at REPEATABLE READ the
#: constraint's refusal is the *only* evidence available: a transaction whose
#: snapshot predates the winner's commit cannot see the winning row at all (see
#: `_deliver_webhook_once`). Odoo derives the name from the model table and the
#: constraint attribute, and `tests/` asserts it stays this value.
RUN_KEY_CONSTRAINT = "turnstone_automation_run_run_key_uniq"

#: Advisory-lock namespace for "one in-flight delivery per rule". Advisory
#: locks are keyed by two int4s, so the first is this module's own constant and
#: the second is the rule id: nothing else in the database can collide with it,
#: and it needs no table. See `_webhook_lock()`.
WEBHOOK_LOCK_NAMESPACE = 0x54535742  # "TSWB", well inside int4


#: Advisory-lock namespace for "one launcher is delivering this run". Same shape
#: as `WEBHOOK_LOCK_NAMESPACE`: advisory locks are keyed by two int4s, the first is
#: this module's own constant and the second is the run id, so nothing else in the
#: database can collide with it and it needs no table. See `_claim_delivery()`.
LAUNCH_DELIVERY_LOCK_NAMESPACE = 0x54534C44  # "TSLD", well inside int4

#: `cr.postcommit.data` key holding the launches this transaction armed for
#: delivery: a list of `(run_id, body)`. One key, one callback, however many
#: launches the transaction made — see `_arm_delivery()`.
DELIVERY_JOBS_KEY = "turnstone_integration.launch.jobs"


class WebhookRefused(Exception):
    """A delivery the webhook route must refuse.

    Carries the HTTP status and a short machine-readable code, and nothing else.
    The code is the *whole* of what a sender is told: no rule name, no model, no
    field, no server message. Those live on the run record, which is where
    somebody with access to this database can read them, and nowhere a third
    party can.

    Not a `UserError`: Odoo's own dispatcher would turn that into a 422 with a
    serialized exception, and an external sender must never see a traceback.
    """

    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


class TurnstoneAutomation(models.Model):
    _name = "turnstone.automation"
    _description = "Turnstone Agent Automation Rule"
    _order = "name, id"
    # `mail.thread` for one reason: a rule is a decision several administrators
    # take together ("should this fire on every write, or only on priority 3?"),
    # and the reasoning belongs next to the rule rather than in someone's inbox.
    # It adds no tracking — the fields here are configuration, and a prompt
    # template pasted into a chatter message would put business-record-shaped
    # text in a second place for no benefit.
    _inherit = ["mail.thread"]
    # Company consistency for `agent_id` and `run_as_user_id` is enforced by
    # Odoo on every create and write, against each model's own company rule.
    # Checked by hand in a constraint it would drift from core: an agent's
    # company comes from its connection, a user's is a list.
    _check_company_auto = True

    name = fields.Char(
        required=True,
        help="What this rule is for, in the administrator's words — "
             "\"New helpdesk ticket: first response\", not \"rule 3\".",
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict",
        default=lambda s: s.env.company,
    )
    agent_id = fields.Many2one(
        "turnstone.agent", string="Agent", required=True, ondelete="restrict",
        check_company=True, index=True,
        help="The saved configuration each run starts a session from. Its "
             "skill, persona, model and approval policy decide what the run "
             "can do; a rule cannot widen them.",
    )

    # -- when it fires ------------------------------------------------------
    trigger_type = fields.Selection(
        [
            ("record_create", "Record is created"),
            ("record_write", "Record is updated"),
            ("record_create_or_write", "Record is created or updated"),
            ("chatter_message", "Message received on a record"),
            ("schedule", "On a schedule"),
            ("webhook", "Inbound webhook"),
            ("manual", "On demand only"),
        ],
        string="Trigger", required=True, default="record_create", index=True,
        help="Which event family wakes this rule up. The adapter that notices "
             "the event is separate; nothing on this form is wired until one is "
             "installed.\n\n"
             "'On demand only' and 'Inbound webhook' need no target model: "
             "they carry their own context.",
    )
    target_model_id = fields.Many2one(
        "ir.model", string="Target Model", ondelete="cascade",
        domain="[('abstract', '=', False), ('transient', '=', False)]",
        help="The model whose records this rule watches, and whose fields the "
             "prompt may use. Required for every trigger that watches records "
             "or selects them on a schedule.",
    )
    # Stored so a trigger adapter can bail out in one indexed query per model,
    # instead of joining ir_model on every create and write of every model.
    target_model = fields.Char(
        related="target_model_id.model", store=True, index=True, readonly=True,
    )
    target_domain = fields.Char(
        string="Only These Records",
        help="Optional filter, in Odoo's usual domain syntax, evaluated "
             "against the target model — for instance [('priority', '=', '3')]. "
             "A record outside it does not fire the rule.\n\n"
             "It may only use fields of the target model and plain values. It is "
             "not Python: anything else is refused when you save.",
    )
    trigger_field_ids = fields.Many2many(
        "ir.model.fields", "turnstone_automation_trigger_field_rel",
        "automation_id", "field_id", string="Only When These Change",
        help="For 'Record is updated': fire only when one of these fields "
             "changed. Empty means any change fires it. Leave it empty if this "
             "rule's own agent touches the record it watches, or you will get a "
             "loop.",
    )

    # -- inbound webhook (#5755) -------------------------------------------
    webhook_key = fields.Char(
        string="Webhook Key", copy=False, index=True, readonly=True,
        help="The last path segment of this rule's webhook URL. Generated when "
             "the rule is saved with the Inbound webhook trigger, and never "
             "reused: rotating it (with the button on the form) is how a URL is "
             "withdrawn, because the old one stops resolving immediately.\n\n"
             "It is not the rule's id on purpose — ids count 1, 2, 3 and can be "
             "guessed, and this value is half of what a caller needs to reach "
             "the rule. It is not a secret either: the signature below is what "
             "authenticates a delivery, and reading this is a manager's right.",
    )

    # -- what it asks -------------------------------------------------------
    prompt_template = fields.Text(
        string="Prompt", required=True,
        help="The message the agent receives. Write it as the instruction, not "
             "as a description of one.\n\n"
             "Pull record values in with {{ field_name }}: a plain field of the "
             "target model, or a path through many2one fields such as "
             "{{ partner_id.name }}. Names, dates, selections and lists render "
             "as their display value.\n\n"
             "Only those field references are allowed. Python, expressions and "
             "anything else are refused when you save; they are never "
             "evaluated.",
    )
    store_prompt = fields.Boolean(
        string="Keep the Rendered Prompt",
        default=False,
        help="Store the rendered prompt on each run. Off by default: the prompt "
             "is built from business records and may hold a customer's name, "
             "address or message, which you may not want retained in a second "
             "place. Turn it on while debugging a rule, then turn it back off.",
    )

    # -- who it runs as -----------------------------------------------------
    run_as = fields.Selection(
        [
            ("triggering_user", "Whoever triggered it"),
            ("fixed_user", "A specific user"),
            ("automation_user", "The automation user"),
        ],
        string="Run As", required=True, default="triggering_user",
        help="The owner of the session a run creates, and the identity every "
             "permission check uses: the agent's group restriction, the "
             "server's allowed groups, and the record rules on the session.\n\n"
             "'Whoever triggered it' is the honest default for user-driven "
             "triggers. A cron or a webhook has no user, so those triggers need "
             "a specific user or the automation user.",
    )
    run_as_user_id = fields.Many2one(
        "res.users", string="Run As User", ondelete="restrict",
        check_company=True,
        help="Used only when Run As is 'A specific user'. Someone who is allowed "
             "to use the agent and the server, and whose companies include this "
             "rule's company.",
    )
    dedupe_window_minutes = fields.Integer(
        string="Suppress Repeats (minutes)", default=0,
        help="How far back to look for a run of this rule on the same record "
             "before starting another. 0 disables the window, so every matching "
             "event fires; set it for triggers that can arrive twice (a retried "
             "webhook, a burst of writes) or the same record will be sent to the "
             "agent repeatedly.\n\n"
             "Exact repeats of the same event are suppressed separately, by the "
             "event's own idempotency key, whatever this is set to.",
    )

    # -- counters -----------------------------------------------------------
    run_count = fields.Integer(readonly=True, copy=False, default=0)
    last_run_at = fields.Datetime(readonly=True, copy=False)
    # The run history, for the read-only list on this rule's form. It is the
    # rule's own view of `turnstone.automation.run`, so it goes no wider than
    # the run's company record rule already allows.
    run_ids = fields.One2many(
        "turnstone.automation.run", "automation_id", string="Runs",
        readonly=True,
        help="Every firing of this rule, newest first — including the ones that "
             "never reached an agent, and why.",
    )

    _name_company_uniq = models.Constraint(
        "unique(name, company_id)",
        "An automation with that name already exists for this company.",
    )
    # Two rules with one webhook URL would make "which rule does this delivery
    # wake" depend on row order. The empty values coexist: Postgres treats
    # NULLs as distinct in a unique index, which is exactly right for the rules
    # that have no webhook.
    _webhook_key_uniq = models.Constraint(
        "unique(webhook_key)",
        "Another automation already uses that webhook key. Give this rule its "
        "own (the button on the form generates one).",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Give every webhook rule its key before it is inserted.

        In `create()` rather than in a default, because the key has to exist for
        the row that is being written: a delivery arriving in the same instant
        as the save must find a rule, and a key filled in by a later write would
        leave a window where the rule exists with no URL at all.
        """
        for vals in vals_list:
            if vals.get("trigger_type") == "webhook" and not vals.get("webhook_key"):
                vals["webhook_key"] = self._generate_webhook_key()
        return super().create(vals_list)

    def write(self, vals):
        """A rule switched to Inbound webhook gets a key, in the same write.

        Without this, switching the trigger on an existing rule would leave it
        with no URL until somebody hit Rotate — a rule that looks configured and
        silently accepts nothing.

        Per record, because the key is a different value for each: a shared one
        would trip the unique constraint, and a delivery naming it could not say
        which rule it meant.
        """
        if vals.get("trigger_type") == "webhook" and not vals.get("webhook_key"):
            needing = self.filtered(lambda rec: not rec.webhook_key)
            for rec in needing:
                super(TurnstoneAutomation, rec).write(
                    dict(vals, webhook_key=self._generate_webhook_key())
                )
            rest = self - needing
            if rest:
                return super(TurnstoneAutomation, rest).write(vals)
            return True
        return super().write(vals)

    @api.model
    def _generate_webhook_key(self):
        """A fresh, unguessable webhook path segment.

        32 hex characters, the same shape as the idempotency keys this module
        already generates: one idea of "unguessable token" in the codebase
        rather than two. `secrets` rather than `random`, because this value is
        half of what reaching a rule requires and `random` is predictable from a
        few outputs.
        """
        return secrets.token_hex(16)

    def action_rotate_webhook_key(self):
        """Replace this rule's webhook URL with a new one. Old URL stops working."""
        self.ensure_one()
        self.sudo().write({"webhook_key": self._generate_webhook_key()})
        return True

    @api.constrains("trigger_type", "webhook_key")
    def _check_webhook_key(self):
        for rec in self:
            if rec.trigger_type == "webhook" and not rec.webhook_key:
                raise ValidationError(_(
                    "%(name)s uses the Inbound webhook trigger but has no webhook "
                    "key, so it would have no URL to receive anything on. Save "
                    "it again, or press Rotate Webhook Key.",
                ) % {"name": rec.name or _("this rule")})

    # -- validation ---------------------------------------------------------

    def _trigger_label(self):
        """The trigger's label in the current language, for error messages."""
        self.ensure_one()
        labels = dict(self._fields["trigger_type"]._description_selection(self.env))
        return labels.get(self.trigger_type, "") or ""

    @api.constrains("trigger_type", "target_model_id")
    def _check_target_model(self):
        for rec in self:
            if rec.trigger_type in TARGET_MODEL_REQUIRED_FOR and not rec.target_model_id:
                raise ValidationError(_(
                    "%(name)s uses the \"%(trigger)s\" trigger, which watches "
                    "records, so it needs a target model.",
                ) % {"name": rec.name, "trigger": rec._trigger_label()})
            if rec.target_model_id and rec.target_model_id.transient:
                raise ValidationError(_(
                    "%(model)s keeps no records to watch, so it cannot be a "
                    "target model.",
                ) % {"model": rec.target_model_id.name})

    @api.constrains("target_domain", "target_model_id")
    def _check_target_domain(self):
        """A domain is a small expression, so it gets `ir.rule`'s treatment:
        `safe_eval` (no builtins, no imports, checked bytecode) and then a real
        validation against the model it is meant for.
        """
        for rec in self:
            raw = (rec.target_domain or "").strip()
            if not raw:
                continue
            if not rec.target_model_id:
                raise ValidationError(_(
                    "%(name)s has a record filter but no target model, so there "
                    "is nothing to apply it to. Pick a target model or clear the "
                    "filter.",
                ) % {"name": rec.name})
            domain = rec._eval_target_domain()
            model = self.env[rec.target_model_id.model]
            try:
                domain.validate(model.sudo())
            except Exception as err:  # noqa: BLE001 - the message is the point
                raise ValidationError(_(
                    "The record filter on %(name)s is not valid for %(model)s: "
                    "%(error)s",
                ) % {
                    "name": rec.name,
                    "model": rec.target_model_id.model,
                    "error": err,
                }) from err

    @api.constrains("trigger_field_ids", "target_model_id")
    def _check_trigger_fields(self):
        for rec in self:
            if not rec.trigger_field_ids:
                continue
            if not rec.target_model_id:
                raise ValidationError(_(
                    "%(name)s lists fields to watch but has no target model.",
                ) % {"name": rec.name})
            foreign = rec.trigger_field_ids.filtered(
                lambda f: f.model != rec.target_model
            )
            if foreign:
                raise ValidationError(_(
                    "%(fields)s not on %(model)s, so a change to %(them)s cannot "
                    "be what fires this rule.",
                ) % {
                    "fields": ", ".join(foreign.mapped("name")),
                    "model": rec.target_model,
                    "them": _("it") if len(foreign) == 1 else _("them"),
                })

    @api.constrains("run_as", "run_as_user_id")
    def _check_run_as_user(self):
        for rec in self:
            if rec.run_as == "fixed_user" and not rec.run_as_user_id:
                raise ValidationError(_(
                    "%(name)s runs as a specific user but none is set. Pick the "
                    "user whose sessions these runs should be — otherwise nobody "
                    "can tell whose access they used.",
                ) % {"name": rec.name})
            if rec.run_as != "fixed_user" and rec.run_as_user_id:
                raise ValidationError(_(
                    "%(name)s sets a run-as user while Run As is not \"A specific "
                    "user\", so the user would be ignored. Clear it, or change "
                    "Run As.",
                ) % {"name": rec.name})

    @api.onchange("run_as")
    def _onchange_run_as(self):
        # A stale user left behind is not just clutter: the constraint above
        # refuses the save, and "why will this not save" is a bad question to
        # put in front of an administrator.
        if self.run_as != "fixed_user":
            self.run_as_user_id = False

    @api.constrains("prompt_template", "target_model_id")
    def _check_prompt_template(self):
        for rec in self:
            if not (rec.prompt_template or "").strip():
                raise ValidationError(_(
                    "%(name)s has an empty prompt, so the agent would be started "
                    "with no instruction. Write the prompt or archive the rule.",
                ) % {"name": rec.name})
            rec._parse_template()

    # -- prompt template ----------------------------------------------------

    def _parse_template(self):
        """Return the template as a list of `("text", str)` and
        `("field", (segment, …))` parts.

        Raises `ValidationError` for anything that is not a plain field
        reference. This is the whole safety story for templates: fields are
        looked up by name and what is between the braces is never evaluated, so
        there is no expression to be clever with. `_render_prompt()` renders
        exactly what this returns, which is why validation and rendering cannot
        drift apart.
        """
        self.ensure_one()
        template = self.prompt_template or ""
        parts = []
        position = 0
        for match in _PLACEHOLDER_RE.finditer(template):
            parts.append(("text", template[position:match.start()]))
            parts.append(("field", self._parse_field_path(match.group("expr"))))
            position = match.end()
        parts.append(("text", template[position:]))

        # A stray brace would otherwise reach the agent as literal text and be
        # mistaken for an instruction that failed to fill in.
        rest = _PLACEHOLDER_RE.sub("", template)
        if "{{" in rest or "}}" in rest:
            raise ValidationError(_(
                "The prompt on %(name)s has an unclosed placeholder. Every value "
                "must be written like {{ field_name }}.",
            ) % {"name": self.name or _("this rule")})
        return parts

    def _parse_field_path(self, expr):
        """Validate one `{{ … }}` expression and return it as a tuple."""
        self.ensure_one()
        expr = (expr or "").strip()
        name = self.name or _("this rule")

        if not expr:
            raise ValidationError(_(
                "The prompt on %(name)s has an empty {{ }} placeholder. Put a "
                "field name in it or remove it.",
            ) % {"name": name})

        if not _FIELD_PATH_RE.match(expr):
            raise ValidationError(_(
                "The prompt on %(name)s contains {{ %(expr)s }}, which is not a "
                "field reference. A prompt may only name fields: Python, "
                "expressions, indexing, calls and private names are refused "
                "rather than evaluated.",
            ) % {"name": name, "expr": expr})

        segments = expr.split(".")
        # `{{ object.name }}` reads naturally and means the same thing as
        # `{{ name }}`, so it is allowed as a kindness — unless the model really
        # has a field called `object`, in which case the administrator meant that
        # field and stripping it would silently read something else.
        target = self.env.get(self.target_model) if self.target_model_id else None
        has_object_field = target is not None and "object" in target._fields
        if segments[0] == "object" and not has_object_field:
            segments = segments[1:]
            if not segments:
                raise ValidationError(_(
                    "The prompt on %(name)s refers to {{ object }} without a "
                    "field. Name the field, e.g. {{ object.name }}.",
                ) % {"name": name})

        if len(segments) > MAX_PATH_SEGMENTS:
            raise ValidationError(_(
                "The prompt on %(name)s follows %(count)s levels of relationship "
                "in {{ %(expr)s }}. At most %(max)s are allowed: a deeper path "
                "reads much of the database to fill in one sentence.",
            ) % {
                "name": name, "count": len(segments), "expr": expr,
                "max": MAX_PATH_SEGMENTS,
            })

        if not self.target_model_id:
            raise ValidationError(_(
                "The prompt on %(name)s uses {{ %(expr)s }}, but the rule has no "
                "target model, so there is no record to read it from. Pick a "
                "target model first.",
            ) % {"name": name, "expr": expr})

        model = target
        if model is None:
            raise ValidationError(_(
                "%(model)s is not installed on this database, so the prompt on "
                "%(name)s cannot read any of its fields.",
            ) % {"model": self.target_model_id.name, "name": name})

        for index, segment in enumerate(segments):
            field = model._fields.get(segment)
            if field is None:
                suggestion = difflib.get_close_matches(segment, model._fields, n=1)
                raise ValidationError(_(
                    "The prompt on %(name)s uses {{ %(expr)s }}, but %(model)s has "
                    "no field named %(field)s.%(did_you_mean)s",
                ) % {
                    "name": name,
                    "expr": expr,
                    "model": model._description or self.target_model,
                    "field": segment,
                    "did_you_mean": _(" Did you mean %s?") % suggestion[0]
                                    if suggestion else "",
                })
            if index == len(segments) - 1:
                break
            if field.type in ("one2many", "many2many"):
                raise ValidationError(_(
                    "The prompt on %(name)s follows {{ %(expr)s }} through "
                    "%(field)s, which is a list of records. A list has no single "
                    "value to use here: reference a field of %(model)s itself, or "
                    "render the list with {{ %(field)s }}.",
                ) % {
                    "name": name, "expr": expr, "field": segment,
                    "model": model._description or self.target_model,
                })
            if not field.relational:
                raise ValidationError(_(
                    "The prompt on %(name)s uses {{ %(expr)s }}, but %(field)s on "
                    "%(model)s is a %(type)s, not a link to another record, so it "
                    "has no .%(next)s.",
                ) % {
                    "name": name, "expr": expr, "field": segment,
                    "model": model._description or self.target_model,
                    "type": field.type, "next": segments[index + 1],
                })
            model = self.env[field.comodel_name]
        return tuple(segments)

    def _render_prompt(self, record=None):
        """Render this rule's prompt, with or without a record.

        `record` is the record that triggered the run. It may be omitted only
        when the prompt uses no fields (`webhook` and `manual` rules carry their
        own context). If the template asks for a field and there is no record —
        or a record of another model — that is a configuration error, and raises
        rather than rendering something half-filled.

        Returns `str`: the message body, which is untrusted content.
        """
        self.ensure_one()
        parts = self._parse_template()
        if record is not None:
            if not record:
                raise UserError(_(
                    "%(name)s was given an empty record, so its prompt cannot be "
                    "filled in.",
                ) % {"name": self.name})
            if record._name != self.target_model:
                raise UserError(_(
                    "%(name)s watches %(expected)s but was given a %(got)s "
                    "record. A prompt can only read the model it was written for.",
                ) % {
                    "name": self.name,
                    "expected": self.target_model_id.name or self.target_model,
                    "got": record._name,
                })

        rendered = []
        for kind, value in parts:
            if kind == "text":
                rendered.append(value)
                continue
            if record is None:
                raise UserError(_(
                    "The prompt on %(name)s uses {{ %(path)s }}, so it needs the "
                    "record that triggered it — and this run has none.",
                ) % {"name": self.name, "path": ".".join(value)})
            rendered.append(self._render_field_path(record, value))
        return "".join(rendered)

    def _render_field_path(self, record, path):
        """Read one validated path off `record` and format it as text."""
        current = record
        value = None
        field = None
        for index, segment in enumerate(path):
            field = current._fields[segment]
            value = current[segment]
            if index == len(path) - 1:
                break
            # Not the last segment: `_parse_template()` already proved each
            # intermediate segment is a many2one, but real data can still find
            # it empty. An empty link renders as nothing, not as "False".
            if not value:
                return ""
            current = value
        return self._render_value(value, field, record)

    def _render_value(self, value, field, record):
        """One field value as plain text: bounded, and never `None`."""
        if field.type in ("one2many", "many2many"):
            names = [name for name in value.mapped("display_name") if name]
            shown = ", ".join(names[:MAX_X2MANY_ITEMS])
            if len(names) > MAX_X2MANY_ITEMS:
                shown += _(", … and %(count)s more") % {
                    "count": len(names) - MAX_X2MANY_ITEMS,
                }
            return shown
        if field.type == "binary":
            raise UserError(_(
                "%(field)s holds a file, which cannot be put into a prompt. Name "
                "the record instead and let the agent fetch what it needs.",
            ) % {"field": field.string or field.name})
        if field.type == "selection":
            # The base implementation renders the raw key, and "blocked_done"
            # in the middle of an instruction is a riddle for the model.
            labels = dict(field._description_selection(self.env))
            text = labels.get(value, value) if value else ""
        else:
            # Display names are what a human reads: dates in the user's zone,
            # many2one links as their record name, numbers as text.
            text = field.convert_to_display_name(value, record)
        if not text:
            return ""
        text = str(text)
        if field.type == "html":
            # A Text body pasted straight into a prompt is markup the agent has
            # to spend context reading around.
            text = html2plaintext(text)
        if len(text) > MAX_VALUE_CHARS:
            text = text[:MAX_VALUE_CHARS] + _(" … [truncated]")
        return text

    # -- contracts for the trigger adapters and the launch service ----------

    def _eval_target_domain(self):
        """The rule's record filter as a `Domain` on the target model."""
        self.ensure_one()
        raw = (self.target_domain or "").strip()
        if not raw:
            return Domain.TRUE
        try:
            return Domain(safe_eval(raw, {}))
        except Exception as err:  # noqa: BLE001 - re-raised with context
            raise ValidationError(_(
                "The record filter on %(name)s could not be read: %(error)s",
            ) % {"name": self.name, "error": err}) from err

    def _matches_domain(self, records):
        """Filter `records` down to the ones this rule's filter accepts."""
        self.ensure_one()
        return records.filtered_domain(self._eval_target_domain())

    def _resolve_run_as_user(self, triggering_user=None):
        """The user a run of this rule is attributed to.

        Every permission check a launch makes — the agent's groups, the server's
        allowed groups, the session's record rules — is made against this user,
        so it is decided here, once, and never defaults to the superuser: a run
        that used root's rights would pass every check and leave nobody
        answerable for it.
        """
        self.ensure_one()
        if self.run_as == "fixed_user":
            if not self.run_as_user_id:
                raise UserError(_(
                    "%(name)s has no run-as user set, so it cannot be started.",
                ) % {"name": self.name})
            return self.run_as_user_id
        if self.run_as == "automation_user":
            return self._automation_user()
        if not triggering_user:
            raise UserError(_(
                "%(name)s runs as whoever triggered it, and this trigger has no "
                "user. Set Run As to a specific user or the automation user.",
            ) % {"name": self.name})
        return triggering_user

    @api.model
    def _automation_user(self):
        """The configured identity for runs nobody triggered.

        Deliberately refused when it is not configured. Falling back to the
        superuser would make every unattended run pass every check, and leave
        the audit trail naming nobody.
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "turnstone_integration.automation_user_id"
        )
        user = self.env["res.users"].sudo().browse(int(raw)) if raw else None
        if not user or not user.exists():
            raise UserError(_(
                "This automation runs as the automation user, but none is "
                "configured. Set the automation user in the Turnstone settings to "
                "someone who may use the agent, or give the rule a run-as user.",
            ))
        return user

    def _dedupe_since(self):
        """Earliest run time that still suppresses a repeat, or `None`.

        Measured back from now: a run of this rule on the same record inside the
        window means this event is a repeat. `0` disables the window, and exact
        repeats are suppressed by idempotency key whatever this returns.
        """
        self.ensure_one()
        if self.dedupe_window_minutes <= 0:
            return None
        return fields.Datetime.now() - timedelta(
            minutes=self.dedupe_window_minutes
        )

    def action_view_runs(self):
        """Open this rule's run history, as a list scoped to the rule.

        Built here rather than referenced as an xmlid so the button works
        without the action record having to be loaded first, and so the domain
        is this rule's, never the caller's to widen.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Automation Runs"),
            "res_model": "turnstone.automation.run",
            "view_mode": "list",
            "domain": [("automation_id", "=", self.id)],
        }

    def _note_run(self, when=None):
        """Count one run and stamp its time. Called by the launch service.

        `sudo()` so the counter still moves when the run itself is attributed to
        an ordinary user: it is bookkeeping about the rule, not about them.

        **Never raises (OTI-5755).** This writes the rule row, and it is the one
        row every concurrent trigger of the same rule shares: at REPEATABLE READ
        two launches incrementing `run_count` at once do not serialise, and the
        loser gets `SerializationFailure` — after the remote call may already
        have happened, which turns a counter into a lost event. Measured before
        this guard existed: 8 concurrent webhook deliveries to one rule lost one
        of them to a 500 raised here. The counter is a convenience; the run rows
        are the record, and `run_ids` still answers "how many times did this
        fire" exactly.

        So the write runs in a savepoint and a lost race is dropped, the same
        trade `turnstone.session._no_replay()` makes for the same reason.
        """
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    """UPDATE turnstone_automation
                          SET run_count = COALESCE(run_count, 0) + 1,
                              last_run_at = %s
                        WHERE id = %s""",
                    (when or fields.Datetime.now(), self.id),
                )
                # The ORM's cache holds the pre-update value, and any reader in
                # this transaction would otherwise see the old count.
                self.invalidate_recordset(["run_count", "last_run_at"])
        except RETRYABLE_WRITE_ERRORS as exc:
            _logger.info(
                "Automation %s: run counter lost a write race (%s); the run itself "
                "is recorded and the counter is advisory.", self.name, exc,
            )
        return True

    # -- launching ----------------------------------------------------------

    def _launch_for_record(self, record=None, *, triggering_user=None,
                           idempotency_key=None, dedupe_token=None, prompt=None):
        """Start one Turnstone session for this rule, and record how it went.

        This is the one entry point other modules call — a server action, a
        workflow, or a trigger adapter — so nothing has to loop back through an
        HTTP controller to start a configured agent. It reuses the session
        create the chat uses (`turnstone.session._ensure_started`), which sends
        the prompt in the create call as `initial_message`: one HTTP request
        makes one workstream carrying one prompt.

        It returns the `turnstone.automation.run` row, always — including when
        the launch failed. That is the point of the run: "did the ticket that
        arrived at 09:14 ever reach an agent, and if not why" has to be
        answerable, so a refusal or a transport failure is written down instead
        of raised into a caller whose rollback would erase the evidence. Read
        `run.state` and `run.error`:

        * `failed` with `attempt_count == 0` — refused before anything was
          tried: no permission, no company, a prompt that would not render.
        * `failed` with an odd count — an attempt was made and did not land.
        * `skipped` — the event arrived and the rule's own window suppressed it.
        * `queued` — the intent is committed (this run, and the `draft` session its
          workstream id is derived from) and the delivery has not finished yet.
          The launch delivers after this transaction commits, so this is what a
          successful launch looks like on the way out — see `_attempt_launch()`.
        * `done` — a session exists and the prompt was delivered to it.

        ``record``
            The record that fired the rule: it supplies the prompt's field
            values, the run's `trigger_res_model`/`trigger_res_id`, and by
            default the dedupe token. `webhook` and `manual` rules launch with
            no record; a prompt that uses `{{ }}` then fails, because there is
            nothing to fill it from.
        ``triggering_user``
            Who caused it, used only when the rule runs as `triggering_user`.
            A cron or a webhook passes nothing and the rule's own run-as setting
            decides — and refuses rather than falling back to the superuser.
        ``idempotency_key``
            Identifies the event. Defaults to a fresh UUID, which is right for a
            manual launch and wrong for anything that can be delivered twice.
        ``dedupe_token``
            What the run is *about*, for the rule's "Suppress Repeats" window.
            Defaults to the record's `model,id`; pass one when "the same thing"
            is coarser than one record.
        ``prompt``
            Overrides the rendered template. The rendered prompt is untrusted
            content either way: it travels only as the message body.

        A repeat call with the same key is answered with the run the first call
        produced and sends nothing — except when that run is `failed`. A failed
        run did not deliver the event (that is what `failed` means here), so
        dedupe must not turn it into a lost event: the run is reused and the
        launch is tried again, which is also why `attempt_count` counts attempts
        at one event rather than runs. A run still `queued` or `running` is left
        alone rather than raced.
        """
        self.ensure_one()
        record = self._launch_record(record)
        runs = self.env["turnstone.automation.run"].sudo()
        if not idempotency_key:
            idempotency_key = runs._generate_key()
        if not dedupe_token and record:
            # "The same thing" for a record-driven rule is the record. A window
            # with no token could never match (`_window_duplicate` refuses
            # that), so defaulting it here is what makes the setting work
            # instead of failing on the first event.
            dedupe_token = "%s,%s" % (record._name, record.id)

        # 1. Does the rule's own window suppress this event? Asked BEFORE this
        #    event gets a run of its own, because `_window_duplicate` matches on
        #    the dedupe token and a run in `queued` counts — so a fresh run would
        #    match itself and every event would suppress itself. A rule with a
        #    window and no token raises here rather than never matching; that is
        #    a caller error, recorded below once the run exists.
        try:
            duplicate = runs._window_duplicate(self, dedupe_token)
            window_error = None
        except UserError as exc:
            duplicate, window_error = runs.browse(), exc

        # 2. Claim the event, or find out it is already handled. This is the
        #    only write that can race, so it is the only one that has to be
        #    safe: `_open_for_event` returns another caller's run with
        #    created=False rather than tripping the unique constraint.
        run, mine = runs._open_for_event(
            self, idempotency_key, record=record, dedupe_token=dedupe_token,
        )
        retry = False
        if not mine:
            if run.state != "failed":
                _logger.info(
                    "Automation %s: key %s is already run %s (%s); not launching again.",
                    self.name, idempotency_key, run.id, run.state,
                )
                return run
            # A failed run did not deliver the event, so this is a retry rather
            # than a duplicate. Serialise it: two callers retrying one key would
            # otherwise both see `failed` and both start a session, and the key
            # would then mean two workstreams after all.
            run = runs._lock_for_retry(run)
            if not run:
                return self._find_run_for_key(idempotency_key)
            retry = True
        else:
            self._note_run()

        if window_error is not None:
            return self._fail_run(run, window_error)
        # A retry is not suppressed by the window. The window answers "did this
        # rule already run about this thing recently", and this event's own run
        # is what suppressed every other event — letting it suppress its own
        # retry would turn a transient failure into a dropped event, which is
        # the one outcome the run model refuses to have.
        if duplicate and not retry:
            return run._mark_skipped(reason=_(
                "%(rule)s suppresses repeats within %(minutes)s minutes, and run "
                "%(run)s already handled this record inside that window.",
            ) % {
                "rule": self.name,
                "minutes": self.dedupe_window_minutes,
                "run": duplicate.id,
            })

        # 3. Every exit from here lands on `run`, so nothing can fail silently.
        try:
            return self._attempt_launch(run, record, triggering_user, prompt)
        except Exception as exc:  # noqa: BLE001 - see the class docstring on errors
            # A bug here must still leave the event answerable rather than a run
            # stuck in `running` with no explanation. Anything already recorded
            # is kept: a failure is a transition, not an overwrite of the
            # attempt count. The message is cleaned before it is logged as well
            # as before it is stored — see `_attempt_launch`.
            secrets = self._launch_secrets()
            cleaned = run._clean_text(exc, secrets)
            _logger.exception(
                "Automation %s: launching run %s raised unexpectedly: %s",
                self.name, run.id, cleaned,
            )
            if run.state in ("queued", "running"):
                run._mark_failed(exc, secrets=secrets)
            return run

    def _find_run_for_key(self, idempotency_key):
        """This event's run, for a caller that lost the retry race."""
        return self.env["turnstone.automation.run"].sudo()._find_by_key(
            self, idempotency_key,
        )

    def _attempt_launch(self, run, record, triggering_user, prompt):
        """One attempt at delivering this run's prompt. Returns the run.

        Two phases, and where the line between them falls is the whole of
        OTI-5786:

        1. **Here, in the caller's transaction** — permission, prompt, and the
           intent rows: this run, and a `draft` session carrying no `ws_id`.
           No HTTP.
        2. **After that transaction commits** — `_arm_delivery()` schedules
           `_deliver_queued_run()` on a cursor of its own: the create call, then
           `ws_id` and state written back in a second transaction.

        Why the split is load-bearing. The workstream id presented to Turnstone
        is derived from the session row's own id
        (`turnstone.session._client_ws_id()`), and that derivation is only an
        idempotency token if the id is **already durable** when the POST is
        made. Minted inside the caller's transaction it was not: Odoo re-runs a
        JSON-RPC handler up to five times on a `SerializationFailure`
        (`service.model.retrying()`), and `ir.cron` retries a job the same way.
        A rollback after the POST therefore left the re-run with a session row
        that had never existed, a fresh id, and a fresh `ws_id` — so the 409 the
        routing proxy answers for a repeated id could never fire, and the same
        prompt started two workstreams. Committing the intent first removes the
        window instead of narrowing it: a rollback now discards an attempt that
        never happened, and every id Turnstone is ever shown for one event was
        committed before the request that carried it. A re-run after a rollback
        mints a new row, sends nothing, and delivers once.
        """
        # 4. Who is this for? Refused before a session exists and before any
        #    HTTP, and recorded on the run: a permission problem is exactly what
        #    a person investigating "why did this not fire" needs to see.
        try:
            owner = self._resolve_run_as_user(triggering_user)
            self._check_launch_allowed(owner)
        except UserError as exc:
            _logger.info("Automation %s refused run %s: %s", self.name, run.id, exc)
            return self._fail_run(run, exc)

        # 5. Render. A template that will not render is a configuration problem
        #    but still untried: `failed` with no attempt counted says so.
        try:
            body = prompt if prompt is not None else self._render_prompt(record)
        except UserError as exc:
            _logger.warning(
                "Automation %s could not render its prompt: %s",
                self.name, run._clean_text(exc),
            )
            return self._fail_run(run, exc)
        run._record_prompt(body)

        # 6. Commit the intent, and only the intent. The session row is created
        #    here — not in the delivery, which runs after this transaction is
        #    gone — because its id is the workstream id Turnstone is shown, and
        #    that has to be durable before the POST. `_mark_running()` is
        #    deliberately NOT called: nothing has been tried yet, and a run that
        #    counted an attempt before the request that makes it would lie about
        #    a delivery that a rollback then erased.
        try:
            session = self._launch_session(run, owner, record)
        except Exception as exc:  # noqa: BLE001 - see the class docstring on errors
            # The log line is as exposed as the run history: a log aggregator is
            # read by more people than a connection is, and the exception a 500
            # produces quotes the response body, which is where a bearer header
            # turns up. So the recorded text is what gets logged too.
            secrets = self._launch_secrets()
            cleaned = run._clean_text(exc, secrets)
            _logger.warning(
                "Automation %s: run %s could not be prepared: %s",
                self.name, run.id, cleaned,
            )
            return run._mark_failed(exc, secrets=secrets)

        run.write({"session_id": session.id})
        self._arm_delivery(run, session, body)
        return run

    def _launch_session(self, run, owner, record=None):
        """The `draft` session this run will send its prompt from. Has no `ws_id`.

        Reused when the run already has one. That is what a retry sees: the
        session was committed with the run, so its id — and therefore the
        workstream id derived from it — is the SAME across every later attempt
        (the post-commit delivery of this call, a cron drain, a retry in another
        process). Re-minting it per attempt would give each attempt a new
        idempotency token and re-open exactly the duplicate this ticket is
        about.
        """
        self.ensure_one()
        run.ensure_one()
        if run.session_id:
            return run.session_id
        return self._create_launch_session(owner, record)

    def _arm_delivery(self, run, session, body):
        """Schedule this run's delivery for after the caller's transaction commits.

        Phase two of the launch, and the reason `_attempt_launch()` commits the
        intent (run + `draft` session) and no HTTP: the session row is committed
        first, so the workstream id derived from it is durable before the request
        that presents it. A rollback after this call now discards an attempt that
        never happened instead of leaving a live workstream with no row behind it,
        and a re-run of the handler sends nothing (its own keyed run already
        exists). See the module's OTI-5786 note in `_attempt_launch()`.

        `env.cr.postcommit` rather than an immediate call: the hook is cleared by
        `rollback()` and runs by `commit()`, so it is the transaction boundary
        itself that decides whether the remote effect happens, with no window
        either side of it. One callback per transaction, delivering every run the
        transaction armed — a rule fired by a batch, or a webhook that also logs,
        must not open one cursor per launch.

        The callback never raises into the caller: it runs *after* the commit, so
        an exception there could not undo anything and would surface as a 500 for
        a request that succeeded. Failures are recorded on the run instead, and
        one run's failure does not stop the next.

        A caller whose process dies between the commit and the hook leaves a
        `queued` run with a committed `draft` session behind it: exactly the shape
        the queue worker (#5754) drains, and draining it presents the same
        workstream id this call would have.
        """
        self.ensure_one()
        postcommit = self.env.cr.postcommit
        # Captured now, deliberately: after the commit the recordset and its
        # environment are stale, and only these three survive that boundary.
        registry = self.env.registry
        context = dict(
            lang=self.env.lang,
            allowed_company_ids=[self.company_id.id],
        )
        jobs = postcommit.data.setdefault(DELIVERY_JOBS_KEY, [])
        if any(job_run_id == run.id for job_run_id, _body in jobs):
            return False
        jobs.append((run.id, body))
        if len(jobs) > 1:
            return True

        @postcommit.add
        def deliver_armed_launches():
            # `with registry.cursor()` commits on success and rolls back on a
            # failure that reached this far, which is the whole of the error
            # handling the hook itself needs. `jobs` is passed explicitly: this
            # cursor's own `postcommit` queue belongs to the *caller's*
            # transaction, and the fresh cursor has an empty one of its own.
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, context)
                env["turnstone.automation"]._deliver_armed(jobs)

        return True

    @api.model
    def _deliver_pending(self):
        """Deliver, now, the launches *this* transaction armed for post-commit.

        The post-commit hook's body, callable directly for the two callers that
        cannot wait for a real commit:

        * a test — `TestCursor.commit()` clears `postcommit` instead of running
          it, so a case that wants to assert on the delivery has to drive it;
        * a caller in a transaction it does not own (a `--test-enable` run, a
          shell) that wants the delivery to happen before it commits.

        Popping the queue is what makes a second call a no-op rather than a
        second delivery. The hook itself does not come through here — it holds
        the list this transaction armed, which a fresh cursor cannot see. A
        commit after this call still runs that hook; by then each run is `done`
        and `_deliver_queued_run()` leaves it alone, so the second pass is a
        read and nothing more.
        """
        jobs = self.env.cr.postcommit.data.pop(DELIVERY_JOBS_KEY, None)
        if not jobs:
            return self.env["turnstone.automation.run"].sudo().browse()
        return self._deliver_armed(jobs)

    @api.model
    def _deliver_armed(self, jobs):
        """Deliver each armed `(run_id, body)`. Runs on the post-commit cursor.

        One savepoint per job: a delivery whose own writes fail must not make the
        transaction unusable for the next one. The savepoint only covers the
        *local* writes — the remote create is not undone by it, and does not need
        to be: the workstream was asked for with an id that was committed before
        the request, so a later attempt adopts it (409) rather than making a
        second one.

        A failure that got past `_deliver_queued_run`'s own handling is recorded
        on the run *after* its savepoint rolls back, in a fresh one: the run's
        own failure write would otherwise be rolled back with the failure it is
        describing, leaving a `queued` row and no explanation.
        """
        delivered = self.env["turnstone.automation.run"].sudo().browse()
        for run_id, body in list(jobs):
            try:
                with self.env.cr.savepoint():
                    delivered |= self._deliver_queued_run(run_id, body)
            except Exception as exc:  # noqa: BLE001 - one run must not stop the next
                _logger.exception(
                    "Automation launch: run %s could not be delivered after "
                    "commit; recording it so the run worker can retry.", run_id,
                )
                delivered |= self._record_delivery_failure(run_id, exc)
        return delivered

    @api.model
    def _record_delivery_failure(self, run_id, exc):
        """Put an unexpected delivery failure on the run, in its own savepoint."""
        runs = self.env["turnstone.automation.run"].sudo()
        run = runs.browse(run_id)
        if not run.exists() or run.state not in ("queued", "running"):
            return run
        try:
            with self.env.cr.savepoint():
                rule = run.automation_id.sudo()
                return run._mark_failed(exc, secrets=rule._launch_secrets())
        except Exception:  # noqa: BLE001 - the log line above is the fallback
            _logger.exception(
                "Automation launch: run %s could not even record why its delivery "
                "failed; it stays queued for the run worker.", run_id,
            )
            return run

    @api.model
    def _deliver_queued_run(self, run_id, body=None):
        """The remote half of one launch: create the workstream, write it back.

        Idempotent by construction rather than by bookkeeping. The session this
        run carries has no `ws_id` yet, and `_ensure_started()` presents
        `session._client_ws_id()` — derived from a row that was committed before
        this call — so a delivery repeated for any reason (a retried hook, the
        run worker #5754, a second process) asks for the same workstream id and
        is answered 409 and adopted rather than creating a second one.

        ``body`` is the rendered prompt the launch already had. Omitted, it is
        re-rendered from the run's own trigger record — which is what a run left
        `queued` by a process that died between the commit and the hook needs,
        and why the run records `trigger_res_model`/`trigger_res_id` at all. A
        webhook run has no such record and its payload is not retained, so a
        re-render for one raises and lands on the run as `failed`: the sender's
        retry then re-delivers the event, and the `failed` run is retried in
        place rather than treated as a duplicate.

        A run that is not `queued` or `failed` is left alone: `running` means
        another launcher holds it, and the terminal states are final. A run whose
        row has gone (its rule was deleted, and the cascade took it) is skipped.
        """
        runs = self.env["turnstone.automation.run"].sudo()
        run = runs.browse(run_id)
        if not run.exists() or run.state not in ("queued", "failed"):
            return run
        rule = run.automation_id.sudo()
        session = run.session_id.sudo()
        if not session.exists():
            # The intent committed with the run, so this is not a race: the
            # session was deleted between the commit and here.
            return run._mark_failed(_(
                "The session this run was prepared with no longer exists, so its "
                "prompt could not be delivered. The rule is intact; launch the "
                "event again to start a new session.",
            ))
        if body is None:
            try:
                body = rule._queued_run_prompt(run)
            except UserError as exc:
                # Nothing was sent, so nothing is counted: `failed` with the
                # attempt count unchanged says "not tried, and why". Called on
                # the rule (not `self`, which is an empty recordset here).
                return rule._fail_run(run, exc)
        if not self._claim_delivery(run_id):
            # Another launcher is delivering the same run right now (the run
            # worker, or a duplicate arm from a second request). It stays queued
            # for whoever holds it, and nothing is sent twice.
            _logger.info(
                "Automation launch: run %s is already being delivered by another "
                "worker; leaving it alone.", run_id,
            )
            return run

        run._mark_running()
        try:
            session._ensure_started(first_message=body)
        except Exception as exc:  # noqa: BLE001 - see the class docstring on errors
            # Recorded rather than raised: a transport failure is the honest
            # `failed` on the run, and a `failed` run is retried in place by
            # whoever re-delivers the event. The log line gets the same cleaning
            # as the row — see `_launch_secrets()`.
            secrets = rule._launch_secrets()
            cleaned = run._clean_text(exc, secrets)
            _logger.warning(
                "Automation %s: run %s failed to launch: %s",
                rule.name, run.id, cleaned,
            )
            return run._mark_failed(exc, secrets=secrets)

        run._mark_done(session)
        self.env["turnstone.audit.log"].log(
            "session_create", session=session, agent=rule.agent_id,
            user=session.user_id,
            detail=_("Started by the automation %(rule)s, run %(run)s.") % {
                "rule": rule.name, "run": run.id,
            },
        )
        return run

    @api.model
    def _claim_delivery(self, run_id):
        """Take this run's delivery slot for the current transaction, or `False`.

        `pg_try_advisory_xact_lock`, not `SELECT … FOR UPDATE`: at Odoo's
        REPEATABLE READ a blocking row lock turns the loser into a
        `SerializationFailure` *after* it may already have POSTed, which is the
        failure this ticket is about. A try-lock lets the loser step back without
        writing anything, and being transaction-scoped it is released by the
        commit that ends this delivery — it cannot be leaked by a crash.
        """
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (LAUNCH_DELIVERY_LOCK_NAMESPACE, run_id),
        )
        return bool(self.env.cr.fetchone()[0])

    def _fail_run(self, run, error):
        """Record a refusal on `run`, including on a retry refused the same way.

        `_mark_failed()` is a state transition, and a run already `failed` is not
        moved — correct for idempotence, wrong here: a second attempt refused for
        a *new* reason would keep the first attempt's explanation and say nothing
        about why the retry did not go. So a run already in `failed` has its
        error restamped instead. Nothing is counted as an attempt, because nothing
        was tried.
        """
        run.ensure_one()
        if run.state == "failed":
            # `_clean_text` is idempotent enough to apply here: the redactions are
            # patterns and a length cap, and an already-clean message passes
            # through unchanged. `write()` would otherwise be the only path that
            # does not bound what it stores.
            run.write({
                "error": run._clean_text(error),
                "finished_at": fields.Datetime.now(),
            })
            return run
        return run._mark_failed(error)

    def _launch_record(self, record):
        """The one record a launch is for, or nothing.

        One launch is one event. `ensure_one()` on the wrong column would give a
        traceback instead of an explanation, and letting a recordset through
        would pick an arbitrary member as the trigger record.
        """
        if record is None or record is False:
            return None
        if len(record) != 1:
            raise UserError(_(
                "%(rule)s can launch one event at a time, and was given %(count)s "
                "records. Call it once per record.",
            ) % {"rule": self.name, "count": len(record)})
        return record

    def _check_launch_allowed(self, owner):
        """Raise unless `owner` may run this rule's agent in this rule's company.

        The layers only narrow: the agent's own group restriction (which also
        applies the connection's allowed groups), and the company wall. Nothing
        here can widen access beyond `group_turnstone_user`.
        """
        self.ensure_one()
        agent = self.sudo().agent_id
        if not agent:
            raise UserError(_("%s has no agent configured.") % self.name)
        if self.company_id not in owner.company_ids:
            raise UserError(_(
                "%(rule)s belongs to %(company)s, and %(user)s does not work in "
                "that company.",
            ) % {
                "rule": self.name,
                "company": self.company_id.display_name,
                "user": owner.display_name,
            })
        if agent.company_id != self.company_id:
            raise UserError(_(
                "%(rule)s belongs to %(company)s but its agent %(agent)s belongs "
                "to %(agent_company)s. Point the rule at an agent of its own "
                "company.",
            ) % {
                "rule": self.name,
                "company": self.company_id.display_name,
                "agent": agent.display_name,
                "agent_company": agent.company_id.display_name,
            })
        agent._check_user_allowed(owner)
        return True

    def _create_launch_session(self, owner, record=None):
        """The session row a launch sends its prompt from.

        The values are the ones `turnstone.agent.action_start_session()` copies,
        kept in step with it by hand: a shared `_prepare_session_vals()` on the
        agent model is the obvious extraction and would touch a file this ticket
        does not own. `user_id` is explicit rather than `env.user` — the launch
        runs under `sudo()` to reach the connection's token, and the session has
        to belong to the person the run is attributed to.

        The name is built from trusted configuration and identifiers only; the
        prompt never goes in here. It is built from business records, and a
        customer's name in a session title is how untrusted content ends up
        somewhere nobody thought to look.
        """
        self.ensure_one()
        agent = self.sudo().agent_id
        name = agent.name
        if record:
            name = "%s: %s,%s" % (agent.name, record._name, record.id)
        return self.env["turnstone.session"].sudo().create({
            "name": name,
            "connection_id": agent.connection_id.id,
            "agent_id": agent.id,
            "user_id": owner.id,
            "kind": agent.kind,
            "skill": agent.skill or False,
            "persona": agent.persona or False,
            "model_alias": agent.model_alias or False,
        })

    def _launch_secrets(self):
        """The literal a failure message must never keep.

        `_clean_text`'s patterns catch the shapes a client library prints, but
        only the caller knows the actual token value — and a 500 whose body
        echoes the request, or an exception that formatted the headers, puts it
        somewhere no pattern recognises.
        """
        self.ensure_one()
        connection = self.sudo().agent_id.connection_id
        return [token for token in (connection.sudo().api_token,) if token] if connection else []

    def _queued_run_prompt(self, run):
        """Re-render a queued run's prompt from its own trigger record.

        The delivery that follows a launch holds the rendered body already, so
        this is for the run nobody carried past the commit: the process died
        between `commit()` and the post-commit hook, and the run worker picks the
        `queued` row up later (#5754). Re-rendering is honest there because the
        record is still the one the run was opened for — `trigger_res_model` and
        `trigger_res_id` are frozen at create for exactly this — and the template
        is trusted configuration.

        Two cases where re-rendering is not possible, and both raise rather than
        send something half-right:

        * A **webhook** run has no trigger record, and its payload is deliberately
          not retained. Its prompt cannot be reconstructed, so the run is failed
          and the sender's retry re-delivers the event (a `failed` run is retried
          in place, never mistaken for a duplicate).
        * A rule whose prompt needs fields, for a run with no surviving record.
          `_render_prompt()` says which field it could not read.
        """
        run.ensure_one()
        record = None
        if run.trigger_res_model and run.trigger_res_id:
            if run.trigger_res_model in self.env:
                record = self.env[run.trigger_res_model].sudo().browse(
                    run.trigger_res_id,
                )
                if not record.exists():
                    record = None
        if record is None:
            raise UserError(_(
                "Run %(run)s of %(rule)s is still queued and its prompt was not "
                "retained, so it cannot be sent now. It was queued by a trigger "
                "that carries no record (a webhook delivery or a manual call); "
                "deliver the event again to launch it.",
            ) % {"run": run.display_name, "rule": self.name})
        return self._render_prompt(record)

    # -- inbound webhooks (#5755) ------------------------------------------

    @api.model
    def _webhook_secret(self):
        """The deployment's shared webhook secret, or `None` when unset.

        `ir.config_parameter` rather than a field on a rule, so one value covers
        every rule and lives where the rest of the deployment's secrets are
        (a config file, an environment variable, a secret manager injecting a
        parameter). Read under `sudo()`: the parameter table is readable by
        ordinary users in Odoo (it holds no ACL secret of its own), so this
        would work either way, but the read is not the caller's business.
        """
        value = self.env["ir.config_parameter"].sudo().get_param(WEBHOOK_SECRET_PARAM)
        return (value or "").strip() or None

    @api.model
    def _webhook_signature(self, timestamp, body):
        """The signature a sender must have presented for this exact delivery.

        Over `"<timestamp>.<raw body>"`, not over the body alone: the timestamp
        has to be signed too, or a captured request could be re-dated to escape
        the skew window and be replayed forever. The body is the **raw bytes**:
        re-serialising parsed JSON would make the check depend on this module's
        choice of separators and key order, and a sender that signed what it
        actually sent would fail.

        `@api.model` and no `ensure_one()`: the secret is one deployment-wide
        value and the signature is checked *before* a rule is resolved, which is
        the whole reason an unknown key and a bad signature answer the same.
        """
        secret = self._webhook_secret()
        if not secret:
            # Nothing can be verified. This is raised, not returned as a
            # "never matches" value: a deployment where webhooks silently never
            # authenticate is worse than one that says so.
            raise UserError(_(
                "Inbound webhooks are not configured on this database: set the "
                "system parameter %s to the shared signing secret.",
            ) % WEBHOOK_SECRET_PARAM)
        mac = hmac.new(
            secret.encode(),
            timestamp.encode() + b"." + body,
            hashlib.sha256,
        )
        return "sha256=" + mac.hexdigest()

    @api.model
    def _webhook_check_signature(self, signature, timestamp, body):
        """Raise `WebhookRefused` unless this delivery is authentic and fresh.

        Both halves are the sender's to get right, and neither answer says which
        one failed: a bad signature and a stale timestamp are both 401 with one
        body, so a prober learns nothing to iterate on.
        """
        if not signature or not timestamp:
            raise WebhookRefused(401, "unauthorized")
        # A timestamp that is not a number is simply a request that was not
        # signed the way this contract says; there is nothing to compare.
        try:
            sent_at = int(timestamp)
        except (TypeError, ValueError):
            raise WebhookRefused(401, "unauthorized") from None
        if abs(time.time() - sent_at) > WEBHOOK_MAX_SKEW_SECONDS:
            raise WebhookRefused(401, "unauthorized")
        expected = self._webhook_signature(timestamp, body)
        # `compare_digest`, not `==`: a byte-by-byte comparison leaks the length
        # of the matching prefix to anyone who can time the response, and one
        # character at a time is enough to forge a signature offline. It also
        # refuses a non-ASCII header outright (TypeError), which is the right
        # answer for a header this contract says is hex.
        try:
            matches = hmac.compare_digest(expected, signature)
        except TypeError:
            matches = False
        if not matches:
            raise WebhookRefused(401, "unauthorized")
        return True

    @api.model
    def _find_by_webhook_key(self, webhook_key):
        """The rule this URL names, or an empty recordset.

        Archived rules are excluded: archiving a rule is how it is retired, and
        a retired rule's URL must stop working without anyone having to remember
        to rotate a key. Any trigger type is *not* filtered here — a URL that
        reaches a rule whose trigger has since changed still owes the sender an
        answer, and `_webhook_accepts` gives it one.
        """
        key = (webhook_key or "").strip()
        if not key:
            return self.browse()
        return self.sudo().search([
            ("webhook_key", "=", key),
            ("active", "=", True),
        ], limit=1)

    @api.model
    def _webhook_event_id(self, headers, payload):
        """The event id this delivery is keyed on. Raises when there is none.

        The header is checked first because a proxy may rewrite the body and
        not the headers, and a sender's delivery id survives both. The body field
        is the fallback for a sender that signs nothing it does not also have to
        sign — and `delivery_id` is accepted because that is what the webhook
        providers actually call it.
        """
        for candidate in (
            headers.get(WEBHOOK_EVENT_HEADER),
            payload.get("event_id") if isinstance(payload, dict) else None,
            payload.get("delivery_id") if isinstance(payload, dict) else None,
        ):
            text = str(candidate or "").strip()
            if text:
                if len(text) > WEBHOOK_MAX_KEY_CHARS:
                    raise WebhookRefused(400, "invalid_event_id")
                return text
        raise WebhookRefused(400, "missing_event_id")

    @api.model
    def _webhook_dedupe_token(self, payload):
        """What this delivery is *about*, for the rule's repeat window, or `None`.

        The run model refuses to guess ("only the caller knows what 'the same
        thing' means"), and here the caller is this route — so the sender says: a
        `dedupe_key` in the body names the object the event is about, the way an
        `issue.number` would. Without one and with a window configured, the
        launch fails with the run model's own explanation rather than silently
        firing every time, which is what a window that never matches would do.

        Bounded like the event id, because it is also a `Char` chosen by a third
        party.
        """
        raw = payload.get("dedupe_key") if isinstance(payload, dict) else None
        text = str(raw or "").strip()
        if not text:
            return None
        return text[:WEBHOOK_MAX_KEY_CHARS]

    def _webhook_prompt(self, payload):
        """The message body this delivery sends.

        The rule's prompt template is the instruction, and it leads — except that
        `{{ }}` placeholders are rendered from the rule, not from the payload: a
        webhook rule has no target model, so any placeholder would already have
        been refused when the rule was saved (`_parse_template`).

        The payload follows as a fenced JSON block, so the instruction and the
        data stay visibly apart. That is a security decision as much as a
        readability one: an event body is third-party text, and a delivery that
        could place its own words where the instruction goes would be choosing
        what the agent (holding a server token) is asked to do. Fenced and after
        the instruction, it reads as data.

        The fence is made longer than any run of backticks **inside** the payload
        (CommonMark closes a fence only on a run at least as long), because a
        payload containing ``` would otherwise end the block early and put the
        rest of its own text back at instruction level — the exact hole the
        fence exists to close. `json.dumps` also escapes the payload's newlines
        and quotes, so what appears between the fences is one escaped string
        value per line, not re-flowed text.
        """
        self.ensure_one()
        try:
            rendered = json.dumps(payload, indent=2, sort_keys=True, default=str)
        except (TypeError, ValueError):
            rendered = repr(payload)
        if len(rendered) > WEBHOOK_MAX_PAYLOAD_CHARS:
            rendered = rendered[:WEBHOOK_MAX_PAYLOAD_CHARS] + "\n… [truncated]"
        longest = max((len(m) for m in re.findall(r"`+", rendered)), default=0)
        fence = "`" * max(3, longest + 1)
        return "%s\n\n%sjson\n%s\n%s" % (
            self._render_prompt(), fence, rendered, fence,
        )

    def _webhook_lock(self):
        """A non-blocking, transaction-scoped lock on this rule, or `False`.

        Every delivery to one rule writes the same row: `run_count` and
        `last_run_at` on the rule itself (`_note_run`). At Odoo's REPEATABLE READ
        two concurrent deliveries that both bump that counter do not serialise —
        the second gets `SerializationFailure` on *commit*, after the remote
        launch has already happened, and the whole request is re-run or lost
        (measured: 8 concurrent deliveries to one rule produced a 500 and a
        dropped event before this existed).

        A lock that makes the deliveries queue would serialise on the same row
        anyway, so this one is *non-blocking*: whoever loses tells the sender
        "busy, try again" (429) without having written anything, which is the
        one answer a webhook sender already knows how to handle. That is cheaper
        and more honest than an internal retry loop around a remote POST.

        Transaction-scoped (`pg_try_advisory_xact_lock`), so it is released by
        the commit or the rollback and cannot be leaked by a crash — the
        failure mode of a session-scoped advisory lock.
        """
        self.ensure_one()
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (WEBHOOK_LOCK_NAMESPACE, self.id),
        )
        return bool(self.env.cr.fetchone()[0])

    def _webhook_accepts(self):
        """Whether this rule can be woken by a delivery, and if not why.

        Checked before any run exists, because these are configuration
        problems: an archived rule, a trigger somebody changed, or a rule with no
        agent. The answer is recorded on the run like every other refusal, so
        the reason is visible to whoever debugs the rule — and *not* to the
        sender, who is told only that the delivery was accepted.
        """
        self.ensure_one()
        if not self.active:
            return _("the rule is archived")
        if self.trigger_type != "webhook":
            return _("its trigger is no longer Inbound webhook")
        if not self.agent_id:
            return _("it has no agent configured")
        return None

    def _deliver_webhook(self, payload, idempotency_key, dedupe_token=None):
        """Handle one authenticated delivery. Returns `(run, is_replay)`.

        The two questions a repeat has to answer differently:

        * **The same event twice** — the run's unique `(automation_id,
          idempotency_key)` is the answer, so the second delivery finds the
          first run and sends nothing. That is the replay defence; the timestamp
          window only stops a captured request being worth replaying at all.
          Reported as a replay, because a sender that gets a success for a
          re-delivery has no reason to stop re-sending it.
        * **A different event for the same sender object** — `dedupe_token`,
          when the rule has a window. Not derived from the payload here: only
          the rule's author knows what "the same thing" means for their event.

        Launch semantics are the launch service's, unchanged: a run that is
        `queued`/`running`/`done`/`skipped` is answered with itself, and a run in
        `failed` is *retried in place* rather than reported as a duplicate — a
        500 from Turnstone must not become a permanently lost event.

        Raises `WebhookRefused(429, "busy")` when another delivery to this same
        rule is in flight — see `_webhook_lock()` on why that is refused rather
        than queued.
        """
        self.ensure_one()
        # The duplicate check happens BEFORE taking the lock: a re-delivery is
        # answered from a read, and making it wait for (or fail on) a lock held
        # by an unrelated event would turn a harmless retry into an error.
        runs = self.env["turnstone.automation.run"].sudo()
        existing = runs._find_by_key(self, idempotency_key)
        if existing and existing.state != "failed":
            # Terminal or in flight. A failed run is the one exception, and the
            # launch service decides that below.
            return existing, True

        if not self._webhook_lock():
            # This rule is already handling a delivery in this instant. Nothing
            # has been written yet, so the sender's retry is a clean first
            # attempt — which is exactly why 429 is right here and 202 would not
            # be.
            raise WebhookRefused(429, "busy")

        refusal = self._webhook_accepts()
        if refusal is not None:
            run, mine = runs._open_for_event(
                self, idempotency_key, dedupe_token=dedupe_token,
            )
            if mine:
                # `failed` with no attempt counted, which is exactly how the
                # launch service records "refused before anything was tried".
                # Not `skipped`: that is terminal, and a rule somebody fixes a
                # minute later has to be able to answer the sender's retry.
                run._mark_failed(_(
                    "The webhook delivery was authenticated but not launched: "
                    "%s.",
                ) % refusal)
            return run, False

        return self._launch_for_record(
            idempotency_key=idempotency_key,
            dedupe_token=dedupe_token,
            prompt=self._webhook_prompt(payload),
        ), False

    def _deliver_webhook_once(self, payload, idempotency_key, dedupe_token=None):
        """`_deliver_webhook`, with a lost insert race answered as a replay.

        `_open_for_event()` closes the race between two callers that both find no
        run — but only for a caller that can **see** the winner. Under REPEATABLE
        READ, a transaction whose snapshot predates the winner's commit cannot:
        `_find_by_key()` finds nothing both before and inside the savepoint, and
        the insert then trips the unique constraint. Postgres's own uniqueness
        check sees the row even though the snapshot does not. That surfaced as a
        500, and a 500 to a webhook sender means "retry", for an event that is
        already recorded — measured with eight simultaneous deliveries of one
        event.

        So the answer comes from the constraint rather than from a re-read: the
        run-key unique constraint refusing this insert **is** the statement "this
        event already has a run", and it is the only thing that can say so when
        the row is invisible to this transaction. Reported as a replay; nothing
        was launched by this caller, and the row it lost to is the one that
        counts.

        Told apart by `constraint_name`, not by catching `UniqueViolation`
        broadly: the savepoint also covers the session create and the audit row,
        and a unique violation from *those* is a bug, not a duplicate. Calling it
        a duplicate would turn a real failure into a cheerful 409.
        """
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                return self._deliver_webhook(
                    payload, idempotency_key, dedupe_token=dedupe_token,
                )
        except psycopg2_errors.UniqueViolation as exc:
            constraint = getattr(exc.diag, "constraint_name", None)
            if constraint != RUN_KEY_CONSTRAINT:
                raise
            _logger.info(
                "Automation %s: delivery %s lost the insert race on %s; answered "
                "as a duplicate (the row is not visible to this transaction's "
                "snapshot).", self.name, idempotency_key, constraint,
            )
            # The row exists but this transaction cannot see it, so there is no
            # recordset to return. The caller only needs "this is a replay", and
            # the run it names is unreadable from here by definition.
            return self.env["turnstone.automation.run"].sudo().browse(), True
