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
    messages             list of message dicts — transcript, oldest first
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

OTI-5736 owns the exact shapes it needs and may extend this; the point of
writing it down here is that nothing in the transcript is a UI concern — the
mock stores what the real API returns and the Odoo client does the interpreting.

Nothing in this module implements agent behaviour, and nothing in it should:
no model call, no tool execution, no timing simulation. A canned reply is a
string read from the environment (OTI-5738); that is the whole of it.
"""

import copy
import itertools
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
                "messages": copy.deepcopy(list(messages or [])),
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
