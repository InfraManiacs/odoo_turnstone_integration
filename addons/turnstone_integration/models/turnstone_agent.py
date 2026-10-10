"""A saved, named agent configuration.

WHY THIS EXISTS

Starting a session needs a server, a kind, a skill, a persona, a model, a judge
model and an auto-approve policy. Asking a user to assemble that correctly every
time guarantees two things: they will not, and no two sessions will be
comparable. Worse, the kind/skill pairing fails quietly when it is wrong — an
interactive session asked to fan out has no spawn_* tools, so it does the work
in one session and looks like it succeeded.

So an administrator defines "Kube SRE" or "Market Brief" once, and a user just
picks it. The agent is also where access is decided: `group_ids` says which Odoo
users may start it at all, which is the module's real RBAC gate.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class TurnstoneAgent(models.Model):
    _name = "turnstone.agent"
    _description = "Turnstone Agent Configuration"
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    description = fields.Text(
        help="Shown to users in the agent picker. Say what it is for and what it "
             "will touch, not how it is configured.",
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer()

    connection_id = fields.Many2one(
        "turnstone.connection", required=True, ondelete="restrict",
        default=lambda s: s.env["turnstone.connection"].search(
            [("is_default", "=", True)], limit=1
        ),
    )
    company_id = fields.Many2one(
        related="connection_id.company_id", store=True, readonly=True,
    )

    # -- what Turnstone is asked for ---------------------------------------
    kind = fields.Selection(
        [("interactive", "Interactive"), ("coordinator", "Coordinator")],
        required=True, default="interactive",
        help="Interactive: one session runs the tools itself — the right default.\n"
             "Coordinator: delegates to spawned children and synthesises their "
             "results. Required for fan-out work, and the only kind that accepts "
             "file attachments.",
    )
    # ONE skill, not many. Verified against 1.8.3: the create endpoints declare
    # `skill` as a string, and passing a list is accepted and then SILENTLY
    # IGNORED — the workstream comes back with skill_id "" and skill_version 0,
    # i.e. no skill at all and no error. An agent can still pick up further
    # skills at runtime through the `skills` tool; that is Turnstone's mechanism
    # for it, and it is not something this module can compose up front.
    skill_id = fields.Many2one(
        "turnstone.skill", string="Skill", ondelete="restrict",
        domain="[('connection_id', '=', connection_id), ('available', '=', True), "
               "'|', ('kind', '=', False), ('kind', 'in', [kind, 'any'])]",
        help="Leave empty for the server's default. The list is filtered to "
             "skills Turnstone exposes to this session kind.",
    )
    skill = fields.Char(compute="_compute_catalog_names", store=True)
    skill_kind = fields.Selection(related="skill_id.kind", readonly=True)
    persona_id = fields.Many2one(
        "turnstone.persona", string="Persona", ondelete="restrict",
        domain="[('connection_id', '=', connection_id), ('available', '=', True)]",
    )
    persona = fields.Char(compute="_compute_catalog_names", store=True)
    model_alias = fields.Char(
        string="Model",
        help="Gateway model alias, e.g. agentic-coder or dixie-flatline. Empty "
             "uses the node default.\n\n"
             "Not settable for coordinators: Turnstone's console create endpoint "
             "takes no model, so a coordinator always runs on the node default. "
             "Its children can still be routed by the coordinator itself.",
    )
    judge_model = fields.Char(
        help="Model alias that grades tool calls. Empty uses the server's "
             "judge.model setting. Pointing this at a small fast alias keeps "
             "approval latency down.",
    )

    # -- approvals ----------------------------------------------------------
    auto_approve = fields.Boolean(
        string="Auto-approve all tools",
        help="Runs every tool without asking. Tool POLICY still applies and is "
             "evaluated first, so a denied tool stays denied — but anything on "
             "'ask' will no longer stop for a human. Use sparingly.",
    )
    auto_approve_tools = fields.Char(
        string="Auto-approve these tools",
        help="Comma-separated tool names auto-approved even when the blanket "
             "flag above is off, e.g. mcp__kube__pods_list, mcp__kube__events_list. "
             "This is the safe shape: name the read-only tools so the agent does "
             "not stall on every lookup, and leave writes to the policy.",
    )

    # -- access -------------------------------------------------------------
    group_ids = fields.Many2many(
        "res.groups", "turnstone_agent_group_rel", "agent_id", "group_id",
        string="Restricted To",
        help="Which Odoo groups may start this agent. Empty means any Turnstone "
             "User. This narrows access and never widens it — Turnstone User is "
             "still required, and the connection's own restriction applies too.",
    )

    session_count = fields.Integer(compute="_compute_session_count")

    _name_company_uniq = models.Constraint(
        "unique(name, company_id)",
        "An agent with that name already exists for this company.",
    )

    @api.depends("skill_id.name", "persona_id.name")
    def _compute_catalog_names(self):
        """The strings actually sent to Turnstone.

        Stored so that a catalogue sync which loses a row cannot silently blank
        what this agent has been starting sessions with.
        """
        for rec in self:
            rec.skill = rec.skill_id.name or False
            rec.persona = rec.persona_id.name or False

    @api.onchange("connection_id")
    def _onchange_connection(self):
        if self.skill_id and self.skill_id.connection_id != self.connection_id:
            self.skill_id = False
        if self.persona_id and self.persona_id.connection_id != self.connection_id:
            self.persona_id = False

    @api.constrains("skill_id", "kind")
    def _check_skill_kind(self):
        """A coordinator skill on an interactive session fails quietly.

        It has no spawn_* tools, so it explains that it cannot delegate and does
        the work in one session instead — which looks like success.
        """
        for rec in self:
            sk = rec.skill_id
            if sk and sk.kind and sk.kind != "any" and sk.kind != rec.kind:
                raise UserError(_(
                    "The skill %(skill)s is a %(skill_kind)s skill, but this agent is "
                    "%(kind)s. Turnstone would not give it the right tools, and the "
                    "mismatch fails quietly rather than erroring."
                ) % {"skill": sk.name, "skill_kind": sk.kind, "kind": rec.kind})

    def _compute_session_count(self):
        data = self.env["turnstone.session"]._read_group(
            [("agent_id", "in", self.ids)], ["agent_id"], ["__count"]
        )
        counts = {a.id: n for a, n in data}
        for rec in self:
            rec.session_count = counts.get(rec.id, 0)

    @api.constrains("kind", "model_alias")
    def _check_coordinator_model(self):
        for rec in self:
            if rec.kind == "coordinator" and rec.model_alias:
                raise UserError(_(
                    "A coordinator cannot be pinned to a model: Turnstone's console "
                    "create endpoint takes no model, so the setting would be silently "
                    "ignored. Clear the Model field, or make this agent interactive."
                ))

    @api.onchange("kind")
    def _onchange_kind(self):
        if self.kind == "coordinator":
            self.model_alias = False

    def _check_user_allowed(self, user=None):
        """Raise unless `user` may start this agent."""
        self.ensure_one()
        user = user or self.env.user
        if not user.has_group("turnstone_integration.group_turnstone_user"):
            raise UserError(_("You do not have access to Turnstone."))
        groups = self.sudo().group_ids
        if groups and not (user.group_ids & groups):
            raise UserError(
                _("You are not permitted to use the agent %s.") % self.display_name
            )
        # The connection has its own restriction, and both must pass.
        self.sudo().connection_id._check_user_allowed(user)
        return True

    @api.model
    def _available_for_user(self, user=None):
        user = user or self.env.user
        if not user.has_group("turnstone_integration.group_turnstone_user"):
            return self.browse()
        agents = self.search([])
        return agents.filtered(
            lambda a: (not a.sudo().group_ids or (user.group_ids & a.sudo().group_ids))
            and (
                not a.sudo().connection_id.allowed_group_ids
                or (user.group_ids & a.sudo().connection_id.allowed_group_ids)
            )
        )

    def auto_approve_tool_list(self):
        self.ensure_one()
        raw = self.auto_approve_tools or ""
        return [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]

    def action_start_session(self):
        """Start a chat with this agent, from the backend."""
        self.ensure_one()
        self._check_user_allowed()
        session = self.env["turnstone.session"].create({
            "name": self.name,
            "connection_id": self.connection_id.id,
            "agent_id": self.id,
            "kind": self.kind,
            "skill": self.skill or False,
            "persona": self.persona or False,
            "model_alias": self.model_alias or False,
        })
        self.env["turnstone.audit.log"].log(
            "session_create", session=session, agent=self,
            detail=_("Started from the agent form."),
        )
        return session.action_open_chat()

    def action_view_sessions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sessions"),
            "res_model": "turnstone.session",
            "view_mode": "list,form",
            "domain": [("agent_id", "=", self.id)],
        }
