"""A transcript that fills the fetch limit is possibly partial, and says so.

`GET /cluster/ws/{id}/detail` answers with a **tail** (`message_limit`, default
20, clamped 200) and carries no `total`, no `has_more` and no offset: the only
signal that a page might be cut is that its length equals the limit. The
head/tail guard OTI-5744 added refuses a *shorter* list that is not a prefix,
which is the right rule and cannot fire here — a rolling window never changes
length. OTI-5763 measured the consequence on a real Turnstone 1.8.3: a mirror
that grew to 20 and stayed there while the server held 26, the user's first
message gone, `sync_note` empty, `remote_gone` false.

So OTI-5772 adds a third state, "at the limit, therefore possibly partial", and
three things follow from it:

* the page is **applied** only when it starts with everything already stored, so
  turns are added and none are dropped;
* a page that only continues the mirror after the window slid is **stitched**
  onto the stored turns it slid past — a long conversation keeps updating
  instead of freezing at 200 turns, and nothing mirrored is lost;
* any other page at the limit is **refused** (nothing deleted) and `sync_note`
  explains it, because the limit rather than a rewind is why it does not line up.

Every case here goes through `sync_from_server` and the real `TurnstoneClient`
against the mock server, so the request really carries `message_limit=200` and
the response really went over HTTP. `_replace_transcript` is never called
directly except where the test is about the limit arithmetic itself: the claim
under test is what a *sync* does with a page at the limit, and a stub that hands
back a canned list cannot show that the limit was even requested.

The last three cases are the ones that must keep working: a page under the limit
still replaces the mirror wholesale (a rewind drops the tail and that is
legitimate), and an authoritative sync still applies whatever it is given.
"""

from unittest.mock import patch

from odoo.addons.turnstone_integration.models.turnstone_client import (
    SESSION_DETAIL_MESSAGE_LIMIT,
)
from odoo.addons.turnstone_integration.models.turnstone_message import (
    WINDOW_OVERLAP_ROWS,
)
from odoo.tests import tagged

from .common import MockTurnstone, TurnstoneTransactionCase

WS_ID = "ws-limit-1"


def _role(n):
    """The speaker of turn `n`. Even turns are the user's, so a fixture's parity
    is one function rather than a `% 2` repeated in every assertion."""
    return "user" if n % 2 == 0 else "assistant"


def _transcript(start, count):
    """`count` distinct turns beginning at `start`, alternating speaker.

    Distinct text per row on purpose: the at-the-limit rules compare stored rows
    against the page, so a fixture where every assistant turn reads "ok" would
    make a shifted window indistinguishable from an unchanged one.
    """
    return [
        {"role": _role(n), "content": "turn %d" % n}
        for n in range(start, start + count)
    ]


