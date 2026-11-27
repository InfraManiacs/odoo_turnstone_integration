"""In-memory state for the mock Turnstone server.

Everything the mock knows lives here and nowhere else: no database, no files,
no cache directory. The point of the mock is that a test can put it back to a
known shape in one call, so `reset()` is a first-class operation rather than a
testing afterthought. It is reachable three ways:

  * in-process (`state.reset()`, or the module-level `reset()`) when something
    imports this module directly,
  * over HTTP as `POST /v1/api/admin/mock/reset` (see server.py) for a test
    running in another container — an Odoo `TransactionCase` cannot import this
    file,
  * implicitly at import time, so a bare `python server.py` starts empty.

`STORE` is process-wide, not per server: two in-process servers share one
store on purpose, because the store is the thing a test resets.

State is plain JSON-able dictionaries, never classes. The HTTP layer returns
these dicts almost verbatim, later tickets add fields without touching the
container, and `snapshot()` can serialise anything here without a custom
encoder.

## The workstream record

One dict per workstream, keyed by id in `MockState.workstreams`. The fields
below are the contract later tickets build on (OTI-5734 creates them over HTTP,
OTI-5736 adds the transcript, OTI-5738 appends turns and moves the counters):

    id                   "ws-000001" — stable, unique, sequential per reset
    kind                 "coordinator" | "interactive"
    state                Turnstone state string: idle/thinking/attention/error/closed
    name                 the name the caller asked for
    title                the title Turnstone would generate from the first turn
    skill, persona       catalogue names or None
    model_alias          the alias it runs on, defaulting to DEFAULT_MODEL_ALIAS. The
                         routing proxy's `model` sets it explicitly; a coordinator
                         gets the node default because the console create endpoint
                         has no `model` field at all. Never None: reporting None would
                         say "this session has no model", which a real cluster does
                         not, and `controllers/main.py` harvests aliases from this feed.
    parent_ws_id         the coordinator that spawned this child, or None
    node_id              which node "hosts" it (always NODE_ID here)
    created_at, updated_at  epoch seconds (float)
    messages             list of message dicts — transcript, oldest first. Every
                         row is normalised by `normalise_message()`, so role,
                         content, timestamp (and tool_calls/tool_call_id when
                         present) always have one spelling. A session with no
                         turns holds `[]`, never None.
    tokens, tool_calls   counters mirrored into the session header
    context_ratio        float 0..1
    activity, activity_state  short human strings for Live Activity
    pending_approval_details  list of approval payloads (empty unless forced)
    attachments          list of attachment dicts
    closed               bool, mirrors state == "closed"
    meta                 free-form dict; later tickets stash test-only knobs

A message dict, as `turnstone_client.py::_sync_messages` parses it:

    role        "user" | "assistant" | "tool" | "system"
    content     string, or a list of {"type": "text", "text": ...} parts
    tool_calls  optional list of {"id", "type", "function": {"name", "arguments"}}
    tool_call_id  optional, links a "tool" result to its call

OTI-5736 fixed the exact shapes below (see "The transcript contract" above);
the point of writing them down here is that nothing in the transcript is a UI
concern — the mock stores what the real API returns and the Odoo client does the
interpreting.

## The transcript contract (OTI-5736)

Every message the mock stores — however it arrived (a create's
`initial_message`, a seed, a planted transcript, a rewind's survivor) — is
normalised by `normalise_message()` on the way in, so there is exactly one
spelling of a turn and a test can never pass against a shape the addon does not
parse. The normaliser is the contract; the store never holds a half-shaped row.

    role         one of MESSAGE_ROLES. Refused if not: `turnstone_message._render`
                 silently SKIPS an unknown role, so accepting one would make a
                 seeded turn vanish from the mirror with no error anywhere.
    content      always present. A string, or a list of {"type": "text", "text"}
                 parts (a bare string in a part list is wrapped, because the
                 parser does `str(part.get("text") or "")` and would read "").
    timestamp    always present. Epoch seconds by default, or whatever the caller
                 passed (an int/float epoch or an ISO-8601 string). The real
                 transcript rows carry a time and a chat UI shows one; the addon's
                 parser ignores unknown keys, so this cannot disturb the mirror.
    tool_calls   optional, only when non-empty. Canonical OpenAI shape:
                 [{"id", "type": "function", "function": {"name", "arguments"}}]
                 with `arguments` a JSON STRING. A loose {name, arguments} entry is
                 upgraded, and a dict `arguments` is dumped, because
                 `_sync_linked_records` json.loads a string and would otherwise
                 lose the call's model name and ids.
    tool_call_id optional, only on a "tool" result that answers a call. `call_id`
                 is accepted as an input alias and normalised to `tool_call_id`.

`content` is the wire key even though the ticket that specified this calls it
"text": `turnstone_message._render` reads `msg.get("content")`, and inventing a
second spelling for the same value is how two halves of a transcript drift.

Nothing in this module implements agent behaviour, and nothing in it should:
no model call, no tool execution, no timing simulation. A canned reply is a
string read from the environment (OTI-5738, `DEFAULT_REPLY` with
`MOCK_TURNSTONE_REPLY` overriding it); that is the whole of it.
"""

import copy
import itertools
import json
import threading
import time

# The single node this mock pretends to be. Real Turnstone is a cluster and
# every workstream carries the node that hosts it; the Odoo client stores the
# value (turnstone_session.py:sync_from_server) and Live Activity shows it, so
# the field has to exist even though there is only ever one node here.
NODE_ID = "node-mock-1"

# Seeded catalogue. Small on purpose: enough for the new-session form and the
# agent picker to render, and named so nobody mistakes a mock entry for a real
# skill. Kinds matter — the module refuses to run a coordinator skill as an
# interactive session, so both kinds are present.
DEFAULT_SKILLS = (
    {
        "name": "mock-interactive",
        "description": "Mock skill for interactive sessions. Does nothing.",
        "kind": "interactive",
        "activation": "auto",
        "template_id": "mock",
        "enabled": True,
    },
    {
        "name": "mock-coordinator",
        "description": "Mock skill for coordinator sessions. Does nothing.",
        "kind": "coordinator",
        "activation": "auto",
        "template_id": "mock",
        "enabled": True,
    },
)

DEFAULT_PERSONAS = (
    {
        "name": "mock-default",
        "display_name": "Mock Default",
        "description": "The only persona the mock knows about.",
        "applies_to_kinds": ["interactive", "coordinator"],
        "is_default": True,
        "enabled": True,
    },
)

DEFAULT_MODELS = (
    {
        "alias": "mock-model",
        "provider": "mock",
        "context_window": 32000,
        "input_cost": 0.0,
        "output_cost": 0.0,
    },
)

# The alias a session runs on when the caller could not choose one.
#
# A coordinator is exactly that case: the console create endpoint has no `model`
# field, so a coordinator runs on the node's default. Reporting None instead
# would say "this session has no model", which is not what a real cluster says —
# and `controllers/main.py` harvests model aliases from the cluster feed, so a
# cluster of coordinators would suggest nothing in the new-session form.
DEFAULT_MODEL_ALIAS = DEFAULT_MODELS[0]["alias"]

# How many requests the log keeps. Bounded because a chat polls every couple of
# seconds for as long as a test runs; the counter keeps counting past the cap so
# "how many sends arrived" stays correct even when the log has rolled over.
REQUEST_LOG_LIMIT = 500

# The assistant reply this server gives every prompt (OTI-5738).
#
# A constant, not a completion: the mock runs no model, so the only honest reply
# is one a test can predict character for character. It names the variable that
# changes it, because the first question asked of any transcript is "where did
# that text come from". `server.Config` reads MOCK_TURNSTONE_REPLY and falls back
# here, so the default lives in one place.
DEFAULT_REPLY = (
    "Mock reply: no model was called. This text comes from MOCK_TURNSTONE_REPLY."
)


# `tokens` in the detail payload is a number the addon displays
# (`turnstone_session.token_count`), so a send has to move it: a reply that cost
# nothing makes the mirror show a conversation of zero size. There is no
# tokenizer here and there should not be (stdlib only, and a tokenizer would be a
# dependency pretending to be accuracy); this is the usual
# four-characters-per-token estimate, applied to both sides of a turn.
# Deterministic is the requirement — a test asserts this arithmetic, not the
# number a real model would burn.
def estimate_tokens(text):
    """A deterministic token estimate for one side of a turn. Never 0."""
    return max(1, (len(text) + 3) // 4)


# One forced approval cycle, shaped like the real `PendingApprovalDetail` from
# Turnstone 1.8.3's /openapi.json: an envelope per live cycle, with the tool call
# nested one level down under `items[]`.
#
# The nesting is load-bearing. `turnstone_session._flatten_approvals` reads
# `cycle_id` from the envelope and `func_name`/`preview` from the item, so a flat
# list of tool calls would render in the chat as an approval with no tool name —
# the panel would open and show nothing to decide about.
DEFAULT_APPROVAL_DETAILS = (
    {
        "cycle_id": "cycle-mock-1",
        "call_id": "call-mock-1",
        "judge_pending": False,
        "items": [
            {
                "call_id": "call-mock-1",
                "header": "Approval required",
                "preview": "mock_write(res_model='res.partner', res_id=1)",
                "func_name": "mock_write",
                "approval_label": "Write a partner",
                "needs_approval": True,
            }
        ],
    },
)

# The roles the addon mirrors (`turnstone_message._render`). Anything else is
# dropped there without a word, which is why the normaliser refuses it here
# instead: a seeded turn that vanishes in the mirror is a test debugging session
# nobody can win.
MESSAGE_ROLES = ("user", "assistant", "tool", "system")


# ---------------------------------------------------------------------------
# transcript rows
# ---------------------------------------------------------------------------
#
# Free functions rather than methods because they are pure: a caller can build a
# row without a store, and `MockState` uses the same code for every write path
# (create, seed, plant, append, rewind). One normaliser, one shape.


def _normalise_tool_call(call, message_index, call_index):
    """One tool call in the shape `turnstone_session._sync_linked_records` reads.

    That code looks for the name at `function.name` OR `name`, the arguments at
    `function.arguments` OR `arguments`, and the id at `id` OR `function.id`, and
    it json.loads the arguments when they arrive as a string — so all of those
    are accepted on the way in and one canonical form is stored. Getting this
    wrong is not cosmetic: the model name in the arguments is what a tool call is
    linked to in Odoo, and a call the parser could not read is a link that
    silently never appears.
    """
    if not isinstance(call, dict):
        raise ValueError(
            "tool call %d of message %d must be a JSON object" % (call_index, message_index)
        )
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = function.get("name") or call.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            "tool call %d of message %d must carry a function name"
            % (call_index, message_index)
        )
    arguments = function.get("arguments", call.get("arguments"))
    if arguments is None:
        arguments = "{}"
    elif isinstance(arguments, (dict, list)):
        # Canonical OpenAI/Turnstone carry the arguments as a JSON string; the
        # parser accepts a dict too, but storing a dict means the same turn has
        # two possible spellings and an assertion has to guess which one.
        arguments = json.dumps(arguments, sort_keys=True)
    elif not isinstance(arguments, str):
        raise ValueError(
            "tool call %d of message %d: arguments must be a JSON string or an object"
            % (call_index, message_index)
        )
    call_id = call.get("id") or function.get("id") or "call-%d-%d" % (message_index, call_index)
    return {
        "id": str(call_id),
        "type": call.get("type") or "function",
        "function": {"name": name, "arguments": arguments},
    }


def _normalise_content(body, message_index):
    """`content` as a string or a list of text parts.

    A list element that is a bare string is wrapped into a part: the parser does
    `str(part.get("text") or "")`, which would read a raw string as nothing at
    all and make the turn disappear. Absent content becomes "" rather than a
    missing key, so `content` is always safe to read.
    """
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if not isinstance(body, list):
        raise ValueError(
            "message %d: content must be a string or a list of text parts" % message_index
        )
    parts = []
    for part in body:
        if isinstance(part, str):
            parts.append({"type": "text", "text": part})
        elif isinstance(part, dict):
            parts.append(copy.deepcopy(part))
        else:
            raise ValueError(
                "message %d: each content part must be a string or an object" % message_index
            )
    return parts


