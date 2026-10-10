"""One turn of a Turnstone conversation, mirrored into Odoo."""

from odoo import fields, models


class TurnstoneMessage(models.Model):
    _name = "turnstone.message"
    _description = "Turnstone Message"
    _order = "sequence, id"

    session_id = fields.Many2one(
        "turnstone.session", required=True, ondelete="cascade", index=True
    )
    # Position in the transcript as Turnstone returned it. Not a timestamp:
    # Turnstone's message list has no per-message time, and ordering by
    # create_date would reshuffle a backfilled history.
    sequence = fields.Integer(required=True, default=0, index=True)

    role = fields.Selection(
        [
            ("user", "User"),
            ("assistant", "Agent"),
            ("tool", "Tool Result"),
            ("system", "System"),
        ],
        required=True,
    )
    body = fields.Text()
    # Rendered names of the tools this turn called, for the "-> pods_list,
    # events_list" line in the chat. The full arguments stay in Turnstone.
    tool_names = fields.Char()
    is_guard_notice = fields.Boolean(
        help="A Turnstone output-guard warning rather than conversation.",
    )
