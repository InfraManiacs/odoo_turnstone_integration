"""JSON endpoints the chat client calls.

The OWL client never talks to Turnstone directly, for one reason that decides
the whole shape of this module: the API token is a bearer credential for an
agent harness that can reach Odoo, the cluster and anything else its MCP servers
expose. Putting it in the browser would hand every chat user the full authority
of the server token, regardless of their Odoo groups.

So the browser calls here, these methods check Odoo access, and the server calls
Turnstone.
"""

import json
import logging

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

# The header names and the refusal type are the contract with the sender, so
# they come from the model that owns it rather than being restated here. Safe at
# module level: `models/` is imported before `controllers/` in this addon's
# `__init__.py`.
from odoo.addons.turnstone_integration.models.turnstone_automation import (
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_TIMESTAMP_HEADER,
    WebhookRefused,
)

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
                or (request.env.user.group_ids & c.sudo().allowed_group_ids)
            )
            icp = request.env["ir.config_parameter"].sudo()
            return {
                "agents": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "description": a.description or "",
                        "kind": a.kind,
                        "skill": a.sudo().skill or None,
                        "persona": a.sudo().persona or None,
                        "model": a.sudo().model_alias or None,
                        "connection_id": a.connection_id.id,
                        "connection_name": a.connection_id.name,
                        "supports_attachments": a.kind == "coordinator",
                        "color": a.color,
                    }
                    for a in request.env["turnstone.agent"]._available_for_user()
                ],
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
            # /v1/api/models is empty when aliases come from the node's TOML
            # rather than the DB, which is this deployment's shape. Harvest the
            # aliases actually in use so the picker suggests something real, and
            # still allow free text.
            aliases = set()
            try:
                for row in (client.cluster_workstreams(per_page=200).get("workstreams") or []):
                    alias = row.get("model_alias") or row.get("model")
                    if alias:
                        aliases.add(alias)
            except UserError:
                pass
            for extra in (conn.sudo().default_model,):
                if extra:
                    aliases.add(extra)
            return {
                "models": sorted(aliases),
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
                        "applies_to_kinds": (
                            ", ".join(p["applies_to_kinds"])
                            if isinstance(p.get("applies_to_kinds"), list)
                            else (p.get("applies_to_kinds") or "")
                        ),
                    }
                    for p in personas
                    if p.get("enabled", True)
                ],
            }
        except (UserError, AccessError) as exc:
            return self._error(exc)

    # -- sessions ----------------------------------------------------------

    @http.route("/turnstone/session/new", type="jsonrpc", auth="user")
    def session_new(self, connection_id=None, name=None, skill=None, persona=None,
                    model=None, kind=None):
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
            if kind not in ("interactive", "coordinator"):
                kind = "interactive"
            session = request.env["turnstone.session"].create({
                "name": name or _("New session"),
                "connection_id": conn.id,
                "skill": skill or False,
                "persona": persona or False,
                "model_alias": model or False,
                "kind": kind,
            })
            request.env["turnstone.audit.log"].log("session_create", session=session)
            # The workstream itself is created lazily on the first message, so
            # opening the composer and changing your mind costs nothing on the
            # Turnstone side.
            return {
                "session": session._as_dict(),
                "sessions": self._session_list(include_id=session.id),
            }
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/new_from_agent", type="jsonrpc", auth="user")
    def session_new_from_agent(self, agent_id, name=None):
        """Start a session from a saved agent configuration.

        This is the path users should normally take: the agent already carries a
        valid kind/skill/persona/model combination, so the quiet failure mode
        (an interactive session asked to fan out) cannot happen by accident.
        """
        try:
            self._require_user()
            agent = request.env["turnstone.agent"].browse(int(agent_id)).exists()
            if not agent:
                raise UserError(_("No such agent."))
            try:
                agent._check_user_allowed()
            except UserError:
                request.env["turnstone.audit.log"].log(
                    "access_denied", agent=agent,
                    detail=_("Tried to start an agent they are not permitted to use."),
                )
                raise
            session = request.env["turnstone.session"].create({
                "name": name or agent.name,
                "connection_id": agent.connection_id.id,
                "agent_id": agent.id,
                "kind": agent.kind,
                "skill": agent.sudo().skill or False,
                "persona": agent.sudo().persona or False,
                "model_alias": agent.sudo().model_alias or False,
            })
            request.env["turnstone.audit.log"].log(
                "session_create", session=session, agent=agent,
            )
            return {
                "session": session._as_dict(),
                "sessions": self._session_list(include_id=session.id),
            }
        except (UserError, AccessError) as exc:
            return self._error(exc)

    # -- attachments -------------------------------------------------------

    @http.route("/turnstone/session/attachments", type="jsonrpc", auth="user")
    def session_attachments(self, session_id):
        try:
            session = self._session(session_id)
            return {
                "supports_attachments": session.supports_attachments,
                "attachments": session.attachments(),
            }
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/attachment/add", type="jsonrpc", auth="user")
    def session_attachment_add(self, session_id, filename, content, mime_type=None):
        try:
            session = self._session(session_id)
            session.check_access("write")
            result = session.action_add_attachment(filename, content, mime_type)
            return {"attachment": result, "attachments": session.attachments()}
        except (UserError, AccessError) as exc:
            return self._error(exc)

    @http.route("/turnstone/session/attachment/remove", type="jsonrpc", auth="user")
    def session_attachment_remove(self, session_id, attachment_id):
        try:
            session = self._session(session_id)
            session.check_access("write")
            session.action_remove_attachment(attachment_id)
            return {"attachments": session.attachments()}
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


