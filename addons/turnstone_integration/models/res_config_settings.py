from odoo import _, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    turnstone_connection_id = fields.Many2one(
        "turnstone.connection",
        string="Default Turnstone Server",
        config_parameter="turnstone_integration.default_connection_id",
        help="Used for new chat sessions when the user does not pick one.",
    )
    turnstone_poll_interval = fields.Integer(
        string="Refresh Interval (ms)",
        default=2000,
        config_parameter="turnstone_integration.poll_interval",
        help="How often the chat asks Turnstone for new output while an agent is "
             "working. Turnstone streams over SSE, but Odoo's client polls, so this "
             "is the trade between responsiveness and request volume.",
    )
    turnstone_show_tool_calls = fields.Boolean(
        string="Show Tool Activity",
        default=True,
        config_parameter="turnstone_integration.show_tool_calls",
        help="Show which tools an agent called between its messages. Turning this "
             "off hides what the agent actually did, which makes approvals harder "
             "to judge.",
    )

    def action_open_turnstone_connections(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Turnstone Servers"),
            "res_model": "turnstone.connection",
            "view_mode": "list,form",
        }

    def action_test_turnstone(self):
        self.ensure_one()
        if not self.turnstone_connection_id:
            raise UserError(_("Pick a Turnstone server first."))
        return self.turnstone_connection_id.action_test_connection()
