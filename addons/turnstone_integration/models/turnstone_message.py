"""One turn of a Turnstone conversation, mirrored into Odoo."""

from odoo import _, api, fields, models


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

    # -- transcript replacement -------------------------------------------
    #
    # The mirror is REPLACED, not appended: Turnstone can rewind or retry a
    # turn, which removes messages, and diffing an append-only log against a
    # mutable one is how you end up showing a reply that was taken back.
    #
    # But "replace" is only right when the response IS the transcript. A
    # response that carries no messages, or fewer than we already have starting
    # somewhere other than the beginning, is a partial view — a node that has
    # not loaded history yet, or a bounded tail — and replacing with it deletes
    # turns that still exist on the server. So the replacement is guarded, and
    # the guard lives here rather than in the session so that every caller gets
    # it. See discoveries/session-persistence-investigation.md (RC-2/RC-4).

    @api.model
    def _render(self, session, messages):
        """The mirror rows for one Turnstone message list.

        Everything Turnstone sends is not conversation: tool results are
        enormous, memory pointers and other plumbing are noise, and an output
        guard notice is a warning rather than a turn. All three are handled
        here, in one place, so the stored transcript is what the chat shows.
        """
        rows = []
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role not in ("user", "assistant", "tool", "system"):
                continue
            body = msg.get("content")
            if isinstance(body, list):
                body = " ".join(str(p.get("text") or "") for p in body)
            body = (body or "").strip()
            tool_names = ", ".join(
                t.get("function", {}).get("name") or t.get("name") or ""
                for t in (msg.get("tool_calls") or [])
            ).strip(", ")

            is_guard = role == "system" and body.startswith("Output guard:")
            # Tool results are enormous and already summarised by the agent's
            # next turn. Keep a stub so the transcript shows work happened.
            if role == "tool":
                body = (body[:280] + "…") if len(body) > 280 else body
            # A system turn that is neither a guard notice nor useful prose is
            # Turnstone plumbing (memory pointers and the like).
            if role == "system" and not is_guard:
                continue
            if not body and not tool_names:
                continue

            rows.append({
                "session_id": session.id,
                "sequence": idx,
                "role": role,
                "body": body,
                "tool_names": tool_names or False,
                "is_guard_notice": is_guard,
            })
        return rows

    @staticmethod
    def _comparable(rows):
        """What two transcripts must agree on to count as the same turns.

        Sequence is left out: it is re-derived from the payload each time, and
        it shifts when Turnstone inserts plumbing we filter out. Role, body and
        tools are what the user reads. ``body`` is normalised the same way on
        both sides (``or ""``) because the stored column is nullable while a
        rendered row is always a string — mixing them makes every poll look
        like a change and rewrites the whole transcript each time.
        """
        return [(r["role"], r["body"] or "", r["tool_names"] or False,
                 r["is_guard_notice"]) for r in rows]

    @api.model
    def _replace_transcript(self, session, messages, authoritative=False):
        """Make the mirror match ``messages``. Returns a note if it refused.

        ``authoritative`` means the caller just changed the remote transcript
        itself (a rewind, or an explicit clear), so a shorter list is the whole
        truth and is applied as given. A background poll cannot know that, so
        for it the two ambiguous shapes are refused instead of applied:

        * **no messages where we have some** — what a node that has not loaded
          history answers, and indistinguishable from "the conversation is
          empty" on this response alone;
        * **fewer messages that are not a prefix of ours** — a bounded/tail-only
          response drops the *head*; a rewind drops the *tail*. If the incoming
          list starts somewhere other than our first turn, it is a partial
          view, not a rewind.

        A refusal changes nothing and returns the reason, which the session
        stores so the user (and the next investigator) can see why the mirror
        stopped following the server. It is not an exception: a slow-loading
        node is a transient condition, and raising would put an error banner on
        a conversation that is intact.

        Atomicity: unlink-then-create is two statements, so a failure between
        them would leave the session with no transcript at all. The caller runs
        this inside a savepoint (``session.sync_from_server``), which rolls the
        unlink back with the failed create.
        """
        session.ensure_one()
        rows = self._render(session, messages)
        existing = self.sudo().search(
            [("session_id", "=", session.id)], order="sequence, id"
        )
        current = [{
            "role": rec.role,
            "body": rec.body or False,
            "tool_names": rec.tool_names or False,
            "is_guard_notice": rec.is_guard_notice,
        } for rec in existing]

        stored = self._comparable(current)
        incoming = self._comparable(rows)
        stored_seq = [rec.sequence for rec in existing]
        incoming_seq = [row["sequence"] for row in rows]

        if stored == incoming and stored_seq == incoming_seq:
            return False  # unchanged; avoid the churn

        if not authoritative and stored:
            if not incoming:
                return _(
                    "Turnstone returned no message history, so the %(kept)s stored "
                    "turn(s) were kept rather than cleared. A node that has not "
                    "loaded a session's history yet answers this way.",
                ) % {"kept": len(stored)}
            if len(incoming) < len(stored) and incoming != stored[:len(incoming)]:
                return _(
                    "Turnstone returned %(got)s of %(kept)s stored turns, and not as "
                    "the beginning of this session's transcript — a partial or "
                    "bounded response. The stored transcript was kept.",
                ) % {"got": len(incoming), "kept": len(stored)}

        existing.unlink()
        if rows:
            self.sudo().create(rows)
        return False
