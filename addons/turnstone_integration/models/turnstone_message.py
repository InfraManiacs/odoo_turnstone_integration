"""One turn of a Turnstone conversation, mirrored into Odoo."""

from odoo import _, api, fields, models

# How many consecutive stored turns a page must repeat, at minimum, before the
# mirror accepts that it is the same conversation with the window slid forward
# (see ``_window_shift``). Twenty rows of user text is a fingerprint; one is a
# coincidence.
WINDOW_OVERLAP_ROWS = 20


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
    #
    # There is a THIRD shape the shortening rule structurally cannot see
    # (OTI-5772): a response whose length fills the fetch limit. `detail`
    # answers with a tail (`message_limit`, default 20, clamped 200) and carries
    # no total, no `has_more` and no offset, so a response of exactly
    # `fetch_limit` rows is possibly — not certainly — the whole conversation.
    # A rolling window is the case a length comparison can never catch: each new
    # turn pushes the oldest out and the list stays the same length, so a mirror
    # at the limit silently loses its head on every poll. OTI-5763 measured
    # exactly that on a real server: 20 rows, the user's first message gone,
    # `sync_note` empty. So at the limit only a response that starts with
    # everything already stored is applied, and every at-the-limit response is
    # disclosed in `sync_note` rather than presented as complete.

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

    # -- the third shape: a response that fills the fetch limit -------------
    #
    # The two rules above catch a response that is SHORTER in a way a rewind
    # does not explain. They cannot catch a response whose length is unchanged,
    # and that is not a corner case: `detail` answers with a TAIL and says
    # nothing about having truncated, so its only signal is the length. Once a
    # conversation fills the page (`message_limit`, the server's 200 clamp), each
    # new turn pushes the oldest one out and the list stays exactly the same
    # length — a rolling window — so a mirror at the limit loses its head on
    # every poll with nothing anywhere recording it. OTI-5763 measured this on a
    # real server (20 rows mirrored while the server held 26, the user's first
    # message gone, `sync_note` empty). OTI-5772 is the fix:
    #
    #   * a page that fills the limit is treated as POSSIBLY partial, and the
    #     fact is disclosed in `sync_note` rather than presented as complete;
    #   * nothing already mirrored is ever dropped because of it — the page is
    #     stitched onto the stored turns it continues, and a page that does not
    #     provably continue them is refused outright.

    @api.model
    def _partial_response_note(self, session, limit, got, kept=None,
                               stitched=False):
        """The disclosure for a response that filled the fetch limit.

        Three statements, because the outcomes differ and a user acts on them
        differently:

        * ``kept is None`` — the page was applied and is possibly a tail, so
          the transcript is presented as possibly missing turns older than the
          first one mirrored, rather than as complete;
        * ``stitched`` — the page was applied *and* the stored turns it
          continues were kept in front of it, so nothing already mirrored was
          dropped, though the conversation may still start after its first turn;
        * otherwise ``kept`` — the page was refused, because applying it would
          have replaced the stored transcript with one whose beginning does not
          continue it. The stored ``kept`` turns were kept.

        The deeper door is named per kind because it exists for only one: a
        coordinator's transcript can be read further back from the console's
        ``/workstreams/{id}/history?limit=500``, while an interactive session has
        no equivalent endpoint at all (OTI-5763 verified both — the
        ``/route/.../history`` twin 404s in 1.8.3). Saying "there is more
        somewhere" without saying where, or presenting the rows at the limit as
        the whole conversation, would be the dishonest version of this note.
        """
        if session.kind == "coordinator":
            deeper = _(
                "Turnstone's console endpoint "
                "`/workstreams/{id}/history?limit=500` reaches further back for "
                "a coordinator.",
            )
        else:
            deeper = _(
                "An interactive session has no deeper endpoint — Turnstone "
                "serves it only through this cluster detail call, so turns it "
                "did not serve cannot be read back into Odoo.",
            )
        if kept is None:
            return _(
                "Turnstone returned %(got)s conversation rows — as many as its "
                "detail endpoint will serve (message_limit=%(limit)s) — so this "
                "conversation may be longer, and its earliest turns may not be "
                "mirrored here. %(deeper)s",
            ) % {"got": got, "limit": limit, "deeper": deeper}
        if stitched:
            return _(
                "Turnstone returned its most recent %(got)s conversation rows — "
                "as many as its detail endpoint will serve "
                "(message_limit=%(limit)s) — so this conversation may be "
                "longer. The %(kept)s earlier turn(s) already stored were kept "
                "in front of them, so nothing mirrored was dropped; turns older "
                "than the first page that ever reached Odoo may still be "
                "missing. %(deeper)s",
            ) % {"got": got, "limit": limit, "kept": kept, "deeper": deeper}
        return _(
            "Turnstone returned its most recent %(got)s conversation rows — as "
            "many as its detail endpoint will serve "
            "(message_limit=%(limit)s) — so this conversation may be longer. "
            "That page did not continue the %(kept)s turn(s) already stored, so "
            "the stored transcript was kept rather than replaced by a page whose "
            "beginning could not be matched to it. %(deeper)s",
        ) % {"got": got, "limit": limit, "kept": kept, "deeper": deeper}

    @staticmethod
    def _overlap(stored, incoming):
        """How much of the stored transcript a page at the limit repeats.

        The question such a page has to answer is: does it continue the
        transcript already mirrored? A page that fills the limit is a *window*
        over the end of the conversation, so as new turns arrive the window
        slides — the page no longer starts where the mirror does, but its
        beginning *is* the mirror's end. Returns how many leading page rows that
        end accounts for, or 0 when the two do not line up (a rewind on the
        server, a page from another conversation, or a coincidence too small to
        trust).

        The floor is `WINDOW_OVERLAP_ROWS` consecutive exact matches, or half the
        shorter list when that is smaller. Stored rows carry the conversation's
        own text, so twenty consecutive matches are a fingerprint; one match is a
        coincidence, and splicing unrelated turns together is worse than refusing
        a page the note explains. The largest usable overlap wins — it is both
        the best evidence and the one that keeps the most stored turns.
        """
        shortest = min(len(stored), len(incoming))
        if not shortest:
            return 0
        floor = max(1, min(shortest // 2, WINDOW_OVERLAP_ROWS))
        for overlap in range(shortest, floor - 1, -1):
            if stored[-overlap:] == incoming[:overlap]:
                return overlap
        return 0

    @api.model
    def _replace_transcript(self, session, messages, authoritative=False,
                            fetch_limit=None):
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

        ``fetch_limit`` is how many rows the caller asked the server for, and it
        adds the third rule (see the block comment above): a page that FILLS the
        limit is possibly partial, whatever its length relative to the mirror.

        * A page that continues the mirror — it starts with what is stored, or
          repeats the mirror's tail after the window slid — is applied, with the
          stored turns the window slid past kept in front of it, so nothing
          already mirrored is dropped.
        * A page at the limit that does not continue the mirror is refused,
          because the limit rather than a rewind is the likely reason and
          replacing with it would delete turns that still exist on the server.

        Either way the limit is disclosed in the returned note, so a transcript
        that is possibly a tail is never presented as the whole conversation.
        Pass ``fetch_limit=None`` where the answer is known to be whole — an
        authoritative caller, or a list this module built itself.

        ``messages`` must be the RAW list the server sent: ``_render`` drops
        Turnstone's system plumbing, so a 200-row page can store fewer turns, and
        measuring the limit against the stored rows would miss a truncated page.

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
        page = self._render(session, messages)
        existing = self.sudo().search(
            [("session_id", "=", session.id)], order="sequence, id"
        )
        stored = [(rec.role, rec.body or "", rec.tool_names or False,
                   rec.is_guard_notice) for rec in existing]
        incoming = self._comparable(page)

        # Did the server fill the page it was allowed to send? Measured on the
        # RAW list, because that is the list the limit bounds: `_render` drops
        # Turnstone's system plumbing, so a 200-row page can store fewer turns.
        at_limit = bool(fetch_limit) and len(messages) >= fetch_limit
        note = (
            self._partial_response_note(session, fetch_limit, len(messages))
            if at_limit else False
        )

        if not authoritative and stored:
            if not incoming:
                return _(
                    "Turnstone returned no message history, so the %(kept)s stored "
                    "turn(s) were kept rather than cleared. A node that has not "
                    "loaded a session's history yet answers this way.",
                ) % {"kept": len(stored)}
            if incoming == stored:
                # The same transcript, row for row. Nothing to write — but the
                # disclosure still belongs on the session, or the next poll would
                # clear the note this one wrote and the transcript would look
                # complete again two seconds later.
                return note
            if incoming == stored[:len(incoming)]:
                # The page starts with the whole mirror, so it either IS the
                # mirror (handled above) or adds turns to its end. Either way it
                # is applied as given — growth, or a rewind of zero, is never a
                # partial view. A page that FILLS the limit and starts with the
                # mirror takes this branch too: nothing stored is dropped, and
                # `note` already says the conversation may be longer.
                if len(incoming) == len(stored):
                    return note  # nothing to write
                existing.unlink()
                self.sudo().create(self._numbered(session, page))
                return note
            if len(incoming) < len(stored) and not at_limit:
                # Shorter and not a prefix: a bounded/tail-only response, which
                # drops the head. A rewind drops the tail, so it is a prefix and
                # never reaches here.
                return _(
                    "Turnstone returned %(got)s of %(kept)s stored turns, and not "
                    "as the beginning of this session's transcript — a partial or "
                    "bounded response. The stored transcript was kept.",
                ) % {"got": len(incoming), "kept": len(stored)}

            if not at_limit:
                # Under the limit the response IS the whole transcript, so a
                # differing list of the same length or longer is applied as
                # given — that is what "replace, not append" means, and it is the
                # behaviour every caller had before this rule existed.
                existing.unlink()
                self.sudo().create(self._numbered(session, page))
                return note

            # At the limit and not a prefix of the mirror: either the window slid
            # forward (normal on a long conversation) or this page does not
            # belong here. Stitch when the overlap proves continuity; otherwise
            # keep every stored turn and say why, because the limit — not a
            # rewind — is the likely reason the page does not line up.
            overlap = self._overlap(stored, incoming)
            if not overlap:
                return self._partial_response_note(
                    session, fetch_limit, len(messages), kept=len(stored),
                )
            # The overlapping rows are in BOTH lists, so only the stored turns
            # the window slid past are prepended; every page row is kept.
            keep = len(stored) - overlap
            page = [{
                "session_id": session.id,
                "sequence": 0,
                "role": rec.role,
                "body": rec.body or False,
                "tool_names": rec.tool_names or False,
                "is_guard_notice": rec.is_guard_notice,
            } for rec in existing[:keep]] + page
            if keep:
                note = self._partial_response_note(
                    session, fetch_limit, len(messages), kept=keep, stitched=True,
                )
            # `keep == 0` means the page already starts with everything stored:
            # ordinary growth, and the plain disclosure computed above is right.

        existing.unlink()
        if page:
            self.sudo().create(self._numbered(session, page))
        return note

    @staticmethod
    def _numbered(session, rows):
        """Give a finished row list one dense sequence per position.

        `_render` numbers the page from zero, and a stitched list starts with
        turns kept from an earlier one, so the halves must not keep their own
        counters: the chat reads by `sequence`, and two rows sharing a number
        interleave. Numbering after composition is the single place that decides
        the order.
        """
        for position, row in enumerate(rows):
            row["session_id"] = session.id
            row["sequence"] = position
        return rows
