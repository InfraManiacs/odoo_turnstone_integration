"""Local mirror of a Turnstone server's skills and personas.

WHY MIRROR RATHER THAN FETCH LIVE

Odoo relation fields need records. Without them every skill and persona is a
free-text box, which means typos become silent misconfiguration: Turnstone
answers "Skill not found or disabled" at session-start time, long after whoever
typed it has moved on.

These rows are a cache, never a source of truth. Turnstone owns the catalogue;
`action_sync_catalog` on the connection pulls it, and anything that disappeared
upstream is marked unavailable rather than deleted, so an agent still points at
something recognisable instead of silently losing its skill.
"""

from odoo import _, fields, models


class TurnstoneSkill(models.Model):
    _name = "turnstone.skill"
    _description = "Turnstone Skill (cached)"
    _order = "connection_id, name"

    connection_id = fields.Many2one(
        "turnstone.connection", required=True, ondelete="cascade", index=True
    )
    name = fields.Char(required=True, index=True, help="The name Turnstone knows it by.")
    template_id = fields.Char(string="Turnstone ID", readonly=True)
    description = fields.Text()
    kind = fields.Selection(
        [("interactive", "Interactive"), ("coordinator", "Coordinator"), ("any", "Any")],
        help="Which session kind Turnstone exposes this skill to. A coordinator "
             "skill run as an interactive session has no spawn tools and will "
             "quietly do the work in one session instead of delegating.",
    )
    activation = fields.Char(
        readonly=True,
        help="How Turnstone activates it. 'named' means a session must ask for it "
             "by name — either at creation, or at runtime through the skills tool.",
    )
    enabled = fields.Boolean(default=True)
    available = fields.Boolean(
        default=True,
        help="False when the skill was in a previous sync but is gone from "
             "Turnstone now. Kept so agents pointing at it still show something.",
    )
    last_sync = fields.Datetime(readonly=True)

    _name_connection_uniq = models.Constraint(
        "unique(name, connection_id)",
        "That skill is already cached for this server.",
    )

    def _compute_display_name(self):
        for rec in self:
            suffix = ""
            if rec.kind == "coordinator":
                suffix = _(" (coordinator)")
            if not rec.available:
                suffix += _(" — no longer on the server")
            rec.display_name = "%s%s" % (rec.name, suffix)


class TurnstonePersona(models.Model):
    _name = "turnstone.persona"
    _description = "Turnstone Persona (cached)"
    _order = "connection_id, display_label"

    connection_id = fields.Many2one(
        "turnstone.connection", required=True, ondelete="cascade", index=True
    )
    name = fields.Char(string="Slug", required=True, index=True)
    display_label = fields.Char(string="Name")
    description = fields.Text()
    applies_to_kinds = fields.Char(
        readonly=True,
        help="Session kinds Turnstone allows this persona on.",
    )
    is_default = fields.Boolean()
    enabled = fields.Boolean(default=True)
    available = fields.Boolean(default=True)
    last_sync = fields.Datetime(readonly=True)

    _name_connection_uniq = models.Constraint(
        "unique(name, connection_id)",
        "That persona is already cached for this server.",
    )

    def _compute_display_name(self):
        for rec in self:
            label = rec.display_label or rec.name
            rec.display_name = label if label == rec.name else "%s (%s)" % (label, rec.name)