def normalise_message(raw, index=0, default_timestamp=None):
    """One transcript row, canonical. Raises ValueError naming the mistake.

    `index` is only used to make the error message name the offending row and to
    name an unlabelled tool call; it is not stored. `text` is accepted as an
    input alias for `content` because the ticket that specified the transcript
    calls the body "text", but only `content` is ever written: the addon reads
    `msg.get("content")` and a second spelling would be a second source of truth.
    """
    if not isinstance(raw, dict):
        raise ValueError("message %d must be a JSON object" % index)
    role = raw.get("role")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("message %d must carry a role" % index)
    role = role.strip().lower()
    if role not in MESSAGE_ROLES:
        raise ValueError(
            "message %d: role must be one of %s, not %r"
            % (index, "/".join(MESSAGE_ROLES), raw.get("role"))
        )

    body = raw.get("content")
    if body is None and "text" in raw:
        body = raw.get("text")

    stamp = raw.get("timestamp", default_timestamp)
    if stamp is None:
        stamp = time.time()
    elif isinstance(stamp, bool) or not isinstance(stamp, (int, float, str)):
        raise ValueError(
            "message %d: timestamp must be epoch seconds or an ISO-8601 string" % index
        )

    tool_calls = raw.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise ValueError("message %d: tool_calls must be a list" % index)

    row = {
        "role": role,
        "content": _normalise_content(body, index),
        "timestamp": stamp,
    }
    if tool_calls:
        row["tool_calls"] = [
            _normalise_tool_call(call, index, i) for i, call in enumerate(tool_calls)
        ]
    # `call_id` is the spelling some real payloads use; `tool_call_id` is the one
    # `turnstone_message`/`turnstone_session` prefer, so that is what is stored.
    call_id = raw.get("tool_call_id") or raw.get("call_id")
    if call_id:
        row["tool_call_id"] = str(call_id)
    return row


def normalise_messages(messages, default_start=None):
    """A whole transcript, canonical and in order.

    Defaulted timestamps step forward by a millisecond per row from
    `default_start` (or now), so a transcript a test did not time still reads as
    a conversation in order. A row that carries its own timestamp keeps it
    exactly; the mock is not in the business of correcting a fixture.
    """
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    start = time.time() if default_start is None else float(default_start)
    rows = []
    for index, raw in enumerate(messages):
        rows.append(normalise_message(raw, index=index,
                                      default_timestamp=start + index * 0.001))
    return rows


def _message_text(row):
    """The plain text of a normalised row, for a title or a length check."""
    body = row.get("content")
    if isinstance(body, list):
        return " ".join(str(part.get("text") or "") for part in body).strip()
    return str(body or "").strip()


def _derived_title(row, limit=60):
    """A session title taken from its first turn, the way Turnstone makes one.

    Only used when a transcript arrived on a session that had no title, so a
    planted conversation shows a heading instead of an empty one. Truncated at a
    word boundary where possible; nothing depends on the exact text.
    """
    text = _message_text(row)
    if not text:
        return None
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return clipped + "…"


