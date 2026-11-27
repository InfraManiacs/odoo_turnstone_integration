"""Every session detail read asks for the server's maximum message tail.

``GET /cluster/ws/{id}/detail`` takes one query parameter, ``message_limit``:
*"Max conversation rows in the tail (default 20, clamped to 200)"*. Two things
about it are easy to miss and both cost real history:

* the default is **20**, and the rows are the **tail** -- the last 20 turns;
* the response carries no ``total``, ``has_more`` or ``offset``, so a caller
  that receives 20 rows cannot tell a tail from a whole transcript.

``TurnstoneClient.session_detail`` used to send no limit at all, so every
response was the last 20 turns and a mirror built from one was quietly
truncated from its first sync. Measured on a real Turnstone 1.8.3 (OTI-5763):
82 of 107 workstreams answered at exactly 20 rows, all 82 were truncated when
asked for 500, and 7,698 turns were never served at the default. OTI-5770 sends
the clamp on the detail call and nowhere else; disclosing what is still a tail
at 200 is OTI-5772's business, not this one's.

These cases assert on the URL the client builds, because the request is the
whole observable change: a captured ``Request`` is the only place the parameter
exists, and a stub that answers whatever it is asked would pass with or without
it. They are also the guard for the ticket's other half -- *no other client
behaviour changes* -- since a limit added to ``_request``'s shared behaviour
would appear on every endpoint at once, and that is what ``test_no_other_call``
catches.
"""

import json
import urllib.parse
from io import BytesIO
from unittest.mock import patch

from odoo.addons.turnstone_integration.models.turnstone_client import (
    SESSION_DETAIL_MESSAGE_LIMIT,
    TurnstoneClient,
)
from odoo.tests import TransactionCase, tagged


def _detail_body(messages=None):
    """A detail payload in the shape the real endpoint answers with."""
    return {
        "persisted": {"ws_id": "ws-1", "state": "idle", "kind": "interactive"},
        "live": {"state": "idle", "tool_calls": 0, "tokens": 0, "context_ratio": 0.0},
        "messages": messages if messages is not None else [],
    }


def _transcript(count):
    """`count` distinct conversation rows, so a trimmed list is visible."""
    return [
        {"role": "user" if n % 2 == 0 else "assistant",
         "content": "turn %d" % n, "_event_id": n}
        for n in range(count)
    ]


class RecordingTransport:
    """A stand-in for ``urllib.request.urlopen`` that records every request.

    Answers each call with a canned JSON body so the client can finish, and
    keeps the method and the parsed URL of every request. Parsed rather than
    whole-URL on purpose: an assertion on the query *string* would be a test of
    parameter order, and an assertion on the URL would break the moment another
    legitimate parameter is added.
    """

    def __init__(self, body=None):
        self.body = body if body is not None else {}
        self.requests = []

    def __call__(self, request, *args, **kwargs):
        url = getattr(request, "full_url", str(request))
        method = request.get_method() if hasattr(request, "get_method") else "GET"
        self.requests.append((method, urllib.parse.urlsplit(url)))
        return BytesIO(json.dumps(self.body).encode())

    # -- assertions helpers -------------------------------------------------

    def url(self, index=-1):
        return self.requests[index][1]

    def query(self, index=-1):
        """The request's query string as a dict of lists, like a real parse."""
        return urllib.parse.parse_qs(self.requests[index][1].query, keep_blank_values=True)

    def all_queries(self):
        return [
            urllib.parse.parse_qs(url.query, keep_blank_values=True)
            for _method, url in self.requests
        ]


