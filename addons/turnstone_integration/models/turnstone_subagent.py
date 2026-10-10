"""Child workstreams a coordinator spawned.

WHY THESE ARE PERSISTED RATHER THAN READ LIVE

Subagents are discovered from Turnstone's cluster feed, and that feed only
lists workstreams it still holds. A child that finishes and is closed by its
parent disappears from it — which, observed live, means the subagent panel
counts up to two and then back to zero, and the user is left with no record of
what actually did the work.

So each sync upserts what it sees. A child that vanishes keeps its last known
state and is marked finished, because "two subagents ran and both completed" is
the useful answer, and the alternative is a panel that silently empties.
"""

from odoo import fields, models


class TurnstoneSubagent(models.Model):
    _name = "turnstone.subagent"
    _description = "Turnstone Subagent"
    _order = "depth, id"

    session_id = fields.Many2one(
        "turnstone.session", required=True, ondelete="cascade", index=True
    )
    ws_id = fields.Char(string="Workstream ID", required=True, index=True)
    parent_ws_id = fields.Char(index=True)
    name = fields.Char()
    state = fields.Char(help="Last state Turnstone reported before it left the feed.")
    kind = fields.Char()
    persona = fields.Char()
    model_alias = fields.Char(string="Model")
    activity = fields.Char()
    tool_call_count = fields.Integer()
    token_count = fields.Integer()
    depth = fields.Integer(default=1, help="1 is a direct child of the session.")
    gone = fields.Boolean(
        string="Finished",
        help="No longer present in Turnstone's live feed — it completed and was "
             "closed, or the node released it.",
    )

    _unique_child = models.Constraint(
        "unique(session_id, ws_id)",
        "That subagent is already recorded for this session.",
    )
