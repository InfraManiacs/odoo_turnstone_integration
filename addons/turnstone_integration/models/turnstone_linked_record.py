"""Odoo records an agent touched during a session.

This is the half of the integration that makes the round trip visible. Turnstone
reaches Odoo through the Odoo MCP server, so when an agent writes a knowledge
article the write happens over JSON-RPC, outside this module entirely — Odoo has
no idea it was an agent.

What we do have is the transcript. A tool call to `mcp__odoo__odoo_create`
carries the model in its arguments and the new id in its result, so the session
can be read back into "this agent created knowledge.article 42" and the chat can
link straight to it.
"""

from odoo import _, fields, models


class TurnstoneLinkedRecord(models.Model):
    _name = "turnstone.linked.record"
    _description = "Odoo Record Touched by a Turnstone Agent"
    _order = "id desc"

    session_id = fields.Many2one(
        "turnstone.session", required=True, ondelete="cascade", index=True
    )
    res_model = fields.Char(string="Model", required=True, index=True)
    res_id = fields.Integer(string="Record ID", required=True)
    operation = fields.Selection(
        [("create", "Created"), ("write", "Updated"), ("message", "Posted to")],
        required=True,
    )
    display_name_cache = fields.Char(
        string="Record Name",
        help="Name at the time the link was built. The record may since have been "
             "renamed or deleted; the link resolves live.",
    )
    # NOT named `exists`: that would shadow BaseModel.exists(), so
    # `session.linked_record_ids.exists()` — which Odoo itself calls — would
    # return a boolean field value instead of filtering the recordset.
    record_exists = fields.Boolean(
        string="Still Exists", compute="_compute_record_exists",
        help="False when the agent's record has since been deleted. The link is kept "
             "so the history stays honest about what happened.",
    )

    _unique_link = models.Constraint(
        "unique(session_id, res_model, res_id, operation)",
        "That record is already linked to this session for that operation.",
    )

    def _compute_record_exists(self):
        for rec in self:
            model = self.env.get(rec.res_model)
            if model is None:
                # The module that defines it is not installed here.
                rec.record_exists = False
                continue
            # sudo: whether the record is still there is not a secret, and the
            # link is already scoped to the caller's own session by record rule.
            rec.record_exists = bool(model.sudo().browse(rec.res_id).exists())

    def action_open_record(self):
        """Jump to the record. Access rules apply as they would anywhere."""
        self.ensure_one()
        if self.res_model not in self.env:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "type": "warning",
                    "title": _("Model not installed"),
                    "message": _("%s does not exist on this database.") % self.res_model,
                },
            }
        return {
            "type": "ir.actions.act_window",
            "res_model": self.res_model,
            "res_id": self.res_id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }
