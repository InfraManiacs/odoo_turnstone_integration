"""JSON endpoints the chat client calls.

The OWL client never talks to Turnstone directly, for one reason that decides
the whole shape of this module: the API token is a bearer credential for an
agent harness that can reach Odoo, the cluster and anything else its MCP servers
expose. Putting it in the browser would hand every chat user the full authority
of the server token, regardless of their Odoo groups.

So the browser calls here, these methods check Odoo access, and the server calls
Turnstone.
"""

import logging

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


class TurnstoneController(http.Controller):

    # -- helpers -----------------------------------------------------------

    def _require_user(self):
        if not request.env.user.has_group("turnstone_integration.group_turnstone_user"):
            raise AccessError(_("You do not have access to Turnstone."))

    def _session(self, session_id):
        """Fetch a session the caller is allowed to see.

        browse + check_access rather than sudo: the record rules in
        security/turnstone_security.xml are the access decision, and going
        around them here would quietly make every session public.
        """
        self._require_user()
        session = request.env["turnstone.session"].browse(int(session_id)).exists()
        if not session:
            raise UserError(_("That Turnstone session no longer exists."))
        session.check_access("read")
        return session

    @staticmethod
    def _error(exc):
        _logger.info("Turnstone request failed: %s", exc)
        return {"error": str(exc)}

    # -- catalogue ---------------------------------------------------------

    @http.route("/turnstone/bootstrap", type="jsonrpc", auth="user")
    def bootstrap(self):
        """Everything the client needs to render before the user does anything."""
        try:
            self._require_user()
            Connection = request.env["turnstone.connection"]
            conns = Connection.search([])
            usable = conns.filtered(
                lambda c: not c.sudo().allowed_group_ids
                or (request.env.user.groups_id & c.sudo().allowed_group_ids)
            )
            icp = request.env["ir.config_parameter"].sudo()
            return {
                "connections": [
                    {
                        "id": c.id,
                        "name": c.name,
                        "is_default": c.is_default,
                        "default_skill": c.sudo().default_skill or None,
                        "default_persona": c.sudo().default_persona or None,
                    }
                    for c in usable
                ],
                "sessions": self._session_list(),
                "is_manager": request.env.user.has_group(
                    "turnstone_integration.group_turnstone_manager"
                ),
                "poll_interval": int(
                    icp.get_param("turnstone_integration.poll_interval", 2000)
                ),
                "show_tool_calls": icp.get_param(
                    "turnstone_integration.show_tool_calls", "True"
                ) in ("True", "true", "1", True),
            }
        except (UserError, AccessError) as exc:
            return self._error(exc)

    def _session_list(self):
        sessions = request.env["turnstone.session"].search(
            [("state", "!=", "closed")], limit=50
        )
        return [
            {
                "id": s.id,
                "name": s.name,
                "state": s.state,
                "skill": s.skill or None,
                "has_pending_approval": s.has_pending_approval,
                "last_activity": s.last_activity and s.last_activity.isoformat(),
                "connection_name": s.connection_id.name,
            }
            for s in sessions
        ]

    @http.route("/turnstone/catalog", type="jsonrpc", auth="user")
    def catalog(self, connection_id):
        """Skills and personas available on a server, for the new-session form."""
        try:
            self._require_user()
            conn = request.env["turnstone.connection"].browse(int(connection_id)).exists()
            if not conn:
                raise UserError(_("No such Turnstone server."))
            conn._check_user_allowed()
            client = conn._client()
            skills = client.skills()
            personas = client.personas()
            return {
                "skills": [
                    {
                        "name": s.get("name"),
                        "description": (s.get("description") or "")[:200],
                        "kind": s.get("kind"),
                    }
                    for s in skills
                    if not s.get("hidden_from_menu")
                ],
                "personas": [
                    {
                        "name": p.get("name"),
                        "display_name": p.get("display_name") or p.get("name"),
                        "description": (p.get("description") or "")[:200],
                    }
                    for p in personas
                    if p.get("enabled", True)
                ],
            }
        except (UserError, AccessError) as exc:
            return self._error(exc)

    # -- sessions ----------------------------------------------------------

    @http.route("/turnstone/session/new", type="jsonrpc", auth="user")
    def session_new(self, connection_id=None, name=None, skill=None, persona=None, model=None):
        try:
            self._require_user()
            Connection = request.env["turnstone.connection"]
            conn = (
                Connection.browse(int(connection_id)).exists()
                if connection_id
                else Connection._default_for_user()
            )
            if not conn:
                raise UserError(_("No such Turnstone server."))
            conn._check_user_allowed()
            session = request.env["turnstone.session"].create({
                "name": name or _("New session"),
                "connection_id": conn.id,
                "skill": skill or False,
                "persona": persona or False,
                "model_alias": model or False,
            })
            # The workstream itself is created lazily on the first message, so
            # opening the composer and changing your mind costs nothing on the
            # Turnstone side.
            return {"session": session._as_dict(), "sessions": self._session_list()}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/get", type="jsonrpc", auth="user")
    def session_get(self, session_id, sync=True):
        try:
            session = self._session(session_id)
            if sync and session.ws_id:
                return {"session": session.sync_from_server()}
            return {"session": session._as_dict()}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/send", type="jsonrpc", auth="user")
    def session_send(self, session_id, body):
        try:
            session = self._session(session_id)
            session.check_access("write")
            return {"session": session.action_send(body), "sessions": self._session_list()}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/approve", type="jsonrpc", auth="user")
    def session_approve(self, session_id, approved, feedback=None):
        try:
            session = self._session(session_id)
            session.check_access("write")
            return {"session": session.action_approve(approved, feedback=feedback)}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/cancel", type="jsonrpc", auth="user")
    def session_cancel(self, session_id):
        try:
            session = self._session(session_id)
            session.check_access("write")
            return {"session": session.action_cancel_generation()}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/close", type="jsonrpc", auth="user")
    def session_close(self, session_id):
        try:
            session = self._session(session_id)
            session.check_access("write")
            session.action_close()
            return {"sessions": self._session_list()}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    # -- schedules ---------------------------------------------------------

    @http.route("/turnstone/schedules", type="jsonrpc", auth="user")
    def schedules(self, connection_id=None):
        """Turnstone's scheduled tasks. Read-only, and managers only.

        A schedule is server-wide configuration, not the caller's own work, and
        its payload names skills and personas that an ordinary chat user has no
        reason to enumerate.
        """
        try:
            if not request.env.user.has_group(
                "turnstone_integration.group_turnstone_manager"
            ):
                raise AccessError(_("Only Turnstone Administrators can view schedules."))
            Connection = request.env["turnstone.connection"]
            conn = (
                Connection.browse(int(connection_id)).exists()
                if connection_id
                else Connection._default_for_user()
            )
            tasks = conn._client().schedules()
            return {
                "connection_id": conn.id,
                "schedules": [
                    {
                        "task_id": t.get("task_id") or t.get("id"),
                        "name": t.get("name"),
                        "cron": t.get("cron") or t.get("cron_expression"),
                        "timezone": t.get("timezone"),
                        "skill": t.get("skill"),
                        "persona": t.get("persona"),
                        "enabled": t.get("enabled", True),
                        "last_run": t.get("last_run") or t.get("last_run_at"),
                        "next_run": t.get("next_run") or t.get("next_run_at"),
                    }
                    for t in tasks
                ],
            }
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/schedule/runs", type="jsonrpc", auth="user")
    def schedule_runs(self, connection_id, task_id):
        try:
            if not request.env.user.has_group(
                "turnstone_integration.group_turnstone_manager"
            ):
                raise AccessError(_("Only Turnstone Administrators can view schedules."))
            conn = request.env["turnstone.connection"].browse(int(connection_id)).exists()
            if not conn:
                raise UserError(_("No such Turnstone server."))
            return {"runs": conn._client().schedule_runs(task_id)}
        except (UserError, AccessError) as exc:
            return self._error(exc)
