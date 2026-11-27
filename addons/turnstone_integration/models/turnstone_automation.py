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
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
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
        """
        self.ensure_one()
        self.sudo().write({
            "run_count": (self.run_count or 0) + 1,
            "last_run_at": when or fields.Datetime.now(),
        })
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
        """One attempt at delivering this run's prompt. Returns the run."""
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

        # 6. Launch. The attempt is counted before the call, so a crash in the
        #    middle leaves an honest `running` rather than a run still claiming
        #    never to have tried. The savepoint matters: a database-level
        #    failure inside the session create would otherwise leave the
        #    transaction unusable and the failure could never be written down.
        run._mark_running()
        try:
            with self.env.cr.savepoint():
                session = self._create_launch_session(owner, record)
                session._ensure_started(first_message=body)
        except Exception as exc:  # noqa: BLE001 - see the class docstring on errors
            # The log line is as exposed as the run history: a log aggregator is
            # read by more people than a connection is, and the exception a 500
            # produces quotes the response body, which is where a bearer header
            # turns up. So the recorded text is what gets logged too.
            secrets = self._launch_secrets()
            cleaned = run._clean_text(exc, secrets)
            _logger.warning(
                "Automation %s: run %s failed to launch: %s", self.name, run.id, cleaned,
            )
            return run._mark_failed(exc, secrets=secrets)

        run._mark_done(session)
        self.env["turnstone.audit.log"].log(
            "session_create", session=session, agent=self.sudo().agent_id, user=owner,
            detail=_("Started by the automation %(rule)s, run %(run)s.") % {
                "rule": self.name, "run": run.id,
            },
        )
        return run

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