@tagged("post_install", "-at_install")
class TestTranscriptAtFetchLimit(TurnstoneTransactionCase):
    """OTI-5772: at the limit is a third state, disclosed and never destructive."""

    def setUp(self):
        super().setUp()
        self.session = self.make_session(
            name="At-limit session", ws_id=WS_ID, kind="interactive",
        )

    # -- helpers ------------------------------------------------------------

    def _seed(self, messages, kind="interactive"):
        """Plant a workstream on the mock with this transcript."""
        return self.mock.seed_transcript(WS_ID, messages, kind=kind)

    def _bodies(self, session=None):
        session = session or self.session
        return [(rec.role, rec.body) for rec in session.message_ids]

    def _sync(self, session=None):
        return (session or self.session).sync_from_server()

    # -- the limit is the real one -----------------------------------------

    def test_the_limit_the_sync_judges_against_is_the_one_it_asks_for(self):
        """The number sync judges a page by must be the number the client sends.

        A second copy of "200" would drift: the client would ask for one size and
        the guard would judge another, and the failure would look like a
        transcript bug rather than a constant one. `sync_from_server` passes this
        same constant to the guard, and the last case in this module proves the
        request really carries it — so this pins the value itself against the
        server's documented clamp.
        """
        self.assertEqual(SESSION_DETAIL_MESSAGE_LIMIT, 200,
                         "the detail endpoint clamps message_limit to 200")

    # -- cold start ---------------------------------------------------------

    def test_a_cold_mirror_at_the_limit_records_that_it_may_be_partial(self):
        """AC 1 and 3 (cold start): the rows are mirrored, and the note says why.

        A first sync of a conversation that fills the page cannot tell a whole
        transcript from a truncated one, because the server says nothing. Both
        honest options are bad if taken alone — refuse and the user sees an empty
        chat, apply silently and the user reads a tail as the whole conversation.
        So: apply (the rows are all that will ever be served), and record that
        the conversation may be longer.
        """
        self._seed(_transcript(0, SESSION_DETAIL_MESSAGE_LIMIT))
        result = self._sync()

        self.assertEqual(len(self.session.message_ids), SESSION_DETAIL_MESSAGE_LIMIT,
                         "the page must be mirrored, not refused into an empty chat")
        self.assertEqual(self._bodies()[0], ("user", "turn 0"),
                         "the first row served must be the first row stored")
        note = result["sync_note"]
        self.assertTrue(note, "an at-the-limit page must never be silent")
        self.assertIn("200", note, "the note must name the limit that was hit")
        self.assertIn("may be longer", note)
        self.assertIn("no deeper endpoint", note,
                      "an interactive session has no history endpoint; the note "
                      "must not promise one")
        self.assertFalse(result["remote_gone"])

    def test_a_page_under_the_limit_is_not_flagged(self):
        """The disclosure is for the limit, not for every sync.

        Without this, "possibly partial" would be printed on every short chat and
        the note would stop meaning anything.
        """
        self._seed(_transcript(0, SESSION_DETAIL_MESSAGE_LIMIT - 1))
        result = self._sync()

        self.assertEqual(len(self.session.message_ids),
                         SESSION_DETAIL_MESSAGE_LIMIT - 1)
        self.assertFalse(result["sync_note"],
                         "one row under the limit is a complete answer")

    # -- grown mirror: the rolling window ----------------------------------

    def test_a_grown_mirror_keeps_its_head_when_the_window_slides(self):
        """AC 1 and 2, and the shape the head/tail guard cannot see.

        The mirror holds the first 200 turns. Five more arrive, so the server's
        200-row page now starts at turn 5 and ends at turn 204 — the same length
        as before. A length comparison sees nothing at all; a wholesale replace
        would drop turns 0-4, which is exactly the observed real-server loss. The
        page continues the mirror after a five-row slide, so it is stitched: 205
        rows, the head intact, and the note says the page was a window.
        """
        self._seed(_transcript(0, SESSION_DETAIL_MESSAGE_LIMIT))
        self._sync()
        self.assertEqual(len(self.session.message_ids), SESSION_DETAIL_MESSAGE_LIMIT)
        head_before = self._bodies()[0]
        self.assertEqual(head_before, ("user", "turn 0"))

        # Five more turns: the page the server serves is now 5..204.
        self.mock.seed(ws_id=WS_ID, append=True, kind="interactive",
                       messages=_transcript(SESSION_DETAIL_MESSAGE_LIMIT, 5))
        result = self._sync()

        bodies = self._bodies()
        self.assertEqual(len(bodies), SESSION_DETAIL_MESSAGE_LIMIT + 5,
                         "the five older turns must be kept and the new ones added")
        self.assertEqual(bodies[0], ("user", "turn 0"),
                         "the user's first message must survive a rolling window")
        last = SESSION_DETAIL_MESSAGE_LIMIT + 4
        self.assertEqual(bodies[-1], (_role(last), "turn %d" % last),
                         "the newest turn must be mirrored")
        self.assertEqual(bodies, [(_role(n), "turn %d" % n)
                                  for n in range(SESSION_DETAIL_MESSAGE_LIMIT + 5)],
                         "the stitched transcript must be the conversation in "
                         "order, with nothing duplicated and nothing missing")
        note = result["sync_note"]
        self.assertIn("kept in front", note)
        self.assertIn("200", note)
        self.assertFalse(result["remote_gone"])

    def _rewind_then_continue(self, turns, fresh):
        """Drive the mock to a full page that shares nothing with the mirror.

        A rewind far enough to leave only a handful of rows, then a batch of new
        turns large enough to refill the page, leaves the server serving a page
        made entirely of turns the mirror has never seen. That is a real
        sequence — a user rewinds and keeps going — and it is the one that
        produces a page at the limit which cannot be matched to the mirror at
        all.
        """
        self.connection._client().rewind(WS_ID, turns=turns, kind="interactive")
        self.mock.seed(ws_id=WS_ID, append=True, kind="interactive",
                       messages=[{"role": _role(n), "content": "fresh %d" % n}
                                 for n in range(fresh)])

    def test_a_window_that_does_not_line_up_is_refused_not_replaced(self):
        """AC 1 and 2: an unmatched page at the limit deletes nothing.

        A page at the limit that shares no turns with the mirror could be a
        server-side rewind and rewrite, a body cut some other way, or a page from
        a different conversation — and the mirror cannot tell which. Replacing
        would silently discard turns that may still exist, so the whole mirror is
        kept and the note says the page could not be matched to it.
        """
        self._seed(_transcript(0, SESSION_DETAIL_MESSAGE_LIMIT))
        self._sync()
        before = self._bodies()

        # Rewind 90 turns (180 rows) -> 20 rows left; then 240 new turns, so the
        # 200-row page served is entirely "fresh".
        self._rewind_then_continue(turns=90, fresh=240)
        result = self._sync()

        self.assertEqual(self._bodies(), before,
                         "an unmatched page must not delete a single stored turn")
        note = result["sync_note"]
        self.assertIn("did not continue", note)
        self.assertIn("%d" % SESSION_DETAIL_MESSAGE_LIMIT, note)
        self.assertIn("stored transcript was kept", note)

    def test_an_unchanged_at_limit_page_keeps_its_note_on_every_poll(self):
        """The note must survive the next poll, or it is invisible in practice.

        The chat polls every couple of seconds. If a sync with no new turns
        cleared `sync_note`, the disclosure would be visible for one poll and
        gone by the time anyone looked — the transcript would read as complete.
        """
        self._seed(_transcript(0, SESSION_DETAIL_MESSAGE_LIMIT))
        self._sync()
        for _ in range(3):
            result = self._sync()
            self.assertTrue(result["sync_note"],
                            "a repeated at-the-limit poll must keep disclosing it")
        self.assertEqual(len(self.session.message_ids), SESSION_DETAIL_MESSAGE_LIMIT,
                         "and it must not rewrite or duplicate the rows")

    # -- what must not change ----------------------------------------------

    def test_a_growing_transcript_under_the_limit_still_replaces(self):
        """AC 2: the ordinary case is untouched. Growth is applied.

        A regression here would be a fix that refuses everything, so this is the
        control: the mirror follows a conversation that is still well under the
        page size, and `sync_note` stays empty.
        """
        self._seed(_transcript(0, 10))
        self._sync()
        self.assertEqual(len(self.session.message_ids), 10)
        self.assertFalse(self.session.sync_note)

        self.mock.seed(ws_id=WS_ID, append=True, kind="interactive",
                       messages=_transcript(10, 4))
        result = self._sync()

        self.assertEqual(len(self.session.message_ids), 14)
        self.assertEqual(self._bodies()[-1], ("assistant", "turn 13"))
        self.assertFalse(result["sync_note"])

    def test_an_edited_transcript_under_the_limit_is_still_replaced(self):
        """AC 2, the other half: a same-length change under the limit applies.

        Turnstone can retry a turn, which replaces a reply in place rather than
        shortening the list. That response is the whole transcript — it is not a
        window, because it is under the page size — so it must replace the
        mirror. The at-the-limit rules must not leak into this case: stitching a
        change here would leave the retracted reply on screen next to its
        replacement.
        """
        self._seed(_transcript(0, 10))
        self._sync()
        self.assertEqual(self._bodies()[-1], ("assistant", "turn 9"))

        retired = _transcript(0, 10)
        retired[9] = {"role": "assistant", "content": "turn 9, retried"}
        self.mock.seed(ws_id=WS_ID, append=True, kind="interactive",
                       messages=[retired[9]])
        # The mock appends, so drop the row it superseded and read the transcript
        # back: a retry replaces a reply in place on the real server.
        self.connection._client().rewind(WS_ID, turns=1, kind="interactive")
        self.mock.seed(ws_id=WS_ID, append=True, kind="interactive",
                       messages=_transcript(8, 1) + [retired[9]])
        result = self._sync()

        self.assertEqual(len(self.session.message_ids), 10,
                         "a same-length replacement must not grow the mirror")
        self.assertEqual(self._bodies()[-1], ("assistant", "turn 9, retried"),
                         "the retracted reply must be gone, not kept alongside")
        self.assertNotIn(("assistant", "turn 9"), self._bodies())
        self.assertFalse(result["sync_note"])

    def test_a_rewind_under_the_limit_still_drops_the_tail(self):
        """AC 2: rewinding is legitimate, and must still work.

        A rewind shortens the remote transcript by dropping its *tail*, so the
        shortened list is a prefix of the mirror. That is the one shortening the
        guard applies rather than refuses, and a fix that treated every shorter
        list as partial would make rewind a no-op.
        """
        self._seed(_transcript(0, 12))
        self._sync()
        self.assertEqual(len(self.session.message_ids), 12)

        self.connection._client().rewind(WS_ID, turns=2, kind="interactive")
        result = self.session.sync_from_server(authoritative=True)

        self.assertEqual(len(self.session.message_ids), 8,
                         "two user turns and their replies must be gone")
        self.assertEqual(self._bodies(),
                         [(_role(n), "turn %d" % n) for n in range(8)])
        self.assertFalse(result["sync_note"])

    def test_an_authoritative_sync_applies_a_page_at_the_limit(self):
        """A rewind on a long conversation still applies, and is still disclosed.

        `action_rewind` passes `authoritative=True` because it just changed the
        remote transcript itself. At the limit that response is still possibly a
        tail, so the note remains — but the rows must be applied, or rewinding a
        long chat would appear to do nothing.
        """
        self._seed(_transcript(0, SESSION_DETAIL_MESSAGE_LIMIT))
        self._sync()

        # The server now serves a full page that does not continue the mirror at
        # all. Without `authoritative` that is exactly the shape refused above.
        self._rewind_then_continue(turns=90, fresh=240)
        result = self.session.sync_from_server(authoritative=True)

        self.assertEqual(len(self.session.message_ids), SESSION_DETAIL_MESSAGE_LIMIT,
                         "an authoritative sync applies the page it is given")
        # The page served is the last 200 of 260 rows, i.e. fresh 40..239.
        self.assertEqual(self._bodies()[0], (_role(40), "fresh 40"),
                         "authoritative means the page it was given IS the truth")
        self.assertTrue(result["sync_note"],
                        "a page at the limit is still possibly partial, even here")