class TurnstoneWebhookController(http.Controller):
    """The inbound side: an external system hands us a signed event (OTI-5755).

    WHY THIS IS NOT `type="json"`

    The ticket asked for a `type="json"` route. In Odoo 19 that name is a
    deprecated alias for `jsonrpc` (`odoo/http.py`, the `@route` decorator), and
    a jsonrpc endpoint cannot satisfy two of the ticket's own criteria:

    * its dispatcher turns every exception into an HTTP **200** with a
      `serialize_exception()` body — including a full traceback — so "bad
      signature and replay are rejected with 4xx" is unreachable, and a
      traceback is exactly the configuration disclosure criterion 3 forbids;
    * it reads `params` out of a JSON-RPC envelope, which no ordinary webhook
      sender produces.

    So this is a `type="http"` route that answers with JSON. That is also the
    only type in 19.0 whose handler sees the **raw request body**, and an HMAC
    has to be checked over the exact bytes that were sent: re-serialising parsed
    JSON would make verification depend on key order and separators, and a
    sender that signed what it actually sent would fail.

    WHAT A CALLER CAN LEARN

    Nothing it did not already know. Every answer is one of a fixed set of
    `{"status": ...}` bodies, with no rule name, model, field, agent, id or
    exception text:

    * the signature is checked **before** the path is resolved, so an unknown
      key and a wrong signature are the same 401 — a caller cannot enumerate
      rules by watching which URLs answer differently;
    * a delivery for a rule that is archived, re-triggered or agentless is
      accepted (202) and recorded as a failed run for an administrator to read;
      the sender is not told that an internal rule refused it;
    * a genuinely malformed delivery (no event id, an oversized body) is a 400,
      which says only that the *request* was malformed;
    * a delivery that arrives while the same rule is already handling one is a
      429 with `Retry-After`, because that is the honest answer — it will work
      when the sender retries, and the alternative (letting both through) is
      either a lost event or a duplicated one.

    AUTH AND SESSION

    `auth="public"` because the caller is a machine with no Odoo session; the
    signature is the authentication. `csrf=False` is required for the same
    reason (there is no browser session to carry a token, and the HMAC binds the
    body far more tightly than a CSRF token would). `save_session=False` so
    answering a webhook never writes a session or a cookie: a hundred deliveries
    should leave no trace in `ir.sessions`.
    """

    #: How much of a body we accept at all, enforced by werkzeug before any
    #: parsing. A webhook payload is an event, not a document.
    MAX_BODY_BYTES = 256 * 1024

    @http.route(
        "/turnstone/webhook/<string:webhook_key>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
        max_content_length=MAX_BODY_BYTES,
    )
    def webhook(self, webhook_key, **kwargs):
        try:
            # `get_data()` is the RAW body, which is what the sender signed. Not
            # `request.params`: by the time the http dispatcher has filled those
            # in, the bytes are parsed and re-encoded, and re-serialising JSON
            # changes key order and separators.
            raw = request.httprequest.get_data()
            result = self._handle_delivery(
                raw,
                webhook_key,
                signature=request.httprequest.headers.get(WEBHOOK_SIGNATURE_HEADER),
                timestamp=request.httprequest.headers.get(WEBHOOK_TIMESTAMP_HEADER),
                headers=request.httprequest.headers,
            )
        except WebhookRefused as refusal:
            _logger.info(
                "Turnstone webhook refused (%s %s) from %s",
                refusal.status, refusal.code, request.httprequest.remote_addr,
            )
            return self._reply(refusal.status, refusal.code)
        except Exception:  # noqa: BLE001 - an external caller sees nothing
            # A bug here must not become a 200 the sender treats as delivered,
            # nor a traceback in the response. It is logged (for us) and
            # answered with a bare 500, so the sender's retry behaviour is "try
            # again later" rather than "stop".
            _logger.exception("Turnstone webhook delivery failed unexpectedly")
            return self._reply(500, "error")
        return self._reply(*result)

    @staticmethod
    def _reply(status, code):
        """One JSON body, and nothing else.

        Fixed vocabulary: `status` is what the sender acts on, `code` is a
        machine-readable reason from a closed set. No ids, no names, no messages
        from this database — see the class docstring. `no-store` because a
        proxy caching one delivery's answer and replaying it to the next would
        turn "duplicate" into a lie.
        """
        return request.make_json_response(
            {"status": code},
            headers={
                "Cache-Control": "no-store",
                # 429 is the one answer that means "this will work in a moment"
                # rather than "this will never work", so it is the one answer
                # that says how long a moment is.
                **({"Retry-After": "1"} if status == 429 else {}),
            },
            status=status,
        )

    def _handle_delivery(self, raw, webhook_key, *, signature, timestamp, headers):
        """Verify, resolve, launch. Returns `(http_status, code)`.

        Split out of the endpoint so the failure paths are one `except` and the
        success paths read as the contract they are.
        """
        Automation = request.env["turnstone.automation"].sudo()

        # 0. Is this deployment able to authenticate anything at all? Checked
        #    explicitly rather than left to `_webhook_signature`, which raises a
        #    UserError: an unset secret is our misconfiguration, not the
        #    sender's fault, and it must not reach the catch-all as a 500 with a
        #    logged traceback on every delivery. The sender is told the same
        #    thing it is told for a wrong signature.
        if not Automation._webhook_secret():
            _logger.error(
                "Turnstone webhook received but webhook_secret is not set on this "
                "database, so nothing can be verified. Set the system parameter "
                "turnstone_integration.webhook_secret.",
            )
            raise WebhookRefused(401, "unauthorized")

        # 1. Authenticity, BEFORE the key is looked up. If this came after, an
        #    unauthenticated caller could tell a real key from a fake one by the
        #    difference in the answer, and enumerate rules. The signature does
        #    not need a rule: the secret is one deployment-wide value.
        Automation._webhook_check_signature(signature, timestamp, raw)

        # 2. The body. A JSON object, or a 400 — a payload that is not JSON is a
        #    sender bug, and the error must not quote it back.
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise WebhookRefused(400, "invalid_payload") from None
        if not isinstance(payload, dict):
            raise WebhookRefused(400, "invalid_payload")

        # 3. The rule. An unknown key is a 404 *after* authentication, which is
        #    the right thing to tell an authenticated caller: its URL is stale
        #    (rotated, or the rule was deleted). An unauthenticated one never
        #    reaches this line, so it cannot use this answer to probe keys.
        automation = Automation._find_by_webhook_key(webhook_key)
        if not automation:
            raise WebhookRefused(404, "unknown_endpoint")

        # 4. The event id. Required: it IS the replay defence, so a delivery
        #    without one cannot be accepted safely. Minting a random key here
        #    instead would make every retry a new run, which is the duplicate
        #    this route exists to prevent.
        event_id = Automation._webhook_event_id(headers, payload)

        # 5. Deliver. A duplicate is 409: a success for a re-delivery tells the
        #    sender its retry worked when nothing happened, and a sender that
        #    retries on success keeps retrying.
        run, replayed = automation._deliver_webhook_once(
            payload, event_id,
            dedupe_token=Automation._webhook_dedupe_token(payload),
        )
        if replayed:
            # `run` can be an empty recordset here: a caller that lost the insert
            # race cannot see the winning row (REPEATABLE READ), and the
            # constraint was the only evidence. So nothing here reads a field.
            _logger.info(
                "Turnstone webhook %s is a duplicate of run %s; nothing sent",
                event_id, run.id or "(not visible to this transaction)",
            )
            return 409, "duplicate_event"
        if run.state in ("failed", "skipped"):
            # The event was accepted and recorded; the launch did not land. The
            # sender is told 202 anyway: "failed" here can mean a permission or
            # a transport problem on our side, neither of which the sender can
            # fix, and telling it "retry" for a configuration error it must not
            # see would be worse than useless. The run is where the truth is.
            _logger.info(
                "Turnstone webhook %s recorded run %s in state %s",
                event_id, run.id, run.state,
            )
        return 202, "accepted"
