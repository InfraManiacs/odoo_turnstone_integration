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
    def bootstrap(self, current_session_id=None):
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
                "sessions": self._session_list(include_id=current_session_id),
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

    def _session_list(self, include_id=None):
        """Sessions for the sidebar.

        `include_id` is never optional in practice: a subagent is usually
        CLOSED by the time you open it — a coordinator calls close_all_children
        when it is done — so a plain "state != closed" list drops the very
        session the user is looking at, stranding them on a conversation that
        appears nowhere.
        """
        Session = request.env["turnstone.session"]
        domain = [("state", "!=", "closed")]
        if include_id:
            domain = ["|", ("id", "=", int(include_id))] + domain
        sessions = Session.search(domain, limit=60)

        # Pull in subagents of everything listed, whatever their state. A
        # coordinator closes its children when it is done, so filtering them out
        # would make the tree appear only while you happen to be standing on a
        # child — it would vanish the moment you went back to the parent.
        parent_ws = [s.ws_id for s in sessions if s.ws_id]
        if parent_ws:
            children = Session.search([
                ("parent_ws_id", "in", parent_ws),
                ("id", "not in", sessions.ids),
            ], limit=60)
            sessions |= children
        return [
            {
                "id": s.id,
                "name": s.name,
                "state": s.state,
                "skill": s.skill or None,
                "kind": s.kind,
                "ws_id": s.ws_id or None,
                "parent_ws_id": s.parent_ws_id or None,
                "adopted": s.adopted,
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
            return {
                "session": session._as_dict(),
                "sessions": self._session_list(include_id=session.id),
            }
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
            return {
                "session": session.action_send(body),
                "sessions": self._session_list(include_id=session.id),
            }
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

    @http.route("/turnstone/session/retry", type="jsonrpc", auth="user")
    def session_retry(self, session_id):
        try:
            session = self._session(session_id)
            session.check_access("write")
            return {"session": session.action_retry()}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/rewind", type="jsonrpc", auth="user")
    def session_rewind(self, session_id, turns=1):
        try:
            session = self._session(session_id)
            session.check_access("write")
            return {"session": session.action_rewind(turns=turns)}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/cancel_queued", type="jsonrpc", auth="user")
    def session_cancel_queued(self, session_id):
        try:
            session = self._session(session_id)
            session.check_access("write")
            return {"session": session.action_cancel_queued()}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/children", type="jsonrpc", auth="user")
    def session_children(self, session_id):
        """Subagents this session spawned."""
        try:
            session = self._session(session_id)
            children = session._children()
            # Which of them already have an Odoo record, so the UI can offer
            # "open" rather than "attach" where it applies.
            known = {
                s.ws_id: s.id
                for s in request.env["turnstone.session"].search(
                    [("ws_id", "in", [c["ws_id"] for c in children])]
                )
            }
            for child in children:
                child["odoo_session_id"] = known.get(child["ws_id"])
            return {"children": children}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/adopt", type="jsonrpc", auth="user")
    def session_adopt(self, connection_id, ws_id, name=None):
        """Join a workstream that already exists in Turnstone."""
        try:
            self._require_user()
            session = request.env["turnstone.session"].adopt(connection_id, ws_id, name=name)
            return {
                "session": session._as_dict(),
                "sessions": self._session_list(include_id=session.id),
            }
        except (UserError, AccessError) as exc:
            return self._error(exc)

    # -- live cluster view -------------------------------------------------

    @http.route("/turnstone/live", type="jsonrpc", auth="user")
    def live(self, connection_id=None, state=None):
        """What Turnstone is doing right now, across the whole cluster.

        Manager-only. The feed covers every workstream on the server regardless
        of who started it, including scheduled jobs and work from other systems,
        and a session name alone can disclose what another team is working on.
        Ordinary users get their own sessions through /turnstone/bootstrap.
        """
        try:
            if not request.env.user.has_group(
                "turnstone_integration.group_turnstone_manager"
            ):
                raise AccessError(
                    _("Only Turnstone Administrators can see cluster-wide activity.")
                )
            Connection = request.env["turnstone.connection"]
            conn = (
                Connection.browse(int(connection_id)).exists()
                if connection_id
                else Connection._default_for_user()
            )
            if not conn:
                raise UserError(_("No such Turnstone server."))
            client = conn._client()
            overview = client.cluster_overview()
            feed = client.cluster_workstreams(state=state, per_page=200)
            rows = feed.get("workstreams") or []

            known = {
                s.ws_id: s.id
                for s in request.env["turnstone.session"].sudo().search(
                    [("ws_id", "in", [r.get("id") for r in rows if r.get("id")])]
                )
            }
            return {
                "connection_id": conn.id,
                "connection_name": conn.name,
                "overview": overview,
                "workstreams": [
                    {
                        "ws_id": r.get("id"),
                        "name": r.get("title") or r.get("name") or (r.get("id") or "")[:12],
                        "state": r.get("state"),
                        "kind": r.get("kind"),
                        "parent_ws_id": r.get("parent_ws_id"),
                        "activity": r.get("activity") or "",
                        "activity_state": r.get("activity_state") or "",
                        "model": r.get("model_alias") or r.get("model"),
                        "persona": r.get("persona"),
                        "tokens": r.get("tokens") or 0,
                        "tool_calls": r.get("tool_calls") or 0,
                        "context_ratio": r.get("context_ratio") or 0,
                        "node": r.get("node"),
                        "odoo_session_id": known.get(r.get("id")),
                    }
                    for r in rows
                ],
            }
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