@tagged("post_install", "-at_install")
class TestAtFetchLimitStitching(TurnstoneTransactionCase):
    """The stitching arithmetic on its own, where a 200-row fixture is too slow.

    These cases drive `_replace_transcript` with a small `fetch_limit` so the
    window can slide by a few rows without building 200-row transcripts through
    HTTP for every scenario. The behaviour is the same code path; the cases above
    prove it is reached from a real sync.
    """

    def setUp(self):
        super().setUp()
        self.session = self.make_session(name="Stitch session", ws_id="ws-stitch")
        self.Transcript = self.env["turnstone.message"]

    def _apply(self, messages, limit, authoritative=False):
        return self.Transcript._replace_transcript(
            self.session, messages, authoritative=authoritative,
            fetch_limit=limit,
        )

    def _bodies(self):
        return [rec.body for rec in self.session.message_ids]

    def test_a_small_window_slides_and_stitches(self):
        """Growth by one row at a four-row limit keeps the dropped row."""
        limit = 4
        self._apply(_transcript(0, limit), limit)
        self.assertEqual(self._bodies(), ["turn %d" % n for n in range(4)])

        note = self._apply(_transcript(1, limit), limit)

        self.assertEqual(self._bodies(), ["turn %d" % n for n in range(5)],
                         "the row the window slid past must be kept in front")
        self.assertTrue(note)
        self.assertIn("kept in front", note)

    def test_the_stitched_rows_carry_dense_sequences(self):
        """Order is by `sequence`, so a stitched list must be numbered once.

        The kept rows come from the database and the page's rows come from
        `_render`, which numbers from 0 — so without renumbering, both halves
        would start at 0 and the chat would interleave them.
        """
        limit = 4
        self._apply(_transcript(0, limit), limit)
        self._apply(_transcript(2, limit), limit)

        self.assertEqual(
            [rec.sequence for rec in self.session.message_ids],
            list(range(6)),
            "sequence must be dense and increasing across the stitch",
        )
        self.assertEqual(self._bodies(), ["turn %d" % n for n in range(6)])

    def test_an_overlap_shorter_than_the_floor_is_not_continuity(self):
        """A one-row coincidence must not stitch two stretches together.

        `WINDOW_OVERLAP_ROWS` exists because a shifted window and a coincidence of
        one row look identical to a naive comparison — and stitching a coincidence
        would silently splice unrelated turns into the transcript.
        """
        limit = 8
        self._apply(_transcript(0, limit), limit)

        # A full page from somewhere else that ends on the mirror's last row — the
        # coincidence a naive "does it overlap at all" test would accept.
        stranger = [
            {"role": "user", "content": "stranger %d" % n} for n in range(limit - 1)
        ] + [{"role": "assistant", "content": "turn 7"}]
        note = self._apply(stranger, limit)

        self.assertEqual(self._bodies(), ["turn %d" % n for n in range(limit)],
                         "an unprovable page must not touch the stored transcript")
        self.assertIn("did not continue", note)

    def test_the_overlap_finder_reads_the_seam(self):
        """`_overlap` counts leading page rows that are the mirror's tail.

        Asserted directly because it is the whole continuity test: an off-by-one
        here would either drop a stored turn or duplicate one, and both are the
        same bug the guard exists to prevent.
        """
        overlap = self.Transcript._overlap
        stored = [("user", "turn %d" % n, False, False) for n in range(25)]

        self.assertEqual(
            overlap(stored, stored[3:]),
            len(stored) - 3,
            "a window slid by three repeats every stored row but the first three, "
            "so three stored turns must be kept in front of it",
        )
        self.assertEqual(
            overlap(stored, [("user", "fresh %d" % n, False, False)
                             for n in range(30)]),
            0,
            "a page sharing nothing with the mirror is not continuity",
        )
        self.assertEqual(
            overlap(stored, [("user", "other", False, False)] * 24 + [stored[-1]]),
            0,
            "one shared row is below the floor, so it is not continuity",
        )

    def test_a_page_that_only_shares_its_last_row_is_refused(self):
        """The floor protects the real path too, not just the helper."""
        limit = 8
        self._apply(_transcript(0, limit), limit)
        stranger = [{"role": "user", "content": "stranger %d" % n}
                    for n in range(limit)] + [
            {"role": "user", "content": "turn %d" % (limit - 1)}
        ]
        note = self._apply(stranger, limit)

        self.assertEqual(len(self.session.message_ids), limit,
                         "the stored transcript must be untouched")
        self.assertIn("did not continue", note)

    def test_the_overlap_floor_is_a_real_constant(self):
        """The floor is named and documented rather than a literal in the loop."""
        self.assertGreaterEqual(WINDOW_OVERLAP_ROWS, 2,
                                "one matching row is a coincidence, not continuity")
        self.assertLess(WINDOW_OVERLAP_ROWS, SESSION_DETAIL_MESSAGE_LIMIT,
                        "the floor must be reachable inside one default page")

    def test_a_coordinator_is_told_where_the_deeper_history_is(self):
        """The note names the deeper door only where one exists (OTI-5763)."""
        session = self.make_session(
            name="Coordinator", ws_id="ws-stitch-coord", kind="coordinator",
        )
        note = self.Transcript._replace_transcript(
            session, _transcript(0, 3), fetch_limit=3,
        )

        self.assertIn("/history?limit=500", note)
        self.assertIn("coordinator", note)

    def test_an_interactive_session_is_not_promised_a_deeper_door(self):
        """An interactive session has no history endpoint — the note must not lie."""
        note = self._apply(_transcript(0, 3), 3)

        self.assertNotIn("/history?limit=500", note)
        self.assertIn("no deeper endpoint", note)


@tagged("post_install", "-at_install")
class TestAtFetchLimitThroughTheClient(TurnstoneTransactionCase):
    """The page size is the client's, and only the detail call sends it.

    Pinned here as well as in `test_session_detail_limit` because these cases
    depend on it: if the client stopped asking for 200, every at-the-limit test
    above would be asserting about a page size that never came back.
    """

    def test_the_sync_asks_the_mock_for_the_limit(self):
        self.mock.seed_transcript("ws-asked", _transcript(0, 3))
        session = self.make_session(name="Asked", ws_id="ws-asked")
        session.sync_from_server()

        asked = [
            row for row in self.mock.request_log(path_prefix="/v1/api/cluster/ws/")
            if "/detail" in row.get("path", "")
        ]
        self.assertTrue(asked, "the sync must have asked the mock for a detail")
        self.assertEqual(asked[-1].get("query", {}).get("message_limit"),
                         "%d" % SESSION_DETAIL_MESSAGE_LIMIT,
                         "the request must carry the limit the guard judges by")