@tagged("post_install", "-at_install")
class TestSessionDetailMessageLimit(TransactionCase):
    """OTI-5770: the detail call asks for 200 rows, and no other call changes."""

    def setUp(self):
        super().setUp()
        # A real client, not a stub: the claim under test is about what it puts
        # on the wire, so nothing between the call and the URL may be faked.
        self.client = TurnstoneClient("http://turnstone.invalid", "test-token")

    def _patched(self, transport):
        return patch("urllib.request.urlopen", transport)

    def test_the_limit_is_the_servers_hard_clamp(self):
        """The number is named, and it is the clamp rather than a guess.

        Asking for more than 200 does not fail -- the server clamps silently --
        which is exactly why the constant is pinned here instead: a later
        "let's ask for 500 so we get everything" edit would look like it worked
        and would be a lie in the code.
        """
        self.assertEqual(SESSION_DETAIL_MESSAGE_LIMIT, 200,
                         "the detail endpoint clamps message_limit to 200")
        self.assertIsInstance(SESSION_DETAIL_MESSAGE_LIMIT, int,
                              "urlencode would stringify a str, but a number is the contract")

    def test_detail_asks_for_the_clamp_on_every_call(self):
        """AC 1: every detail request carries message_limit=200."""
        transport = RecordingTransport(_detail_body())
        with self._patched(transport):
            self.client.session_detail("ws-1")
            self.client.session_detail("ws-2")

        self.assertEqual(len(transport.requests), 2,
                         "one call must be one request, not a retry")
        for index, ws_id in enumerate(("ws-1", "ws-2")):
            self.assertEqual(
                transport.url(index).path,
                "/v1/api/cluster/ws/%s/detail" % ws_id,
                "the endpoint's path and the ws_id must not move",
            )
            self.assertEqual(
                transport.query(index), {"message_limit": ["200"]},
                "the detail call must send exactly message_limit=200, not the "
                "server's default of 20 and nothing else",
            )

    def test_a_full_page_comes_back_whole(self):
        """AC 2: 200 rows in means 200 rows out -- the client does not slice.

        A test that only counted rows after a 20-row response would pass on a
        client that truncated, so the response is exactly the limit and the
        assertion is on the actual rows: any client-side trimming (to 20, to
        the first page, to "what a display needs") shows up as a mismatch.
        """
        messages = _transcript(200)
        body = _detail_body(messages)
        transport = RecordingTransport(body)
        with self._patched(transport):
            detail = self.client.session_detail("ws-long")

        self.assertEqual(transport.query(), {"message_limit": ["200"]})
        self.assertEqual(detail, body, "the payload must come back as the server sent it")
        self.assertEqual(len(detail["messages"]), 200)
        self.assertEqual(detail["messages"], messages)
        self.assertEqual(detail["messages"][0]["content"], "turn 0",
                         "the first row must survive; a tail-only mirror loses it")

    def test_no_other_call_asks_for_a_message_limit(self):
        """The rest of the client is untouched.

        `message_limit` is a parameter of the detail endpoint alone. Putting it
        in `_request` -- the tempting one-line version of this fix -- would send
        it with every call, and the console would 400 the ones that do not
        declare it. So: no other request may carry it, and the calls that have
        query parameters of their own must keep exactly those.
        """
        transport = RecordingTransport({"workstreams": [], "total": 0})
        with self._patched(transport):
            self.client.whoami()
            self.client.cluster_workstreams()
            self.client.send("ws-1", "hello")
            self.client.approve("ws-1", True, kind="coordinator")
            self.client.rewind("ws-1", turns=1)
            self.client.close("ws-1")

        self.assertEqual(len(transport.requests), 6)
        for query in transport.all_queries():
            self.assertNotIn(
                "message_limit", query,
                "message_limit belongs to /cluster/ws/{id}/detail alone; sending "
                "it anywhere else is a 400 from the console",
            )
        self.assertEqual(
            transport.query(1), {"page": ["1"], "per_page": ["100"]},
            "cluster_workstreams' own parameters must be unchanged",
        )
        self.assertEqual(transport.query(2), {}, "a send carries its payload, not a query")
        self.assertEqual(transport.url(2).path, "/v1/api/route/workstreams/ws-1/send")
