"""An Odoo-side mirror of a Turnstone workstream.

Turnstone owns the conversation. This record exists so the session is
searchable, access-controlled and linkable like any other Odoo record, and so
the chat UI has somewhere to hang. Every field that describes agent state is
refreshed from Turnstone rather than maintained here — if the two disagree,
Turnstone is right.
"""

import base64
import json
import logging
import mimetypes
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .turnstone_client import TurnstoneError, TurnstoneNotFound

_logger = logging.getLogger(__name__)

# Odoo MCP tools that mean "something changed in Odoo". Mapped to the operation
# we record so the chat can say "Created knowledge.article 42" rather than
# "called a tool".
ODOO_WRITE_TOOLS = {
    "mcp__odoo__odoo_create": "create",
    "mcp__odoo__odoo_write": "write",
    "mcp__odoo__odoo_message_post": "message",
}


class TurnstoneSession(models.Model):
    _name = "turnstone.session"
    _description = "Turnstone Chat Session"
    _order = "last_activity desc, id desc"
    _rec_name = "name"

    name = fields.Char(required=True, default=lambda s: _("New session"))
    ws_id = fields.Char(
        string="Workstream ID", readonly=True, index=True, copy=False,
        help="Turnstone's id for this session. Empty until the first message is sent.",
    )
    agent_id = fields.Many2one(
        "turnstone.agent", ondelete="set null", index=True,
        help="The saved configuration this session was started from.",
    )
    supports_attachments = fields.Boolean(
        compute="_compute_supports_attachments",
        help="Turnstone accepts attachments on coordinator workstreams only.",
    )
    connection_id = fields.Many2one(
        "turnstone.connection", required=True, ondelete="restrict",
        default=lambda s: s.env["turnstone.connection"]._default_for_user(),
    )
    user_id = fields.Many2one(
        "res.users", string="Owner", required=True, default=lambda s: s.env.user,
        index=True, ondelete="cascade",
    )

    skill = fields.Char(help="Turnstone skill driving this session.")
    persona = fields.Char()
    model_alias = fields.Char(string="Model")

    state = fields.Selection(
        [
            ("draft", "Not started"),
            ("idle", "Ready"),
            ("thinking", "Working"),
            ("attention", "Waiting for you"),
            ("error", "Error"),
            ("closed", "Closed"),
        ],
        default="draft", readonly=True, index=True,
    )
    node_id = fields.Char(readonly=True, help="Turnstone node hosting this session.")
    kind = fields.Selection(
        [("interactive", "Interactive"), ("coordinator", "Coordinator")],
        default="interactive", required=True,
        help="Chosen when the session starts, and fixed once the workstream exists.\n\n"
             "Interactive: one session that runs the tools itself. This is the right "
             "default for asking an agent to do something.\n\n"
             "Coordinator: delegates to spawned child sessions and synthesises their "
             "results. Needed for fan-out work. A coordinator has spawn_* and "
             "wait_for_workstream but NOT the MCP tools, so it cannot touch the "
             "cluster or Odoo itself — its children do that.",
    )
    parent_ws_id = fields.Char(
        readonly=True, index=True,
        help="Set when Turnstone spawned this session as a child of another.",
    )
    activity = fields.Char(
        readonly=True, help="What the agent is doing right now, as Turnstone reports it.",
    )
    activity_state = fields.Char(readonly=True)
    adopted = fields.Boolean(
        readonly=True,
        help="Attached to a workstream that already existed in Turnstone — a "
             "scheduled run, or work started outside Odoo — rather than started here.",
    )
    has_queued_message = fields.Boolean(
        readonly=True,
        help="A message is waiting behind the turn currently in flight.",
    )
    tool_call_count = fields.Integer(readonly=True)
    token_count = fields.Integer(readonly=True)
    context_ratio = fields.Float(
        readonly=True, help="Fraction of the model's context window in use.",
    )
    last_activity = fields.Datetime(default=fields.Datetime.now, readonly=True)

    message_ids = fields.One2many("turnstone.message", "session_id")
    subagent_ids = fields.One2many("turnstone.subagent", "session_id")
    subagent_count = fields.Integer(compute="_compute_subagent_count")
    linked_record_ids = fields.One2many("turnstone.linked.record", "session_id")
    linked_record_count = fields.Integer(compute="_compute_linked_record_count")

    # Raw pending-approval payload from Turnstone, rendered by the chat client.
    pending_approval_json = fields.Text(readonly=True)
    has_pending_approval = fields.Boolean(
        compute="_compute_has_pending_approval", store=True,
    )

    remote_gone = fields.Boolean(
        readonly=True,
        help="Turnstone no longer has this workstream (HTTP 404 on the last sync). "
             "The transcript was cleared then; this flag is why it is empty.",
    )
    sync_note = fields.Text(
        readonly=True,
        help="Why the last sync did not apply a remote response it did not trust — "
             "for example a node that answered before it had loaded the history, or "
             "a bounded message tail. Empty when the mirror is following the server.",
    )

    @api.depends("pending_approval_json")
    def _compute_has_pending_approval(self):
        for rec in self:
            rec.has_pending_approval = bool(rec.pending_approval_json and
                                            rec.pending_approval_json != "[]")

    @api.depends("kind")
    def _compute_supports_attachments(self):
        for rec in self:
            rec.supports_attachments = rec.kind == "coordinator"

    def _compute_subagent_count(self):
        data = self.env["turnstone.subagent"]._read_group(
            [("session_id", "in", self.ids)], ["session_id"], ["__count"]
        )
        counts = {s.id: n for s, n in data}
        for rec in self:
            rec.subagent_count = counts.get(rec.id, 0)

    def _compute_linked_record_count(self):
        data = self.env["turnstone.linked.record"]._read_group(
            [("session_id", "in", self.ids)], ["session_id"], ["__count"]
        )
        counts = {s.id: n for s, n in data}
        for rec in self:
            rec.linked_record_count = counts.get(rec.id, 0)

    # -- access ------------------------------------------------------------

    def _check_owner(self):
        """A session is private to its owner, except for managers.

        Record rules already enforce this for ordinary ORM access; this guards
        the controller entry points, which act through sudo() to reach the
        connection's token and would otherwise bypass the rule.
        """
        if self.env.user.has_group("turnstone_integration.group_turnstone_manager"):
            return True
        for rec in self:
            if rec.user_id != self.env.user:
                raise AccessError(_("This Turnstone session belongs to someone else."))
        return True

    # -- lifecycle ---------------------------------------------------------

    def _ensure_started(self, first_message=None):
        """Create the workstream on Turnstone if this session has none yet.

        A create that comes back without a ``ws_id`` is a FAILURE, not a
        session with an empty id. The console answers an unroutable node or a
        half-initialised workstream with HTTP 200 and either ``{}`` or
        ``{"error": …}``, and treating that as success is the one shape in
        this module that silently destroys something the user typed: the
        session got ``state=thinking``, ``ws_id`` stayed empty, nothing was
        mirrored, ``action_send`` short-circuited on the falsy id, and the
        message the user had just written existed in neither Odoo nor
        Turnstone, with no error anywhere (OTI-5729 shape 1, RC-1).

        So: no id, no write. The session stays ``draft`` — which is what it
        truthfully is — the message is still in the user's composer (the
        client restores it on an error), and the raise names the response so
        the reason is visible.

        A body carrying ``error`` raises even when it also carries a ``ws_id``,
        which is the same reading ``_fetch_detail`` applies to a detail response
        (OTI-5744): the console says an error when it means one, and a
        half-built workstream it flagged is not one to start writing into.
        """
        self.ensure_one()
        if self.ws_id:
            return self.ws_id
        conn = self.connection_id
        conn._check_user_allowed(self.user_id)
        agent = self.agent_id.sudo()
        result = conn._client().create_session(
            name=self.name,
            skill=self.skill or agent.skill or conn.sudo().default_skill or None,
            persona=self.persona or agent.persona or conn.sudo().default_persona or None,
            model=self.model_alias or agent.model_alias or conn.sudo().default_model or None,
            initial_message=first_message,
            kind=self.kind or "interactive",
            judge_model=agent.judge_model or None,
            auto_approve=agent.auto_approve or None,
            auto_approve_tools=agent.auto_approve_tool_list() if agent else None,
        )
        ws_id = result.get("ws_id") if isinstance(result, dict) else None
        if not ws_id or result.get("error"):
            # Nothing is written here, deliberately. The session's own state,
            # ws_id and last_activity stay exactly as they were before the
            # call, so a retry starts from the same clean draft. No audit row
            # either: the raise rolls the whole request transaction back, so an
            # audit entry written on this path would never survive — the log
            # line below and the exception are the honest surface.
            detail = self._describe_create_response(result)
            _logger.warning(
                "Turnstone created no workstream for session %s: %s", self.id, detail,
            )
            raise TurnstoneError(_(
                "Turnstone did not start this session: its create response carried no "
                "usable workstream id (%(detail)s). Nothing was sent, so your message "
                "was not lost — send it again, and if this repeats check the server's "
                "routing and node health.",
            ) % {"detail": detail})
        self.sudo().write({
            "ws_id": ws_id,
            "node_id": result.get("node_id"),
            "state": "thinking" if first_message else "idle",
            "last_activity": fields.Datetime.now(),
        })
        return self.ws_id

    def _describe_create_response(self, result, limit=300):
        """What a workstream-less create response said, for the error and the log.

        Truncated, because it goes in a chat toast. Tokens are not in a create
        response — the console echoes the workstream it made — but the
        truncation is what keeps a proxy's HTML error page from becoming a wall
        of text in the UI.

        An instance method rather than a staticmethod for a reason that is not
        cosmetic: ``_()`` locates the language through ``self.env`` on the
        calling frame, and from a staticmethod there is no ``self``, so every
        call logged "no translation language detected" with a stack trace. The
        translation fell back to the source string either way, so the output was
        never wrong — only the log was.
        """
        if not isinstance(result, dict):
            return _("not an object but a %s") % type(result).__name__
        if result.get("error"):
            return _("Turnstone reported: %s") % str(result["error"])[:limit]
        if not result:
            return _("an empty response")
        try:
            rendered = json.dumps(result, default=str, sort_keys=True)
        except (TypeError, ValueError):
            rendered = repr(result)
        return rendered[:limit] + ("…" if len(rendered) > limit else "")

    def action_send(self, body):
        """Send a user message and mirror it immediately.

        The user's own turn is written locally rather than waited for, so the
        chat echoes instantly; the agent's reply arrives via sync_from_server().
        """
        self.ensure_one()
        self._check_owner()
        body = (body or "").strip()
        if not body:
            raise UserError(_("Nothing to send."))
        if self.state == "closed":
            raise UserError(_("This session is closed. Start a new one."))

        if not self.ws_id:
            self._ensure_started(first_message=body)
        else:
            self.connection_id._client().send(self.ws_id, body, kind=self.kind)

        # Only a session that HAS a workstream can be working. This is a
        # backstop, not the primary check: _ensure_started raises rather than
        # returning without an id, so by here the id exists or we never got
        # here. It stays because the invariant is the whole point of this fix —
        # `thinking` written over a session with nothing behind it is the ghost
        # that used to swallow the first message — and one field read is cheap
        # insurance against a future caller reintroducing it.
        if not self.ws_id:
            raise TurnstoneError(_(
                "This session has no Turnstone workstream, so the message was not "
                "sent. Nothing was recorded as working.",
            ))
        self.sudo().write({"state": "thinking", "last_activity": fields.Datetime.now()})
        self.env["turnstone.audit.log"].log(
            "message_send", session=self, detail=body[:500],
        )
        return self.sync_from_server()

    def action_approve(self, approved, feedback=None):
        """Resolve a pending tool approval."""
        self.ensure_one()
        self._check_owner()
        if not self.ws_id:
            raise UserError(_("This session has not started."))
        cycle_id, tool_name = None, None
        try:
            pending = json.loads(self.pending_approval_json or "[]")
            if pending:
                cycle_id = pending[0].get("cycle_id")
                items = pending[0].get("items") or []
                if items:
                    tool_name = items[0].get("func_name") or items[0].get("approval_label")
        except (ValueError, AttributeError, IndexError):
            cycle_id, tool_name = None, None
        result = self.connection_id._client().approve(
            self.ws_id, approved, feedback=feedback, cycle_id=cycle_id, kind=self.kind
        ) or {}
        if result.get("already_resolved"):
            # Someone (probably this user, twice) resolved it already. Do not
            # log a second decision for one approval, and do not claim the
            # agent is working — just show whatever is actually true now.
            return self.sync_from_server()
        self.sudo().write({"state": "thinking", "pending_approval_json": "[]"})
        self.env["turnstone.audit.log"].log(
            "tool_approve" if approved else "tool_deny",
            session=self, tool=tool_name, detail=feedback or None,
        )
        return self.sync_from_server()

    def action_retry(self):
        """Re-run the last user turn. Useful when an agent went down a wrong path."""
        self.ensure_one()
        self._check_owner()
        if not self.ws_id:
            raise UserError(_("This session has not started."))
        self.connection_id._client().retry(self.ws_id, kind=self.kind)
        self.env["turnstone.audit.log"].log("session_retry", session=self)
        self.sudo().write({"state": "thinking"})
        return self.sync_from_server()

    def action_rewind(self, turns=1):
        """Drop the last N turns, so the conversation can take another route."""
        self.ensure_one()
        self._check_owner()
        if not self.ws_id:
            raise UserError(_("This session has not started."))
        self.connection_id._client().rewind(self.ws_id, turns=turns, kind=self.kind)
        self.env["turnstone.audit.log"].log(
            "session_rewind", session=self, detail=_("%s turn(s)") % turns,
        )
        # Authoritative: dropping turns is this call's whole purpose, so a
        # shorter transcript here is the truth rather than a partial response.
        # Without it a rewind back to nothing would be refused as "no history".
        return self.sync_from_server(authoritative=True)

    def action_cancel_queued(self):
        """Drop a message queued behind the in-flight turn, leaving the turn alone."""
        self.ensure_one()
        self._check_owner()
        if self.ws_id:
            self.connection_id._client().cancel_queued(self.ws_id, kind=self.kind)
        return self.sync_from_server()

    def action_cancel_generation(self):
        self.ensure_one()
        self._check_owner()
        if self.ws_id:
            self.connection_id._client().cancel(self.ws_id, kind=self.kind)
        self.env["turnstone.audit.log"].log("generation_cancel", session=self)
        return self.sync_from_server()

    def action_close(self):
        self.ensure_one()
        self._check_owner()
        if self.ws_id:
            try:
                self.connection_id._client().close(self.ws_id, kind=self.kind)
            except UserError:
                # Already gone server-side; closing locally is still correct.
                _logger.info("Turnstone close failed for %s; closing locally", self.ws_id)
        self.sudo().write({"state": "closed"})
        self.env["turnstone.audit.log"].log("session_close", session=self)
        return True

    # -- sync --------------------------------------------------------------

    def sync_from_server(self, authoritative=False):
        """Pull the authoritative state and transcript from Turnstone.

        Three outcomes, and the difference between them is the whole point of
        this method:

        1. **A usable response** — state, counters and transcript are applied
           together, inside one savepoint, so a failure part-way leaves the
           previous transcript (and the header that goes with it) intact
           rather than half replaced.
        2. **A transport failure** — an exception is raised **before anything
           is written**, so the caller gets an error to show and the mirror is
           exactly as it was. This covers HTTP 401/403/5xx and a non-JSON
           body, and — the case that used to be silent — HTTP 200 carrying
           ``{"error": …}``, which is what a console answers for a session it
           cannot serve. None of those is evidence about the conversation.
        3. **A 404** — the workstream is gone from Turnstone. Only here may the
           transcript be cleared, and the clearing is recorded (``remote_gone``
           plus an audit entry) so an empty transcript has a reason attached
           instead of looking like data loss. ``authoritative=True`` clears it
           too, for a caller that knows the remote is gone.

        ``authoritative`` is for a caller that just changed the remote
        transcript itself (a rewind), so a short or empty list is the truth
        rather than a partial view. See
        ``turnstone.message._replace_transcript``.
        """
        self.ensure_one()
        self._check_owner()
        if not self.ws_id:
            return self._as_dict()

        detail = self._fetch_detail()
        if detail is None:
            # 404: recorded by _fetch_detail as the clear. Nothing else to apply.
            return self._as_dict()

        vals = self._detail_vals(detail)
        messages = detail.get("messages")
        if not isinstance(messages, list):
            # The key is absent or not a list. That is no evidence about the
            # transcript, so the mirror is left alone rather than emptied.
            vals["sync_note"] = _(
                "Turnstone's response carried no message list, so the stored "
                "transcript was left as it was.",
            )
            messages = None

        # The transcript and the header it describes go in as ONE unit: a
        # response that is refused must not have already rewritten the state,
        # counters and last-activity of a session it did not describe. The
        # savepoint makes that true even if the write itself fails part-way.
        with self.env.cr.savepoint():
            if messages is not None:
                vals["sync_note"] = self.env["turnstone.message"]._replace_transcript(
                    self, messages, authoritative=authoritative,
                ) or False
            self.sudo().write(vals)

        self._sync_linked_records(messages or [])
        self._sync_subagents()
        return self._as_dict()

    # -- sync internals ----------------------------------------------------

    def _fetch_detail(self):
        """The session detail, or None when Turnstone says it is gone.

        A 404 is the one remote answer that may destroy local state, because it
        is the one that is a statement about this session: it does not exist.
        Everything else either raises (and nothing is written) or is applied.

        A body that CARRIES an error is an error even at HTTP 200: the console
        answers a workstream it cannot serve with ``{"error": …}`` and a 200
        status, and that body has no ``live``/``persisted``/``messages`` — read
        as data it looks exactly like an empty conversation, which is how a
        working session's transcript was wiped (OTI-5729 shape 3).
        ``approve()``'s deliberate ``{"already_resolved": True}`` has no
        ``error`` key, so it is not affected.

        A session this side already considers closed is treated differently on
        404: its remote workstream being gone is expected rather than news, and
        the local transcript is the only copy left. That is the case where
        deleting rows would throw away the thing the user came to read.
        """
        self.ensure_one()
        try:
            detail = self.connection_id._client().session_detail(self.ws_id)
        except TurnstoneNotFound:
            # Record it once. A session that is gone keeps being polled (the
            # chat refreshes on a timer), and one audit row per poll would bury
            # the log that exists to say this happened.
            if not self.remote_gone:
                self._record_remote_gone(keep_transcript=self.state == "closed")
            return None
        if isinstance(detail, dict) and detail.get("error"):
            raise TurnstoneError(
                _("Turnstone refused the session's detail: %s") % detail["error"]
            )
        if not isinstance(detail, dict):
            raise TurnstoneError(
                _("Turnstone returned an unexpected session detail (a %s, not an object).")
                % type(detail).__name__
            )
        if not ({"live", "persisted", "messages"} & set(detail)):
            # Parsed, 200, and says nothing about the session. Applying it would
            # be inventing a state for a session this response never described.
            raise TurnstoneError(
                _("Turnstone's session detail carried none of the expected blocks "
                  "(live, persisted, messages), so nothing was applied.")
            )
        return detail

    def _record_remote_gone(self, keep_transcript=False):
        """Record that Turnstone no longer has the workstream, and usually clear.

        What is cleared is the VOLATILE half — transcript, live state, counters,
        pending approval — because none of it can be refreshed any more and a
        half-kept header would be a claim about a session that no longer exists.
        Subagent rows and linked records stay: they are history (what ran, what
        it touched), not a claim that anything is still live. The Odoo row
        itself stays too, with the reason attached.

        ``keep_transcript`` is for a session already closed this side: its
        remote workstream being gone is expected, not news, and the local
        transcript is then the only copy there is. The outcome is still
        recorded, so "why is this greyed out and unsyncable" has an answer.
        """
        self.ensure_one()
        note = _(
            "Turnstone no longer has this session's workstream (%s): it was "
            "deleted or expired on the server, so the transcript cannot be "
            "read any more.",
        ) % (self.ws_id or "")
        vals = {
            "remote_gone": True,
            "sync_note": note,
            "activity": False,
            "activity_state": False,
            "pending_approval_json": "[]",
            "tool_call_count": 0,
            "token_count": 0,
            "context_ratio": 0.0,
            "last_activity": fields.Datetime.now(),
        }
        if not keep_transcript:
            vals["state"] = "error"
        with self.env.cr.savepoint():
            if not keep_transcript:
                self.sudo().message_ids.unlink()
            self.sudo().write(vals)
        self.env["turnstone.audit.log"].log(
            "sync_remote_gone", session=self,
            detail=_("Turnstone answered 404 for workstream %(ws)s; %(what)s")
                   % {"ws": self.ws_id or "",
                      "what": ("closed session's transcript kept" if keep_transcript
                               else "local transcript cleared")},
        )
        _logger.info(
            "Turnstone no longer has workstream %s (session %s); kept transcript: %s",
            self.ws_id, self.id, keep_transcript,
        )

    def _detail_vals(self, detail):
        """The header fields one detail response actually carries.

        Only keys the response contains are written. A payload that omits
        ``live`` is not a claim that the agent has no pending approvals and is
        using no tokens — it says nothing at all — and treating absence as zero
        rewrites the header of a session that is working. The one exception is
        ``state``: it falls back to ``idle`` because a session with a
        workstream and no reported state is, as far as anything can tell, not
        working.
        """
        self.ensure_one()
        live = detail.get("live")
        live = live if isinstance(live, dict) else {}
        persisted = detail.get("persisted")
        persisted = persisted if isinstance(persisted, dict) else {}

        vals = {}
        if live or persisted.get("state"):
            raw_state = live.get("state") or persisted.get("state") or "idle"
            # Approvals are only cleared or replaced by a live block that
            # actually carries the key. A response with no live block says
            # nothing about them, and blanking them would hide a tool call that
            # is still waiting on a human.
            if "pending_approval_details" in live:
                pending = live["pending_approval_details"] or []
                vals["pending_approval_json"] = json.dumps(pending)
            else:
                try:
                    pending = json.loads(self.pending_approval_json or "[]")
                except ValueError:
                    pending = []
            vals["state"] = "attention" if pending else self._map_state(raw_state)
        for key, field in (
            ("tool_calls", "tool_call_count"),
            ("tokens", "token_count"),
            ("context_ratio", "context_ratio"),
        ):
            if key in live and live[key] is not None:
                vals[field] = live[key]
        if "activity" in live:
            vals["activity"] = live["activity"] or False
        if "activity_state" in live:
            vals["activity_state"] = live["activity_state"] or False
        if live or persisted:
            vals["last_activity"] = fields.Datetime.now()

        if persisted.get("kind"):
            vals["kind"] = persisted["kind"]
        if persisted.get("parent_ws_id"):
            vals["parent_ws_id"] = persisted["parent_ws_id"]
        if persisted.get("persona") and not self.persona:
            vals["persona"] = persisted["persona"]
        # Turnstone titles a session from its first turn; adopt that once, so
        # the sidebar shows something better than "New session".
        title = persisted.get("title") or live.get("title")
        if title and self.name in (_("New session"), "New session", False):
            vals["name"] = title
        if persisted.get("node_id"):
            vals["node_id"] = persisted["node_id"]
        # A response that parsed is evidence the session is back, whatever
        # made it look gone before.
        vals["remote_gone"] = False
        return vals

    def _sync_subagents(self):
        """Upsert the children currently visible, and mark the rest finished."""
        self.ensure_one()
        live = self._children()
        Sub = self.env["turnstone.subagent"].sudo()
        existing = {s.ws_id: s for s in self.sudo().subagent_ids}
        seen = set()
        for child in live:
            seen.add(child["ws_id"])
            vals = {
                "name": child["name"],
                "state": child["state"],
                "kind": child["kind"],
                "persona": child.get("persona"),
                "model_alias": child.get("model"),
                "activity": child.get("activity"),
                "tool_call_count": child.get("tool_calls") or 0,
                "token_count": child.get("tokens") or 0,
                "depth": child.get("depth") or 1,
                "parent_ws_id": child.get("parent_ws_id"),
                "gone": False,
            }
            if child["ws_id"] in existing:
                existing[child["ws_id"]].write(vals)
            else:
                Sub.create(dict(vals, session_id=self.id, ws_id=child["ws_id"]))
        # Anything we knew about that is no longer listed has finished. Keep the
        # row and its last known numbers; only the liveness flag changes.
        for ws_id, rec in existing.items():
            if ws_id not in seen and not rec.gone:
                rec.write({"gone": True, "activity": False})

    @staticmethod
    def _map_state(raw):
        if raw in ("idle", "complete", "completed"):
            return "idle"
        if raw in ("error",):
            return "error"
        if raw in ("closed",):
            return "closed"
        return "thinking"

    def _sync_messages(self, messages, authoritative=False):
        """Replace the mirrored transcript. Thin alias for the message model.

        Kept because it is what the rest of this module (and the README) calls
        it; the comparison and the guards live in
        ``turnstone.message._replace_transcript``, next to the rows they
        touch.

        Rewritten wholesale rather than appended because Turnstone can rewind
        or retry a turn, which removes messages. Diffing an append-only log
        against a mutable one is how you end up showing a reply that was taken
        back. What that rule must NOT mean is "replace with any response that
        arrives" — see the guards in ``_replace_transcript``.
        """
        self.ensure_one()
        return self.env["turnstone.message"]._replace_transcript(
            self, messages, authoritative=authoritative,
        )

    def _sync_linked_records(self, messages):
        """Build links from the agent's Odoo MCP writes.

        Reads the tool call for the model name and the matching tool RESULT for
        the id Odoo returned. Both halves are needed: the call says what model,
        only the result says which record.
        """
        self.ensure_one()
        # call_id -> (operation, model)
        intents = {}
        for msg in messages:
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                name = fn.get("name") or call.get("name") or ""
                op = ODOO_WRITE_TOOLS.get(name)
                if not op:
                    continue
                args = fn.get("arguments") or call.get("arguments") or "{}"
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                res_model = args.get("model") or args.get("model_name")
                if not res_model:
                    continue
                call_id = call.get("id") or fn.get("id")
                ids = args.get("ids") or args.get("record_ids") or args.get("res_id")
                intents[call_id] = (op, res_model, ids)

        found = {}
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            call_id = msg.get("tool_call_id") or msg.get("call_id")
            if call_id not in intents:
                continue
            op, res_model, known_ids = intents[call_id]
            # Where the id lives depends on the operation, and getting this
            # wrong produces a link that looks right and points at the wrong
            # record:
            #   create  -> the RESULT is the new record's id; the call had none
            #   write   -> the call carries the ids; the result is just `true`
            #   message -> the call carries res_id; the RESULT is the new
            #              mail.message id, which belongs to a different model
            if op == "create":
                ids = self._extract_ids(msg.get("content"), None)
            else:
                ids = self._extract_ids(None, known_ids)
            for rid in ids:
                found[(res_model, rid, op)] = True

        if not found:
            return
        Link = self.env["turnstone.linked.record"].sudo()
        existing = {
            (l.res_model, l.res_id, l.operation) for l in self.sudo().linked_record_ids
        }
        to_create = []
        for (res_model, res_id, op) in found:
            if (res_model, res_id, op) in existing:
                continue
            label = False
            model = self.env.get(res_model)
            if model is not None:
                rec = model.sudo().browse(res_id).exists()
                label = rec.display_name if rec else False
            to_create.append({
                "session_id": self.id,
                "res_model": res_model,
                "res_id": res_id,
                "operation": op,
                "display_name_cache": label,
            })
        if to_create:
            Link.create(to_create)

    @staticmethod
    def _is_id(value):
        """A usable record id.

        `bool` subclasses `int` in Python, so a plain isinstance(x, int) accepts
        True — and odoo_write returns exactly `true` on success. Without this
        guard every successful write linked to record id 1, which is a real
        record in most models and therefore a wrong link rather than an obvious
        bug.
        """
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _extract_ids(content, known_ids=None):
        """Pull record ids out of an Odoo MCP tool result.

        odoo_create returns the new id; odoo_write and message_post return a
        bare success, so the ids come from the call arguments instead.
        """
        is_id = TurnstoneSession._is_id
        ids = set()
        if is_id(known_ids):
            ids.add(known_ids)
        elif isinstance(known_ids, (list, tuple)):
            ids.update(i for i in known_ids if is_id(i))

        if isinstance(content, list):
            content = " ".join(str(p.get("text") or "") for p in content)
        if not isinstance(content, str):
            return sorted(ids)
        text = content.strip()
        try:
            parsed = json.loads(text)
            if is_id(parsed):
                ids.add(parsed)
            elif isinstance(parsed, dict):
                for key in ("id", "res_id", "record_id"):
                    if is_id(parsed.get(key)):
                        ids.add(parsed[key])
            elif isinstance(parsed, list):
                ids.update(i for i in parsed if is_id(i))
        except ValueError:
            # Plain-text result, e.g. "Created record with ID 42".
            match = re.search(r"\b(?:id|ID)[\s:=]+(\d+)", text)
            if match:
                ids.add(int(match.group(1)))
            elif text.isdigit():
                ids.add(int(text))
        return sorted(ids)

    # -- attachments -------------------------------------------------------

    def action_add_attachment(self, filename, content_b64, mime_type=None):
        """Attach a file to this session's next turn.

        Turnstone accepts images, PDFs and text; it rejects xlsx and docx
        outright. Rather than let the user discover that from a raw API error,
        the refusal is caught and explained with what to do instead.
        """
        self.ensure_one()
        self._check_owner()
        if not self.supports_attachments:
            raise UserError(_(
                "Turnstone accepts attachments on coordinator sessions only. "
                "Start this work with a coordinator agent if it needs files."
            ))
        self._ensure_started()
        raw = base64.b64decode(content_b64)
        guessed = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        result = self.connection_id._client().upload_attachment(
            self.ws_id, filename, raw, guessed
        )
        self.env["turnstone.audit.log"].log(
            "attachment_add", session=self,
            detail=_("%(name)s (%(mime)s, %(size)s bytes)") % {
                "name": filename, "mime": guessed, "size": len(raw),
            },
        )
        return result

    def attachments(self):
        self.ensure_one()
        self._check_owner()
        if not self.ws_id or not self.supports_attachments:
            return []
        return self.connection_id._client().list_attachments(self.ws_id)

    def action_remove_attachment(self, attachment_id):
        self.ensure_one()
        self._check_owner()
        if not self.ws_id:
            return True
        self.connection_id._client().delete_attachment(self.ws_id, attachment_id)
        self.env["turnstone.audit.log"].log(
            "attachment_remove", session=self, detail=attachment_id,
        )
        return True

    # -- subagents ---------------------------------------------------------

    def _children(self):
        """Child workstreams Turnstone spawned from this session.

        Read off the cluster feed rather than /workstreams/{id}/children: that
        endpoint is coordinator-only and answers "coordinator not found" for an
        interactive session, whereas every row of the cluster feed carries
        parent_ws_id.
        """
        self.ensure_one()
        if not self.ws_id:
            return []
        try:
            feed = self.connection_id._client().cluster_workstreams(per_page=200)
        except UserError:
            # Subagents are a nicety; a chat that still works beats an error
            # banner because one extra call failed.
            _logger.info("Could not read cluster feed for children of %s", self.ws_id)
            return []
        rows = feed.get("workstreams") or []
        by_parent = {}
        for row in rows:
            by_parent.setdefault(row.get("parent_ws_id"), []).append(row)

        # Walk the tree so a child that spawned its own children is reachable.
        out, queue, seen = [], list(by_parent.get(self.ws_id) or []), set()
        depth = {c.get("id"): 1 for c in queue}
        while queue:
            row = queue.pop(0)
            rid = row.get("id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            out.append({
                "ws_id": rid,
                "name": row.get("title") or row.get("name") or rid[:12],
                "state": row.get("state"),
                "kind": row.get("kind"),
                "activity": row.get("activity") or "",
                "model": row.get("model_alias") or row.get("model"),
                "tool_calls": row.get("tool_calls") or 0,
                "tokens": row.get("tokens") or 0,
                "persona": row.get("persona"),
                "depth": depth.get(rid, 1),
                "parent_ws_id": row.get("parent_ws_id"),
            })
            for grandchild in by_parent.get(rid) or []:
                depth[grandchild.get("id")] = depth.get(rid, 1) + 1
                queue.append(grandchild)
        return out

    # -- adoption ----------------------------------------------------------

    @api.model
    def adopt(self, connection_id, ws_id, name=None):
        """Attach to a workstream that already exists in Turnstone.

        This is how a user joins work they did not start from Odoo — a
        scheduled run, a job kicked off by another system, or a child spawned by
        a coordinator. The conversation is already there; this just gives it an
        Odoo record so it can be opened, steered and access-controlled.
        """
        conn = self.env["turnstone.connection"].browse(int(connection_id)).exists()
        if not conn:
            raise UserError(_("No such Turnstone server."))
        conn._check_user_allowed()
        self._check_may_adopt(conn, ws_id)

        existing = self.search([("ws_id", "=", ws_id), ("connection_id", "=", conn.id)], limit=1)
        if existing:
            existing._check_owner()
            existing.sync_from_server()
            return existing

        detail = conn._client().session_detail(ws_id)
        persisted = detail.get("persisted") or {}
        if not persisted.get("ws_id"):
            raise UserError(_("Turnstone has no workstream %s.") % ws_id)

        session = self.create({
            "name": name or persisted.get("title") or persisted.get("name") or _("Adopted session"),
            "connection_id": conn.id,
            "ws_id": ws_id,
            "adopted": True,
            "kind": persisted.get("kind") or "interactive",
            "parent_ws_id": persisted.get("parent_ws_id") or False,
            "persona": persisted.get("persona") or False,
            "node_id": persisted.get("node_id") or False,
        })
        session.sync_from_server()
        self.env["turnstone.audit.log"].log(
            "session_adopt", session=session,
            detail=_("Attached to workstream %s started outside Odoo.") % ws_id,
        )
        return session

    @api.model
    def _check_may_adopt(self, connection, ws_id):
        """Guard adoption. Knowing a workstream id must not be enough.

        Adopting hands over the whole transcript, and a transcript contains
        whatever the agent read — with the SERVER token's authority, not the
        caller's. Without this check any Turnstone User could attach to another
        person's chat, or to an unattended scheduled job, just by passing its id.

        Managers may adopt anything; that is what the Live Activity view is for.
        Everyone else may adopt only a workstream Turnstone already told us is a
        descendant of a session they own — the "open my subagent" case. Those
        rows exist only because the user's own session synced them.
        """
        if self.env.user.has_group("turnstone_integration.group_turnstone_manager"):
            return True
        own_subagent = self.env["turnstone.subagent"].sudo().search([
            ("ws_id", "=", ws_id),
            ("session_id.connection_id", "=", connection.id),
            ("session_id.user_id", "=", self.env.user.id),
        ], limit=1)
        if own_subagent:
            return True
        raise AccessError(
            _("You can only attach to a subagent of one of your own sessions. "
              "Ask a Turnstone Administrator to attach this workstream for you.")
        )

    # -- serialisation for the chat client ---------------------------------

    def _parent_session(self):
        """The Odoo session mirroring this one's parent workstream, if we have it."""
        self.ensure_one()
        if not self.parent_ws_id:
            return self.browse()
        return self.sudo().search([
            ("ws_id", "=", self.parent_ws_id),
            ("connection_id", "=", self.connection_id.id),
        ], limit=1)

    def _as_dict(self):
        self.ensure_one()
        rec = self.sudo()
        parent = rec._parent_session()
        # Which subagents Odoo already mirrors, so the panel can offer "open"
        # rather than "attach" and avoid a needless round trip.
        subagent_sessions = {}
        sub_ws = [s.ws_id for s in rec.subagent_ids if s.ws_id]
        if sub_ws:
            subagent_sessions = {
                s.ws_id: s.id
                for s in rec.search([
                    ("ws_id", "in", sub_ws),
                    ("connection_id", "=", rec.connection_id.id),
                ])
            }
        try:
            pending = json.loads(rec.pending_approval_json or "[]")
        except ValueError:
            pending = []
        return {
            "id": rec.id,
            "ws_id": rec.ws_id or None,
            "name": rec.name,
            "state": rec.state,
            "skill": rec.skill or None,
            "persona": rec.persona or None,
            "model": rec.model_alias or None,
            "connection_id": rec.connection_id.id,
            "connection_name": rec.connection_id.name,
            "node_id": rec.node_id or None,
            "kind": rec.kind,
            "parent_ws_id": rec.parent_ws_id or None,
            # So the chat can offer a way back rather than stranding the user
            # on a subagent that is not in the sidebar.
            "parent_session_id": parent.id if parent else None,
            "parent_name": parent.name if parent else None,
            "adopted": rec.adopted,
            "agent_id": rec.agent_id.id or None,
            "agent_name": rec.agent_id.name or None,
            # Why the transcript is empty, when it is empty for a reason the
            # user can act on: the workstream was deleted on the server, or a
            # sync was refused as partial. Without these an empty transcript
            # reads as data loss.
            "remote_gone": rec.remote_gone,
            "sync_note": rec.sync_note or "",
            "supports_attachments": rec.supports_attachments,
            "activity": rec.activity or "",
            "activity_state": rec.activity_state or "",
            "tool_calls": rec.tool_call_count,
            "tokens": rec.token_count,
            "context_ratio": rec.context_ratio,
            "last_activity": rec.last_activity and rec.last_activity.isoformat(),
            "pending_approvals": self._flatten_approvals(pending),
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "body": m.body or "",
                    "tool_names": m.tool_names or "",
                    "is_guard_notice": m.is_guard_notice,
                }
                for m in rec.message_ids
            ],
            "subagents": [
                {
                    "ws_id": sub.ws_id,
                    "odoo_session_id": subagent_sessions.get(sub.ws_id),
                    "name": sub.name or sub.ws_id[:12],
                    "state": "done" if sub.gone else (sub.state or "unknown"),
                    "kind": sub.kind,
                    "activity": sub.activity or "",
                    "tool_calls": sub.tool_call_count,
                    "depth": sub.depth,
                    "gone": sub.gone,
                }
                for sub in rec.subagent_ids
            ],
            "linked_records": [
                {
                    "id": l.id,
                    "res_model": l.res_model,
                    "res_id": l.res_id,
                    "operation": l.operation,
                    "name": l.display_name_cache or "%s/%s" % (l.res_model, l.res_id),
                }
                for l in rec.linked_record_ids
            ],
        }

    @staticmethod
    def _flatten_approvals(cycles):
        """Turnstone nests the useful fields one level down, under items[]."""
        out = []
        for cycle in cycles or []:
            for item in cycle.get("items") or []:
                out.append({
                    "cycle_id": cycle.get("cycle_id"),
                    "call_id": item.get("call_id"),
                    "tool": item.get("func_name") or item.get("approval_label") or "?",
                    "preview": (item.get("preview") or "").strip(),
                    "judge": item.get("judge_verdict") or item.get("heuristic_verdict") or None,
                })
        return out

    def action_open_chat(self):
        """Open this session in the chat client."""
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "turnstone_integration.chat",
            "name": self.name,
            "params": {"session_id": self.id},
        }
