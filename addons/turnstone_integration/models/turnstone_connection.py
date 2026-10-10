"""A Turnstone server Odoo can talk to."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .turnstone_client import TurnstoneClient


class TurnstoneConnection(models.Model):
    _name = "turnstone.connection"
    _description = "Turnstone Server Connection"
    _order = "sequence, id"

    name = fields.Char(required=True, help="How this server is referred to in the UI.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    base_url = fields.Char(
        string="Server URL",
        required=True,
        help="Root URL of the Turnstone console, e.g. https://turnstone.example.com. "
             "Do not include /v1/api — the client appends it.",
    )
    api_token = fields.Char(
        string="API Token",
        required=True,
        groups="turnstone_integration.group_turnstone_manager",
        help="Turnstone API token. Readable only by Turnstone Administrators, and never "
             "sent to the browser — the server calls Turnstone on the user's behalf.",
    )

    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company, required=True
    )
    is_default = fields.Boolean(
        string="Default",
        help="Used for new chat sessions when the user does not pick a server.",
    )

    skill_ids = fields.One2many("turnstone.skill", "connection_id")
    persona_ids = fields.One2many("turnstone.persona", "connection_id")
    skill_count = fields.Integer(compute="_compute_catalog_counts")
    persona_count = fields.Integer(compute="_compute_catalog_counts")
    catalog_sync = fields.Datetime(string="Catalogue Synced", readonly=True)

    default_skill_id = fields.Many2one(
        "turnstone.skill", string="Default Skill",
        domain="[('connection_id', '=', id), ('available', '=', True)]",
        ondelete="set null",
        help="Applied to new sessions when none is chosen.",
    )
    default_persona_id = fields.Many2one(
        "turnstone.persona", string="Default Persona",
        domain="[('connection_id', '=', id), ('available', '=', True)]",
        ondelete="set null",
    )
    # The names actually sent to Turnstone. Kept as stored fields so a sync that
    # loses a row cannot silently blank what sessions were being created with.
    default_skill = fields.Char(compute="_compute_default_names", store=True)
    default_persona = fields.Char(compute="_compute_default_names", store=True)
    default_model = fields.Char(
        help="Model alias for new sessions. Leave empty to use the node default.",
    )

    allowed_group_ids = fields.Many2many(
        "res.groups",
        string="Restricted To",
        help="If set, only members of these groups may start sessions on this server. "
             "Empty means any Turnstone User may. This narrows access; it never widens "
             "it beyond the Turnstone User group.",
    )

    session_count = fields.Integer(compute="_compute_session_count")
    status = fields.Char(readonly=True, help="Result of the last connection test.")
    status_ok = fields.Boolean(readonly=True)

    _base_url_company_uniq = models.Constraint(
        "unique(base_url, company_id)",
        "A connection to that Turnstone URL already exists for this company.",
    )

    @api.depends("default_skill_id.name", "default_persona_id.name")
    def _compute_default_names(self):
        for rec in self:
            rec.default_skill = rec.default_skill_id.name or False
            rec.default_persona = rec.default_persona_id.name or False

    def _compute_catalog_counts(self):
        for rec in self:
            rec.skill_count = len(rec.skill_ids.filtered("available"))
            rec.persona_count = len(rec.persona_ids.filtered("available"))

    def action_sync_catalog(self):
        """Pull skills and personas from Turnstone into local records.

        Upsert, never replace. A skill that has gone from the server is flagged
        unavailable rather than deleted, so an agent still points at a named
        thing instead of silently losing its configuration.
        """
        self.ensure_one()
        client = self._client()
        Skill = self.env["turnstone.skill"].sudo()
        Persona = self.env["turnstone.persona"].sudo()
        now = fields.Datetime.now()

        seen_skills = []
        for row in client.skills():
            name = row.get("name")
            if not name:
                continue
            seen_skills.append(name)
            vals = {
                "description": (row.get("description") or "")[:2000],
                "kind": row.get("kind") if row.get("kind") in
                        ("interactive", "coordinator", "any") else False,
                "activation": row.get("activation") or False,
                "template_id": row.get("template_id") or False,
                "enabled": bool(row.get("enabled", True)),
                "available": True,
                "last_sync": now,
            }
            existing = Skill.search(
                [("connection_id", "=", self.id), ("name", "=", name)], limit=1
            )
            if existing:
                existing.write(vals)
            else:
                Skill.create(dict(vals, connection_id=self.id, name=name))
        Skill.search([
            ("connection_id", "=", self.id), ("name", "not in", seen_skills),
        ]).write({"available": False})

        seen_personas = []
        for row in client.personas():
            name = row.get("name")
            if not name:
                continue
            seen_personas.append(name)
            kinds = row.get("applies_to_kinds")
            vals = {
                "display_label": row.get("display_name") or name,
                "description": (row.get("description") or "")[:2000],
                "applies_to_kinds": ", ".join(kinds) if isinstance(kinds, list) else (kinds or False),
                "is_default": bool(row.get("is_default")),
                "enabled": bool(row.get("enabled", True)),
                "available": True,
                "last_sync": now,
            }
            existing = Persona.search(
                [("connection_id", "=", self.id), ("name", "=", name)], limit=1
            )
            if existing:
                existing.write(vals)
            else:
                Persona.create(dict(vals, connection_id=self.id, name=name))
        Persona.search([
            ("connection_id", "=", self.id), ("name", "not in", seen_personas),
        ]).write({"available": False})

        self.sudo().catalog_sync = now
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Catalogue synced"),
                "message": _("%(s)s skills and %(p)s personas from %(name)s.")
                % {"s": len(seen_skills), "p": len(seen_personas), "name": self.name},
                "sticky": False,
            },
        }

    def _compute_session_count(self):
        data = self.env["turnstone.session"]._read_group(
            [("connection_id", "in", self.ids)], ["connection_id"], ["__count"]
        )
        counts = {c.id: n for c, n in data}
        for rec in self:
            rec.session_count = counts.get(rec.id, 0)

    @api.constrains("is_default", "company_id")
    def _check_single_default(self):
        for rec in self.filtered("is_default"):
            other = self.search([
                ("is_default", "=", True),
                ("company_id", "=", rec.company_id.id),
                ("id", "!=", rec.id),
            ], limit=1)
            if other:
                raise UserError(
                    _("%(other)s is already the default connection for %(company)s. "
                      "Clear it first.")
                    % {"other": other.name, "company": rec.company_id.name}
                )

    def _client(self, timeout=None):
        """Build an API client. sudo() because api_token is group-restricted:
        an ordinary Turnstone User must be able to chat without being able to
        read the token."""
        self.ensure_one()
        rec = self.sudo()
        return TurnstoneClient(rec.base_url, rec.api_token, timeout=timeout or 30)

    def _check_user_allowed(self, user=None):
        """Raise unless `user` may start sessions here."""
        self.ensure_one()
        user = user or self.env.user
        if not user.has_group("turnstone_integration.group_turnstone_user"):
            raise UserError(_("You do not have access to Turnstone."))
        allowed = self.sudo().allowed_group_ids
        if allowed and not (user.group_ids & allowed):
            raise UserError(
                _("You are not permitted to use the Turnstone server %s.") % self.display_name
            )
        return True

    @api.model
    def _default_for_user(self, user=None):
        """The connection a new session should use, honouring per-connection groups."""
        user = user or self.env.user
        candidates = self.search([("company_id", "in", self.env.companies.ids)])
        usable = candidates.filtered(
            lambda c: not c.sudo().allowed_group_ids or (user.group_ids & c.sudo().allowed_group_ids)
        )
        if not usable:
            raise UserError(
                _("No Turnstone server is configured, or none you are permitted to use. "
                  "Ask a Turnstone Administrator to set one up under Settings.")
            )
        return usable.filtered("is_default")[:1] or usable[:1]

    def action_test_connection(self):
        """Verify URL + token, and record what the token can actually do."""
        self.ensure_one()
        client = self._client()
        try:
            health = client.health()
            who = client.whoami()
        except UserError as exc:
            self.sudo().write({"status": str(exc), "status_ok": False})
            raise
        perms = (who.get("permissions") or "").split(",")
        self.sudo().write({
            "status": _("OK — connected as %(user)s, %(perms)s permissions, Turnstone %(ver)s")
            % {
                "user": who.get("username") or who.get("user_id"),
                "perms": len([p for p in perms if p]),
                "ver": ", ".join(health.get("versions") or []) or health.get("status", "?"),
            },
            "status_ok": True,
        })
        # The first thing anyone wants after a successful test is the catalogue,
        # and doing it here means skills are selectable immediately.
        try:
            self.action_sync_catalog()
        except UserError:
            pass
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Turnstone reachable"),
                "message": self.sudo().status,
                "sticky": False,
            },
        }

    def action_view_sessions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sessions"),
            "res_model": "turnstone.session",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
            "context": {"default_connection_id": self.id},
        }
