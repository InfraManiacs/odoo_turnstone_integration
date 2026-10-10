"""What Odoo users did in this module.

Turnstone keeps its own audit of tool calls and verdicts, but it sees one
identity — the server API token — so it cannot answer "which of our people
approved that restart". That question is only answerable here, which is the
whole reason this table exists.

Append-only by construction: no write, no unlink, no editable fields in any
view. A log that can be tidied up answers nothing.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class TurnstoneAuditLog(models.Model):
    _name = "turnstone.audit.log"
    _description = "Turnstone Audit Log"
    _order = "create_date desc, id desc"
    _rec_name = "action"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="restrict",
        default=lambda s: s.env.user,
    )
    action = fields.Selection(
        [
            ("session_create", "Started a session"),
            ("session_adopt", "Attached to a running job"),
            ("message_send", "Sent a message"),
            ("tool_approve", "Approved a tool call"),
            ("tool_deny", "Denied a tool call"),
            ("generation_cancel", "Stopped a turn"),
            ("session_retry", "Retried a turn"),
            ("session_rewind", "Rewound a turn"),
            ("session_close", "Closed a session"),
            ("attachment_add", "Attached a file"),
            ("attachment_remove", "Removed an attachment"),
            ("connection_test", "Tested a connection"),
            ("access_denied", "Was refused access"),
        ],
        required=True, index=True,
    )
    agent_id = fields.Many2one("turnstone.agent", ondelete="set null", index=True)
    session_id = fields.Many2one("turnstone.session", ondelete="set null", index=True)
    connection_id = fields.Many2one("turnstone.connection", ondelete="set null")
    # Kept as plain text as well as a link: the session may be deleted later, and
    # the point of an audit row is that it still means something afterwards.
    ws_id = fields.Char(string="Workstream", index=True)
    session_name = fields.Char()
    agent_name = fields.Char()
    tool_name = fields.Char(index=True)
    detail = fields.Text()

    @api.model
    def log(self, action, session=None, agent=None, connection=None,
            tool=None, detail=None, user=None):
        """Record one action. Never raises — a failed log must not fail the act.

        sudo() because an ordinary Turnstone User has no write access to this
        model, which is deliberate: they must not be able to forge or amend
        their own trail.
        """
        try:
            vals = {
                "user_id": (user or self.env.user).id,
                "action": action,
                "tool_name": tool or False,
                "detail": detail or False,
            }
            if session:
                vals.update({
                    "session_id": session.id,
                    "session_name": session.name,
                    "ws_id": session.ws_id or False,
                    "connection_id": session.connection_id.id,
                })
                if session.agent_id:
                    agent = agent or session.agent_id
            if agent:
                vals.update({"agent_id": agent.id, "agent_name": agent.name})
            if connection and not vals.get("connection_id"):
                vals["connection_id"] = connection.id
            return self.sudo().create(vals)
        except Exception:  # noqa: BLE001 - logging must never break the action
            self.env.cr.rollback()
            return self.browse()

    def write(self, vals):
        raise UserError(_("Audit entries cannot be edited."))

    def unlink(self):
        raise UserError(_("Audit entries cannot be deleted."))