class MockState:
    """Thread-safe container for everything the mock server knows.

    The server is threaded (a poll and a send can overlap, exactly as they do
    against a real Turnstone), so every mutation takes `lock`. Reads return
    copies: a handler that mutates a record it read must put it back with
    `save()`. Without that rule, two request threads edit the same dict and a
    test failure shows up as an impossible state rather than a race.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.workstreams = {}
        self.requests = []
        self.request_seq = 0
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self):
        """Return to the post-startup shape and report what was cleared.

        Deliberately total: the counter goes back to zero so ids are
        deterministic, and the catalogue is re-seeded so a test that edited it
        cannot leak into the next one.
        """
        with self.lock:
            self.workstreams = {}
            self.requests = []
            self.request_seq = 0
            self._ids = itertools.count(1)
            self.skills = copy.deepcopy(list(DEFAULT_SKILLS))
            self.personas = copy.deepcopy(list(DEFAULT_PERSONAS))
            self.models = copy.deepcopy(list(DEFAULT_MODELS))
            self.attachments = {}
            self.started_at = time.time()
            return {
                "reset": True,
                "workstreams": 0,
                "skills": len(self.skills),
                "personas": len(self.personas),
                "models": len(self.models),
            }

    def next_workstream_id(self):
        """A stable, unique id. Sequential per reset, so assertions can name it.

        Skips ids a test pinned through `create_workstream(..., ws_id=...)`: the
        counter knows nothing about those, and handing out one that is already
        taken would silently overwrite a seeded workstream.
        """
        with self.lock:
            while True:
                candidate = "ws-%06d" % next(self._ids)
                if candidate not in self.workstreams:
                    return candidate

    # -- workstreams -------------------------------------------------------

    def create_workstream(self, kind, name=None, title=None, state=None,
                          skill=None, persona=None, model_alias=None,
                          parent_ws_id=None, messages=None, meta=None,
                          tokens=0, tool_calls=0, context_ratio=0.0,
                          activity="", activity_state="", ws_id=None):
        """Create and store one workstream record, returning a copy.

        `kind` is the only required argument and the only one validated here:
        whether a *request* was allowed to ask for that kind is the HTTP
        layer's job (the console creates coordinators, the route proxy creates
        interactive sessions), not the container's.

        `ws_id` pins the id, for a test that wants to name the workstream in an
        assertion instead of reading the id back. A pinned id that is already
        taken raises KeyError rather than replacing the existing record.
        """
        if kind not in ("coordinator", "interactive"):
            raise ValueError("kind must be 'coordinator' or 'interactive', not %r" % (kind,))
        # Normalised here rather than in each handler: every write path into a
        # transcript goes through this method (create, seed), and a row that
        # skipped the normaliser would be a second shape to reason about.
        rows = normalise_messages(messages)
        now = time.time()
        with self.lock:
            if ws_id is not None:
                if ws_id in self.workstreams:
                    raise KeyError("workstream already exists: %s" % ws_id)
                ws_id = str(ws_id)
            else:
                ws_id = self.next_workstream_id()
            record = {
                "id": ws_id,
                "kind": kind,
                "state": state or "idle",
                "name": name or ("Mock coordinator" if kind == "coordinator" else "Mock session"),
                "title": title,
                "skill": skill,
                "persona": persona,
                "model_alias": model_alias or DEFAULT_MODEL_ALIAS,
                "parent_ws_id": parent_ws_id,
                "node_id": NODE_ID,
                "created_at": now,
                "updated_at": now,
                "messages": copy.deepcopy(rows),
                "tokens": int(tokens or 0),
                "tool_calls": int(tool_calls or 0),
                "context_ratio": float(context_ratio or 0.0),
                "activity": activity or "",
                "activity_state": activity_state or "",
                "pending_approval_details": [],
                "attachments": [],
                "closed": False,
                "meta": copy.deepcopy(dict(meta or {})),
            }
            self.workstreams[ws_id] = record
            return copy.deepcopy(record)

    def get(self, ws_id):
        """One workstream, or None. A copy: mutate and `save()` it."""
        with self.lock:
            record = self.workstreams.get(ws_id)
            return copy.deepcopy(record) if record is not None else None

    def save(self, record):
        """Write a record back, bumping `updated_at`. Returns the stored copy."""
        with self.lock:
            current = self.workstreams.get(record["id"])
            if current is None:
                raise KeyError("no such workstream: %s" % record["id"])
            record = dict(record)
            record["updated_at"] = time.time()
            self.workstreams[record["id"]] = copy.deepcopy(record)
            return copy.deepcopy(record)

    def delete(self, ws_id):
        with self.lock:
            return self.workstreams.pop(ws_id, None) is not None

    def all(self):
        """Every workstream as a copy, oldest first."""
        with self.lock:
            return [copy.deepcopy(self.workstreams[k]) for k in self.workstreams]

    def list_workstreams(self, state=None, search=None, page=1, per_page=100):
        """One page of workstreams, oldest first, plus the unpaged total.

        Filtering and paging live here rather than in the handler because a
        listing endpoint means one thing by "page 2" and the record contract is
        defined in this module, so the query over it is too.

        `state` is an exact match on the Turnstone state string. `search`
        matches the id, name or title, case-insensitively — the three things a
        person types into a cluster search box. Both filters are applied
        **before** paging, so `total` is how many workstreams match, not how
        many the page holds.

        Order is creation order (oldest first), the same order `all()` gives and
        the same order ids are handed out in, so an assertion can name positions
        instead of sorting. A caller that wants newest-first sorts the page.
        """
        with self.lock:
            rows = [copy.deepcopy(self.workstreams[k]) for k in self.workstreams]
        if state:
            rows = [r for r in rows if r.get("state") == state]
        if search:
            needle = search.lower()
            rows = [
                r for r in rows
                if needle in str(r.get("id") or "").lower()
                or needle in str(r.get("name") or "").lower()
                or needle in str(r.get("title") or "").lower()
            ]
        total = len(rows)
        page = max(1, int(page or 1))
        per_page = max(1, int(per_page or 100))
        start = (page - 1) * per_page
        return {
            "rows": rows[start:start + per_page],
            "total": total,
            "page": page,
            "per_page": per_page,
        }

    def exists(self, ws_id):
        with self.lock:
            return ws_id in self.workstreams

    # -- transcript --------------------------------------------------------
    #
    # One set of operations over `record["messages"]`, used by the seed/plant
    # helpers, the history endpoint and rewind. They live here rather than in
    # server.py for the same reason `list_workstreams` does: the transcript is
    # part of the record contract this module defines, so the operations that
    # keep it honest are here and the HTTP layer only shapes the answer.
    #
    # Every one of them returns a copy, and every one refuses an unknown
    # workstream with KeyError rather than creating a half-record — a transcript
    # for a session nobody can reach is never what a test meant.

    def transcript(self, ws_id):
        """One session's messages, oldest first. `[]` for a session with none.

        Never None: `detail.get("messages") or []` is what the addon does, and an
        empty list is the answer that does not wipe a mirror. A session with no
        messages and a session that does not exist are different answers (empty
        list vs KeyError) so a test cannot mistake one for the other.
        """
        with self.lock:
            record = self.workstreams.get(ws_id)
            if record is None:
                raise KeyError("no such workstream: %s" % ws_id)
            return copy.deepcopy(record.get("messages") or [])

    def plant_transcript(self, ws_id, messages, replace=True):
        """Put a whole transcript on an existing session, as OTI-5736's seed helper.

        `replace=True` overwrites (a test arranging a known conversation before
        opening chat); `replace=False` appends to whatever is there (a test
        extending a transcript a create already started). Returns the stored
        transcript so a caller can assert on what was normalised.

        Seeding through `create_workstream(messages=...)` is the other way to get
        a transcript; this one is for a session that already exists, which is
        what "seed a transcript before opening chat" means when the session row
        comes from `POST /workstreams/new`.
        """
        rows = normalise_messages(messages)
        with self.lock:
            record = self.workstreams.get(ws_id)
            if record is None:
                raise KeyError("no such workstream: %s" % ws_id)
            record = copy.deepcopy(record)
            if not replace:
                rows = list(record.get("messages") or []) + rows
            record["messages"] = rows
            # The title is derived from the first turn on a real server, so a
            # planted transcript gives a session one — otherwise a session seeded
            # this way would show an empty heading next to a full conversation.
            if rows and not record.get("title"):
                record["title"] = _derived_title(rows[0])
            record["updated_at"] = time.time()
            self.workstreams[ws_id] = record
            return copy.deepcopy(rows)

    def append_message(self, ws_id, message):
        """Add one turn to a session's transcript and return the stored row.

        A single row. `send_turn()` is what a prompt goes through (two rows and
        the counters, in one lock); this is here for a fixture that wants to plant
        one row at a time, and it is what `plant_transcript` grows from.
        """
        row = normalise_message(message, index=0)
        with self.lock:
            record = self.workstreams.get(ws_id)
            if record is None:
                raise KeyError("no such workstream: %s" % ws_id)
            record = copy.deepcopy(record)
            rows = list(record.get("messages") or [])
            rows.append(row)
            record["messages"] = rows
            if not record.get("title"):
                record["title"] = _derived_title(rows[0])
            record["updated_at"] = time.time()
            self.workstreams[ws_id] = record
            return copy.deepcopy(row)

    def rewind(self, ws_id, turns=1):
        """Drop the last `turns` user turns and everything after them.

        A "turn" is a user message plus the assistant/tool rows answering it, so
        dropping one means finding the `turns`-th user row from the end and
        truncating there — not deleting the last N rows, which would leave half a
        turn and make the next reply read as a continuation of a prompt that no
        longer exists. The real endpoint clamps to the available count
        (RewindRequest: "Clamped to the available turn count"), so asking to drop
        five turns from a two-turn conversation empties it rather than failing.

        Returns `{dropped, remaining, turns}` where `dropped` counts the ROWS
        removed, because that is what an assertion about a transcript wants.
        """
        if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
            raise ValueError("turns must be an integer of 1 or greater")
        with self.lock:
            record = self.workstreams.get(ws_id)
            if record is None:
                raise KeyError("no such workstream: %s" % ws_id)
            record = copy.deepcopy(record)
            rows = list(record.get("messages") or [])
            cut = len(rows)
            seen = 0
            for position in range(len(rows) - 1, -1, -1):
                if rows[position].get("role") == "user":
                    seen += 1
                    cut = position
                    if seen >= turns:
                        break
            else:
                # Fewer user turns than asked for: everything goes. Reached only
                # when the loop ran out without hitting `turns`, including a
                # transcript with no user turn at all.
                cut = 0
            dropped = len(rows) - cut
            record["messages"] = rows[:cut]
            record["updated_at"] = time.time()
            self.workstreams[ws_id] = record
            return {"dropped": dropped, "remaining": len(record["messages"]),
                    "turns": turns}

    # -- prompt submission (OTI-5738) --------------------------------------

    def send_turn(self, ws_id, message, reply=DEFAULT_REPLY, force_approval=False):
        """Append a user turn and its canned assistant reply, as one operation.

        The whole point of this method, rather than two calls to
        `append_message()`, is the lock: transcript, counters, `updated_at` and
        the optional approval all move together. A chat polling detail while a
        send is in flight either sees the session before the send or both turns
        after it — never a user message with no reply and a token count that has
        not caught up. That half-state is exactly what a flaky test would be
        written against, and it would be the mock's fault rather than the
        client's.

        This is where the mock's "no agent behaviour" line is drawn and made
        explicit: the reply is `reply`, a string handed in by the caller (from
        the environment, via `server.Config`), not anything derived from the
        message. No model is called, no tool runs, nothing is timed. Two sends
        of the same text produce two identical turns, which is what makes a
        transcript assertion possible at all.

        `force_approval` is the approval-path hook: it parks the session in
        `attention` with a `pending_approval_details` cycle, so a test does not
        need a real gate thread (which this server will never have) to exercise
        the approve/deny UI. The flag is per send, so a test can drive the same
        session from a normal reply to a gated one.

        Returns the two stored rows plus the counters after the write, so a
        handler can answer without re-reading the record.
        """
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        body = message.strip()
        if not isinstance(reply, str):
            raise ValueError("reply must be a string")

        user_row = normalise_message({"role": "user", "content": body}, index=0)
        assistant_row = normalise_message(
            {"role": "assistant", "content": reply}, index=1,
            # A hair after the user turn, so a transcript read back sorts the way
            # a conversation reads even though the two rows were built together.
            default_timestamp=float(user_row["timestamp"]) + 0.001,
        )

        with self.lock:
            record = self.workstreams.get(ws_id)
            if record is None:
                raise KeyError("no such workstream: %s" % ws_id)
            record = copy.deepcopy(record)
            rows = list(record.get("messages") or [])
            rows.append(user_row)
            rows.append(assistant_row)
            record["messages"] = rows
            if not record.get("title"):
                record["title"] = _derived_title(user_row)

            # Counters: both sides of the turn, so a session that has had one
            # exchange reports a size that grows with the conversation rather
            # than staying at the create's zero.
            record["tokens"] = int(record.get("tokens") or 0) + (
                estimate_tokens(body) + estimate_tokens(reply)
            )
            # `tool_calls` is deliberately untouched: no tool is executed by this
            # server, and a send that incremented it would be the mock claiming
            # agent work it did not do.

            if force_approval:
                # attention, not idle: `sync_from_server` maps a pending approval
                # to attention regardless of the raw state, so answering idle
                # with a pending cycle would hand the addon a contradiction it
                # silently corrects instead of a state a test can assert on.
                record["state"] = "attention"
                record["pending_approval_details"] = copy.deepcopy(
                    list(DEFAULT_APPROVAL_DETAILS)
                )
                record["activity"] = "Waiting for approval"
                record["activity_state"] = "attention"
            else:
                # The turn is complete the moment it is written (there is
                # nothing to wait for), so the session is idle again rather than
                # stuck in `thinking`, which would make every chat poll show a
                # spinner for work that already finished.
                record["state"] = "idle"

            record["updated_at"] = time.time()
            self.workstreams[ws_id] = record
            return {
                "user": copy.deepcopy(user_row),
                "assistant": copy.deepcopy(assistant_row),
                "message_count": len(rows),
                "tokens": record["tokens"],
                "tool_calls": record["tool_calls"],
                "state": record["state"],
                "pending_approval_details": copy.deepcopy(
                    record["pending_approval_details"]
                ),
            }

    def force_approval(self, ws_id, details=None):
        """Park a session in `attention` with a pending approval cycle.

        The in-process twin of `POST /v1/api/admin/mock/seed`'s
        `pending_approval_details`: a test that can import this module should not
        have to drive an HTTP request to arrange an approval. Also the way to
        put an approval on a session *after* it exists, which seeding cannot do.

        `details=None` uses `DEFAULT_APPROVAL_DETAILS` (one cycle, one tool
        call). Returns the stored record.
        """
        with self.lock:
            record = self.workstreams.get(ws_id)
            if record is None:
                raise KeyError("no such workstream: %s" % ws_id)
            record = copy.deepcopy(record)
            record["pending_approval_details"] = copy.deepcopy(
                list(DEFAULT_APPROVAL_DETAILS if details is None else details)
            )
            record["state"] = "attention"
            record["updated_at"] = time.time()
            self.workstreams[ws_id] = record
            return copy.deepcopy(record)

    def clear_approval(self, ws_id):
        """Drop any pending approval and return the session to `idle`.

        The other half of `force_approval`: a test that has resolved an approval
        needs the state to go back, and no endpoint here does that yet
        (`POST .../approve` is still an unclaimed stub).
        """
        with self.lock:
            record = self.workstreams.get(ws_id)
            if record is None:
                raise KeyError("no such workstream: %s" % ws_id)
            record = copy.deepcopy(record)
            record["pending_approval_details"] = []
            if record["state"] == "attention":
                record["state"] = "idle"
            record["updated_at"] = time.time()
            self.workstreams[ws_id] = record
            return copy.deepcopy(record)

    # -- catalogue ---------------------------------------------------------

    def catalogue(self, name):
        """`skills`, `personas` or `models` — the list, as a copy."""
        with self.lock:
            return copy.deepcopy(getattr(self, name))

    def replace_catalogue(self, name, rows):
        """Swap a catalogue list (used by the admin path in tests)."""
        if name not in ("skills", "personas", "models"):
            raise ValueError("unknown catalogue: %r" % (name,))
        if not isinstance(rows, list):
            raise ValueError("%s must be a list" % name)
        with self.lock:
            setattr(self, name, copy.deepcopy(rows))
            return len(rows)

    # -- request log -------------------------------------------------------

    def log_request(self, entry):
        """Record one request. The log is the harness's evidence trail, so it
        is capped rather than unbounded: a polling chat would otherwise grow it
        without limit during a long test run.
        """
        with self.lock:
            self.request_seq += 1
            row = dict(entry, seq=self.request_seq, at=time.time())
            self.requests.append(row)
            if len(self.requests) > REQUEST_LOG_LIMIT:
                del self.requests[: len(self.requests) - REQUEST_LOG_LIMIT]
            return row

    def request_count(self):
        """Every request since reset, including ones dropped from the log."""
        with self.lock:
            return self.request_seq

    def request_log(self, limit=100, path_prefix=None, method=None):
        """The most recent requests, newest last (chronological reading order).

        Filters are for the common debugging questions: "what sends arrived"
        (`path_prefix=/v1/api/route`, `method=POST`) and "did anything call
        detail at all".
        """
        with self.lock:
            rows = self.requests
            if path_prefix:
                rows = [r for r in rows if r.get("path", "").startswith(path_prefix)]
            if method:
                rows = [r for r in rows if r.get("method") == method.upper()]
            return copy.deepcopy(rows[-limit:] if limit else rows)

    # -- introspection -----------------------------------------------------

    def snapshot(self):
        """Everything, JSON-safe. Served by `GET /admin/state`."""
        with self.lock:
            return {
                "node_id": NODE_ID,
                "started_at": self.started_at,
                "uptime_s": round(time.time() - self.started_at, 3),
                "workstreams": self.all(),
                "skills": copy.deepcopy(self.skills),
                "personas": copy.deepcopy(self.personas),
                "models": copy.deepcopy(self.models),
                "attachments": copy.deepcopy(self.attachments),
                "request_count": self.request_seq,
            }


# The one instance the server uses. A module-level singleton rather than an
# object passed around because the admin path, the handlers and any in-process
# test helper all have to mean the same state.
STORE = MockState()


def reset():
    """Convenience wrapper so `state.reset()` reads well from a test."""
    return STORE.reset()
