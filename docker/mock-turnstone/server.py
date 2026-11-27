#!/usr/bin/env python3
"""A dependency-free mock of the Turnstone HTTP API.

The addon has no automated tests and cannot be exercised without a Turnstone
server, so this process stands in for one. It speaks the exact paths
`addons/turnstone_integration/models/turnstone_client.py` calls, holds its state
in memory (`state.py`), and performs **no agent work at all**: no model call, no
tool execution, no timing simulation. A canned reply is a string read from the
environment (OTI-5738); that is the whole of it.

python3 docker/mock-turnstone/server.py            # starts on 0.0.0.0:8099, token "mock-token"
python3 docker/mock-turnstone/server.py --help     # this text; --version prints the mock's version

There are no other flags: everything that shapes behaviour is an environment
variable, because a compose service and a container healthcheck can only pass
env. An unrecognised argument exits 2 with a message instead of being ignored —
a harness that silently starts a server when asked a question is a harness that
wastes an hour.

Configuration, all read from the environment at startup:

    MOCK_TURNSTONE_HOST        bind address, default 0.0.0.0 (containers need it)
    MOCK_TURNSTONE_PORT        bind port, default 8099 (away from Odoo's 8069)
    MOCK_TURNSTONE_TOKEN       the bearer token every request must present,
                               default "mock-token"
    MOCK_TURNSTONE_HEALTH_PUBLIC  "1" makes GET /health need no token, for a
                               container healthcheck. Default: health needs the
                               token, like everything else.
    MOCK_TURNSTONE_PERMISSIONS comma-separated permissions reported by
                               /v1/api/auth/whoami, so a test can pretend the
                               token is less powerful than it is
    MOCK_TURNSTONE_LOG_LEVEL   logging level, default INFO. Every request is
                               logged, because "what did Odoo actually send" is
                               the first question about any test failure.
    MOCK_TURNSTONE_REPLY       the assistant text every prompt gets, verbatim.
                               This server runs no model, so a reply a test can
                               predict is the only honest one. Default in
                               `state.DEFAULT_REPLY`; setting it is how a test
                               proves the reply is configuration and not a
                               constant buried in a handler.

Requests are matched against a declared route table before anything else, so an
unknown path answers 404 even without a token; a known path without the right
token answers 401. That ordering is deliberate and is documented in README.md —
the route table is published there anyway, so answering 404 first leaks nothing
and makes both behaviours testable in isolation.

Every path in the table is either implemented here or marked with the ticket
that owns it (and answers 501 until then). A path nobody has claimed is
called out in README.md rather than silently missing, so a later test that needs
it knows to ask instead of debugging a mysterious 404.
"""

import copy
import hmac
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:  # `python server.py` (script) and `import` from a package both work.
    from . import state
except ImportError:  # pragma: no cover - the script path is the normal one
    import state

_logger = logging.getLogger("mock-turnstone")

# The mock's own version, reported by /health and distinguishable from a real
# Turnstone so nobody wonders which server answered.
MOCK_VERSION = "mock-turnstone/0.1"

# Body ceiling. The real client never sends more than an attachment; a runaway
# upload should fail loudly here rather than fill memory.
MAX_BODY_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class Config:
    """Runtime settings, read from the environment at construction.

    Constructed per server (see `make_server`) rather than once at import, so a
    test that imports this module can start two servers with different tokens or
    bind addresses in one process. `main()` builds one from the real environment;
    an in-process caller passes its own.
    """

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.host = env.get("MOCK_TURNSTONE_HOST", "0.0.0.0")
        self.port = int(env.get("MOCK_TURNSTONE_PORT", "8099"))
        self.token = env.get("MOCK_TURNSTONE_TOKEN", "mock-token")
        self.health_public = env.get("MOCK_TURNSTONE_HEALTH_PUBLIC", "0") in (
            "1", "true", "True", "yes",
        )
        self.log_level = env.get("MOCK_TURNSTONE_LOG_LEVEL", "INFO").upper()
        # The canned reply every prompt gets (OTI-5738). Kept on Config rather
        # than read at the handler because it is a *server* setting: two
        # in-process servers in one test can answer differently, which is how a
        # test proves the reply really comes from configuration and is not a
        # constant buried in a handler. An empty value is refused rather than
        # accepted: an empty reply would append an assistant turn with no text,
        # which the addon's renderer drops, and the transcript would then look
        # like a send that half-worked.
        self.reply = env.get("MOCK_TURNSTONE_REPLY", state.DEFAULT_REPLY)
        if not self.reply.strip():
            raise ValueError(
                "MOCK_TURNSTONE_REPLY must not be blank: it is the assistant turn every "
                "prompt gets, and an empty one renders as a send that produced nothing"
            )
        permissions = env.get(
            "MOCK_TURNSTONE_PERMISSIONS",
            "admin.console,admin.coordinator,admin.workstreams,admin.schedules,"
            "admin.skills,admin.personas,chat",
        )
        self.permissions = [p.strip() for p in permissions.split(",") if p.strip()]

    @classmethod
    def from_env(cls):
        """Settings from `os.environ`. Named so intent is obvious at a call site."""
        return cls()

    def describe(self):
        """A safe startup line: never prints the token itself."""
        return (
            "mock-turnstone listening on http://%s:%d (token from MOCK_TURNSTONE_TOKEN, "
            "%d permissions, health %s, reply %d chars)"
            % (
                self.host,
                self.port,
                len(self.permissions),
                "public" if self.health_public else "token-protected",
                len(self.reply),
            )
        )


# ---------------------------------------------------------------------------
# errors and responses
# ---------------------------------------------------------------------------


class HttpError(Exception):
    """A failure that becomes a JSON error response.

    Everything the mock returns on failure is shaped `{"error": "..."}` because
    that is what the Odoo client looks for when a body arrives at HTTP 200 and
    what its log lines quote on a real failure. A body without that key would
    make the mock's errors less useful than the real server's.
    """

    def __init__(self, status, message, headers=None, **extra):
        super().__init__(message)
        self.status = status
        self.payload = dict(extra, error=message)
        self.headers = headers or {}


def not_found(message="not found", **extra):
    return HttpError(404, message, **extra)


def bad_request(message, **extra):
    return HttpError(400, message, **extra)


def conflict(message, **extra):
    return HttpError(409, message, **extra)


def reserved(owner, what, **extra):
    """A 501 stub for a path another ticket owns.

    501, not 404: the path is real and listed, it just has no behaviour yet.
    Keeping the distinction means a test that hits one is told which ticket
    fills it instead of concluding its URL is wrong.
    """
    owner = owner or "unclaimed"

    def handler(ctx):
        raise HttpError(
            501,
            "not implemented in the mock yet: %s" % what,
            owner=owner,
            detail="Route is declared so the path shape is fixed; %s implements it." % owner,
            **extra
        )

    # The route table reads these to report which paths are stubs and who fills
    # them. Set here rather than repeated at every call site, because a call site
    # that forgot would report a stub as working and lose its owner.
    handler.is_stub = True
    handler.owner = owner
    return handler


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


class Route:
    """One path/method pair.

    `template` uses `{name}` for a path segment. `surface` is None for a
    surface-independent path, or "console"/"route" for one of the two per-session
    surfaces Turnstone really has; `session` says whether the dispatcher should
    resolve `{ws_id}` against the store first (and reject the wrong surface).
    `implemented` is derived from the handler unless given: a `reserved()` stub
    carries `is_stub`, so `GET /admin/mock/routes` can say which paths actually
    do something without anyone maintaining a second list.

    `public` may be a bool or a predicate taking the running Config, so a path
    whose auth depends on configuration (`/health`) is evaluated per server
    rather than frozen at import.
    """

    _segment = re.compile(r"\{([a-z_]+)\}")

    def __init__(self, method, template, handler, *, surface=None, session=False,
                 public=False, note="", owner="", implemented=None):
        self.method = method
        self.template = template
        self.handler = handler
        self.surface = surface
        self.session = session
        self.public = public
        self.note = note
        self.owner = owner or getattr(handler, "owner", "") or ""
        self.implemented = (
            not getattr(handler, "is_stub", False) if implemented is None else implemented
        )
        self.regex = re.compile("^" + self._segment.sub(r"(?P<\1>[^/]+)", template) + "$")

    def match(self, path):
        return self.regex.match(path)

    def is_public(self, config):
        """Whether this path skips the token check on this server."""
        return self.public(config) if callable(self.public) else bool(self.public)


def both_surfaces(method, template, handler, *, session=True, note="", owner=""):
    """Declare one per-session path on both the console and the route proxy.

    Turnstone splits per-session calls by kind: a coordinator lives on the
    console at `/v1/api/workstreams/{id}/...` and an interactive session behind
    the routing proxy at `/v1/api/route/workstreams/{id}/...`. Using the wrong
    one does not degrade gracefully, so both are declared here and the
    dispatcher enforces the pairing (`resolve_session`), which is the one place
    the split is decided.
    """
    console_path = template
    route_path = template.replace("/v1/api/", "/v1/api/route/", 1)
    return [
        Route(method, console_path, handler, surface="console", session=session,
              note=note, owner=owner),
        Route(method, route_path, handler, surface="route", session=session,
              note=note, owner=owner),
    ]


# ---------------------------------------------------------------------------
# request context
# ---------------------------------------------------------------------------


class Context:
    """Everything a handler needs, and the one place session resolution lives."""

    def __init__(self, handler, method, path, query, raw_body, route, config):
        self.handler = handler
        self.config = config
        self.method = method
        self.path = path
        self.query = query
        self.raw_body = raw_body
        self.route = route
        self.store = state.STORE
        self._json = None
        self._json_error = None
        self._session = None
        self._session_resolved = False

    # -- parsed input ------------------------------------------------------

    def json_body(self):
        """The decoded JSON body, or {} when there is none.

        Parsed lazily: a 501 stub or a GET should not fail because a caller sent
        a body the handler never reads. A malformed body raises here, which is
        the first moment a handler actually asked for it.
        """
        if self._json is None and self._json_error is None:
            if not self.raw_body:
                self._json = {}
            else:
                try:
                    self._json = json.loads(self.raw_body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    self._json_error = str(exc)
                    raise bad_request("invalid JSON body: %s" % exc) from exc
        if self._json_error:
            raise bad_request("invalid JSON body: %s" % self._json_error)
        return self._json

    def arg(self, name, default=None):
        """A query-string value, first occurrence."""
        values = self.query.get(name)
        return values[0] if values else default

    def int_arg(self, name, default):
        raw = self.arg(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise bad_request("query parameter %r must be an integer" % name) from exc

    # -- session resolution ------------------------------------------------

    def resolve_session(self):
        """Resolve `{ws_id}` against the store and enforce the surface split.

        Three failures are possible and each has its own answer, because the
        real server distinguishes them and the module's error text depends on
        the difference:

        * unknown id                  -> 404 {"error": "workstream not found"}
        * console path, interactive   -> 404 {"error": "coordinator not found"}
          (the exact body Turnstone's console returns; the README quotes it)
        * route path, coordinator     -> 404 (the routing proxy hosts only
          interactive sessions; a bare 404, as the proxy gives)

        Memoised for the life of one request. The dispatcher resolves every
        `session=True` route before the handler runs, and a handler that needs
        the record (session detail) asks again; without the memo that is two
        store round trips and, worse, two answers that could disagree if the
        record changed in between. One request, one resolution.
        """
        if self._session_resolved:
            if isinstance(self._session, HttpError):
                raise self._session
            return self._session
        try:
            self._session = self._lookup_session()
        except HttpError as exc:
            self._session = exc
            raise
        finally:
            self._session_resolved = True
        return self._session

    def _lookup_session(self):
        """The store lookup and split check behind `resolve_session`."""
        ws_id = self.ws_id()
        record = self.store.get(ws_id)
        if record is None:
            raise not_found("workstream not found", ws_id=ws_id)
        if self.route.surface == "console" and record["kind"] != "coordinator":
            raise not_found("coordinator not found", ws_id=ws_id)
        if self.route.surface == "route" and record["kind"] != "interactive":
            raise not_found("workstream not found", ws_id=ws_id,
                            detail="the routing proxy hosts interactive sessions only")
        return record

    def ws_id(self):
        match = self.route.match(self.path)
        return match.group("ws_id") if match else None


# ---------------------------------------------------------------------------
# handlers: console health and identity
# ---------------------------------------------------------------------------


def handle_health(ctx):
    """GET /health — outside /v1/api, exactly as the client calls it.

    Reports liveness plus the version list `turnstone_connection.action_test_
    connection` prints, and a `mock` marker so nobody mistakes which server
    answered.
    """
    return 200, {
        "status": "ok",
        "versions": [MOCK_VERSION],
        "mock": True,
        "node_id": state.NODE_ID,
        "uptime_s": round(time.time() - state.STORE.started_at, 3),
        "workstreams": len(state.STORE.all()),
    }


def handle_whoami(ctx):
    """GET /v1/api/auth/whoami — what the token is and what it may do.

    `permissions` is a comma-joined string because that is what
    `action_test_connection` splits on; `username`/`user_id` are both present
    because it prints whichever exists.
    """
    return 200, {
        "username": "mock-user",
        "user_id": "mock-user",
        "auth_type": "api_token",
        "scopes": list(ctx.config.permissions),
        "permissions": ",".join(ctx.config.permissions),
        "node_id": state.NODE_ID,
    }


# ---------------------------------------------------------------------------
# handlers: catalogue
# ---------------------------------------------------------------------------


def handle_skills(ctx):
    """GET /v1/api/admin/skills — the ADMIN endpoint, because the public
    summary omits `kind` and kind is what decides whether a session can fan out.

    A bare JSON list, deliberately: `TurnstoneClient.skills()` returns the body
    as-is when it is a list and only falls back to `data.get("skills")`
    otherwise, so wrapping this in an envelope would make the mock's catalogue
    silently empty in the addon.
    """
    return 200, state.STORE.catalogue("skills")


def handle_personas(ctx):
    """GET /v1/api/admin/personas — also a bare list, same reason as skills."""
    return 200, state.STORE.catalogue("personas")


def handle_models(ctx):
    """GET /v1/api/models. Inert in the module today (the catalogue controller
    harvests aliases from the cluster feed instead) but declared because the
    client has a method for it, and a mock that answers 404 for a call the real
    server supports is a trap."""
    return 200, {"models": state.STORE.catalogue("models")}


# ---------------------------------------------------------------------------
# handlers: cluster-wide visibility
# ---------------------------------------------------------------------------


def handle_cluster_overview(ctx):
    """GET /v1/api/cluster/overview — the summary strip on Live Activity.

    `turnstone_live.xml` reads `workstreams`, `states.{thinking,attention,error}`,
    `nodes` and `aggregate.total_tool_calls`, so those keys exist by name here
    rather than being left for later. Counts are computed over every workstream
    the mock holds, including ones seeded directly into state.
    """
    records = state.STORE.all()
    counts = {"idle": 0, "thinking": 0, "attention": 0, "error": 0, "closed": 0}
    for record in records:
        key = record.get("state") if record.get("state") in counts else "idle"
        counts[key] += 1
    tokens = sum(r.get("tokens") or 0 for r in records)
    tool_calls = sum(r.get("tool_calls") or 0 for r in records)
    return 200, {
        "workstreams": len(records),
        "nodes": 1,
        "states": counts,
        "aggregate": {
            "tokens": tokens,
            "tool_calls": tool_calls,
            "total_tool_calls": tool_calls,
            "total_tokens": tokens,
        },
        "node_id": state.NODE_ID,
        "mock": True,
    }


# ---------------------------------------------------------------------------
# handlers: workstream creation
#
# Creation obeys the same console/route split as everything else, and the split
# is expressed by having TWO routes with two handlers rather than one handler
# that branches on `ctx.route.surface`. There is no shared "create" that could
# be pointed at the wrong surface: the console handler can only make a
# coordinator and the proxy handler can only make an interactive session, which
# is exactly what the real API can do. Rejecting a coordinator through the
# proxy is therefore schema validation (kind is not a value that surface
# accepts), not the per-session prefix check in `resolve_session`.
# ---------------------------------------------------------------------------


def _optional_str(body, name):
    """A string field, or None. An integer or list is a caller mistake, not a value."""
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise bad_request("%s must be a string" % name)
    value = value.strip()
    return value or None


def _creation_meta(body, allowed):
    """The request fields this mock records but does not act on.

    Turnstone's create endpoints take settings the mock has no behaviour for
    (`client_type`, `judge_model`, the auto-approve lists). Dropping them would
    make "what did Odoo ask for" unanswerable, and inventing behaviour for them
    would be worse, so they are kept on the record under `meta` where a test can
    assert them and nothing else reads them.
    """
    meta = {}
    for name in allowed:
        if name in body and body[name] is not None:
            meta[name] = body[name]
    return meta


def handle_create_workstream_console(ctx):
    """POST /v1/api/workstreams/new — the console's create, coordinators only.

    The console endpoint takes `name`, `skill`, `persona` and `initial_message`
    and nothing else. It has no `kind` and no `model` in its schema: it always
    makes a coordinator, which runs on the node's default alias, and its
    children are the only sessions a model may be chosen for.

    That is a trap worth reproducing rather than guarding against — a caller
    that asks the console for an interactive session gets a coordinator on the
    real server, silently — so a `kind` in the body is ignored and the response
    says `coordinator`. A test that wants the other behaviour must use the
    proxy, and the response is where it finds out it did not.

    `initial_message` becomes the first user turn of the stored transcript, and
    this server answers it with the same canned reply a `send` does — the addon's
    first message goes through this endpoint, not through send, so a create with
    no reply would leave every session's opening prompt unanswered while later
    ones were answered. The state stays `idle`: there is nothing to wait for.
    """
    body = ctx.json_body()
    if not isinstance(body, dict):
        raise bad_request("create body must be a JSON object")
    initial = _optional_str(body, "initial_message")
    record = state.STORE.create_workstream(
        kind="coordinator",
        name=_optional_str(body, "name"),
        skill=_optional_str(body, "skill"),
        persona=_optional_str(body, "persona"),
        # No model_alias: this endpoint has no `model` field to set one with, so
        # the store's DEFAULT_MODEL_ALIAS applies, which is the node default a
        # real coordinator runs on. A `model` sent here is not in the schema and
        # is not recorded either — the console would silently ignore it.
        messages=_initial_turns(initial, ctx.config.reply),
        tokens=_initial_tokens(initial, ctx.config.reply),
        meta=_creation_meta(body, ("kind",)),
    )
    return 201, _creation_response(record, surface="console")


def handle_create_workstream_route(ctx):
    """POST /v1/api/route/workstreams/new — the proxy's create, interactive only.

    The routing proxy is the only surface that accepts `kind`, `model`,
    `judge_model` and the auto-approve fields, because it is the only surface
    that creates a session on a node. `kind` defaults to `interactive` (a
    proxied session is one by definition); any other value is refused, and
    `coordinator` gets the pointer to the console rather than a generic 400 —
    that is the one mistake this endpoint exists to catch.
    """
    body = ctx.json_body()
    if not isinstance(body, dict):
        raise bad_request("create body must be a JSON object")
    kind = body.get("kind")
    if kind is None:
        kind = "interactive"
    if kind == "coordinator":
        raise bad_request(
            "kind 'coordinator' cannot be created through the routing proxy",
            detail="coordinator workstreams must be created on the console via "
                   "POST /v1/api/workstreams/new",
        )
    if kind != "interactive":
        raise bad_request("kind must be 'interactive' through the routing proxy", kind=kind)

    initial = _optional_str(body, "initial_message")
    record = state.STORE.create_workstream(
        kind="interactive",
        name=_optional_str(body, "name"),
        skill=_optional_str(body, "skill"),
        persona=_optional_str(body, "persona"),
        model_alias=_optional_str(body, "model"),
        messages=_initial_turns(initial, ctx.config.reply),
        tokens=_initial_tokens(initial, ctx.config.reply),
        meta=_creation_meta(body, ("client_type", "judge_model", "auto_approve",
                                   "auto_approve_tools")),
    )
    return 201, _creation_response(record, surface="route")


def _initial_turns(initial_message, reply):
    """The first exchange, if the caller supplied a prompt.

    A create with `initial_message` is the addon's *first* send: `action_send`
    calls `_ensure_started(first_message=body)`, which uses `POST
    .../workstreams/new` and never touches the send route. So this path has to
    produce the canned assistant turn too, or the first message of every session
    would sit in the mirror as a prompt with nothing answering it — while every
    later message (which does go through send) got a reply. OTI-5736 recorded
    that handoff: "the assistant side is OTI-5738's canned reply".

    Same source as `handle_send`: `MOCK_TURNSTONE_REPLY`, read once at startup
    into `Config`. There is no model call on either path, which is why the two
    produce byte-identical replies for the same setting.

    No `force_approval` here: creating a session is not a turn, and a harness that
    gated the very first message implicitly would make it hard to test the
    ungated path at all. A test that wants the first turn gated seeds the session
    with `force_approval` and sends a second message.
    """
    if not initial_message:
        return []
    return [
        {"role": "user", "content": initial_message},
        {"role": "assistant", "content": reply},
    ]


def _initial_tokens(initial_message, reply):
    """Token count for a create's first exchange. 0 when there is no prompt.

    The counters have to agree with the transcript however it was filled: a
    created session holding two rows and reporting `tokens: 0` would say the
    conversation was free, and an assertion about a session's size would have to
    know which entry point made it.
    """
    if not initial_message:
        return 0
    return state.estimate_tokens(initial_message) + state.estimate_tokens(reply)


def _creation_response(record, surface):
    """What a create endpoint answers.

    `ws_id` and `node_id` are at the top level because that is exactly what
    `turnstone_session._ensure_started` reads, and it writes them straight onto
    the Odoo row. A body without `ws_id` is failure mode 1 in the persistence
    investigation — the user's first message silently goes nowhere — so this
    endpoint always answers with one.

    The whole record is included under `workstream` as well, so a test can read
    the created state without a second request, and `surface` says which
    endpoint made it. No `error` key on success: a 200 with an `error` key is
    parsed by the addon as an empty session.
    """
    return {
        "ws_id": record["id"],
        "node_id": record["node_id"],
        "kind": record["kind"],
        "state": record["state"],
        "name": record["name"],
        "title": record["title"],
        "skill": record["skill"],
        "persona": record["persona"],
        "model_alias": record["model_alias"],
        "parent_ws_id": record["parent_ws_id"],
        "created_at": record["created_at"],
        "surface": surface,
        "workstream": record,
    }


# ---------------------------------------------------------------------------
# handlers: cluster-wide reads
# ---------------------------------------------------------------------------


def _feed_row(record):
    """One row of the cluster feed.

    The keys are the ones `turnstone_session._children` and
    `controllers/main.py::cluster_activity` read by name: `id` (not `ws_id`),
    `title`/`name`, `kind`, `parent_ws_id`, counters, and a model alias under
    both spellings the controller accepts. `node` and `node_id` carry the same
    value for the same reason — the controller reads `node`, the record stores
    `node_id`.

    A row is a summary, not the whole record: `messages` is deliberately absent
    (a 200-row feed that each carried a transcript would be enormous), and the
    transcript is what `/cluster/ws/{id}/detail` is for.
    """
    return {
        "id": record["id"],
        "ws_id": record["id"],
        "kind": record["kind"],
        "state": record["state"],
        "name": record["name"],
        "title": record["title"],
        "skill": record["skill"],
        "persona": record["persona"],
        "model_alias": record["model_alias"],
        "model": record["model_alias"],
        "parent_ws_id": record["parent_ws_id"],
        "node": record["node_id"],
        "node_id": record["node_id"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "tokens": record["tokens"],
        "tool_calls": record["tool_calls"],
        "context_ratio": record["context_ratio"],
        "activity": record["activity"],
        "activity_state": record["activity_state"],
        "closed": record["closed"],
    }


def handle_cluster_workstreams(ctx):
    """GET /v1/api/cluster/workstreams — every workstream, whoever started it.

    This is also how the addon finds child workstreams, because
    `/workstreams/{id}/children` is coordinator-only and every row here carries
    `parent_ws_id`. Filtering and paging are applied to the store's list before
    the page is cut, so `total` counts matches and not the page size.
    """
    page = ctx.int_arg("page", 1)
    per_page = ctx.int_arg("per_page", 100)
    if page < 1:
        raise bad_request("page must be 1 or greater")
    if per_page < 1:
        raise bad_request("per_page must be 1 or greater")
    result = ctx.store.list_workstreams(
        state=ctx.arg("state") or None,
        search=ctx.arg("search") or None,
        page=page,
        per_page=per_page,
    )
    return 200, {
        "workstreams": [_feed_row(row) for row in result["rows"]],
        "total": result["total"],
        "page": result["page"],
        "per_page": result["per_page"],
        "node_id": state.NODE_ID,
    }


def handle_session_detail(ctx):
    """GET /v1/api/cluster/ws/{ws_id}/detail — one session, from any node.

    The one surface that answers for either kind, and the only one the addon
    reads a session's authoritative state from (`sync_from_server`). Three
    blocks, because the client treats them as three:

    * `persisted` — the row as stored: `ws_id` (adopt() refuses a session without
      it), kind, parent, persona, node, state, title/name, model, timestamps.
    * `live` — the counters and the moment-to-moment fields: state, tokens,
      tool_calls, context_ratio, activity, activity_state, title, and
      `pending_approval_details` (a non-empty list is what the addon maps to
      `attention`).
    * `messages` — the transcript, as `_history_payload` shapes it: a list,
      present and empty rather than missing when there are no turns
      (`detail.get("messages") or []` cannot tell "absent" from "empty", and a
      missing key is how a real sync wipes a good mirror). Each row carries
      `role`, `content`, `timestamp` and, when present, `tool_calls` /
      `tool_call_id` — see `state.normalise_message`, which is the one place
      that shape is decided.

    `message_limit` is the real endpoint's parameter for a bounded tail. It is
    applied only when the caller sends it; the default is the whole transcript,
    because the real default of 20 would silently truncate the very fixtures this
    mock exists to serve. OTI-5740 owns reproducing the bounded-tail default as a
    failure mode.
    """
    record = ctx.resolve_session()
    limit = ctx.int_arg("message_limit", None)
    if limit is not None and limit < 1:
        raise bad_request("message_limit must be 1 or greater", message_limit=limit)
    history = _history_payload(record, limit=min(limit, 200) if limit else None)
    return 200, {
        "persisted": {
            "ws_id": record["id"],
            "id": record["id"],
            "kind": record["kind"],
            "state": record["state"],
            "name": record["name"],
            "title": record["title"],
            "skill": record["skill"],
            "persona": record["persona"],
            "model_alias": record["model_alias"],
            "model": record["model_alias"],
            "parent_ws_id": record["parent_ws_id"],
            "node_id": record["node_id"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "closed": record["closed"],
        },
        "live": {
            "ws_id": record["id"],
            "kind": record["kind"],
            "state": record["state"],
            "title": record["title"],
            "model_alias": record["model_alias"],
            "tokens": record["tokens"],
            "tool_calls": record["tool_calls"],
            "context_ratio": record["context_ratio"],
            "activity": record["activity"],
            "activity_state": record["activity_state"],
            "pending_approval_details": record["pending_approval_details"],
            "node_id": record["node_id"],
        },
        "messages": history["messages"],
        "message_limit": limit,
    }


def _history_payload(record, limit=None, max_limit=500):
    """The body `GET .../history` answers, and the one detail's tail uses.

    `ws_id` and `messages` are the required shape (1.8.3 `WorkstreamHistoryResponse`
    requires `ws_id`, carries `messages`). `cursor` and `handoff_token` are part of
    that documented response but are meaningless here: they belong to the
    REST-to-SSE handoff, and the mock has no event stream, so they are `null`
    exactly as a real read with nothing in flight returns. Present-and-null rather
    than absent, so a test asserting on the shape sees the same keys it would get
    from a server.

    `limit` bounds the tail to the most recent N rows, as the real `limit` query
    parameter does. Only applied when the caller asked: the default is the whole
    transcript, because "seed 25 messages, read them back" is what the harness is
    for, and a silent 20-row tail here would look like a transcript bug. The real
    server's default of 20 is a failure mode in its own right and OTI-5740 owns
    reproducing it.
    """
    messages = record.get("messages") or []
    if limit is not None:
        messages = messages[-limit:] if limit > 0 else []
    return {
        "ws_id": record["id"],
        "messages": copy.deepcopy(messages),
        "cursor": None,
        "handoff_token": None,
    }


def handle_session_history(ctx):
    """GET .../workstreams/{ws_id}/history — the transcript, oldest first.

    The same rows `detail` carries under `messages`, for the sessions its own
    surface hosts: a coordinator on the console path, an interactive session on
    the route path. Both handlers read the same store, so the two surfaces cannot
    disagree about a conversation, and the surface pairing is still decided in
    `resolve_session` — this handler never re-checks a kind.

    `limit` is the real query parameter's name and meaning (max conversation rows
    from the tail, 500 max); see `_history_payload` for why the default here is
    the whole transcript rather than the real server's 100.

    Divergence from the real server worth knowing: 1.8.3's published spec declares
    history on the console path only, with no `/route/` twin, while this mock
    declares both (the route entry exists so an interactive session's history is
    readable at all — the console path refuses it, `resolve_session`). OTI-5763 is
    confirming the real surface list; if it turns out interactive history is only
    served through cluster detail, the route twin should be withdrawn rather than
    kept for symmetry.
    """
    record = ctx.resolve_session()
    limit = ctx.int_arg("limit", None)
    if limit is not None and limit < 1:
        raise bad_request("limit must be 1 or greater", limit=limit)
    return 200, _history_payload(record, limit=min(limit, 500) if limit else None)


def handle_session_rewind(ctx):
    """POST .../workstreams/{ws_id}/rewind — drop the last N turns.

    The real endpoint takes `{"turns": N}` (required, minimum 1) and answers a
    generic `{"status": "ok"}`. `turns` counts user turns — a prompt and the
    replies answering it — not rows, and it is clamped to what the session has
    (RewindRequest: "Clamped to the available turn count"), so dropping five
    turns from a two-turn conversation empties it instead of failing.

    This exists in the harness because a rewind is the one thing that makes a
    transcript SHORTER, and a mirror that only ever grows cannot be shown to
    follow the server. OTI-5744's guards are written against exactly this
    (a shorter list that is not a prefix of ours is a rewind, not a partial
    view), so a test needs to be able to cause it on demand.

    The status body also carries `dropped`/`remaining`, which the real server
    does not send. That is deliberate: an assertion can then say "three rows
    went and one turn stayed" without a second read, and the extra keys are
    additive on a response nothing parses strictly.
    """
    record = ctx.resolve_session()
    body = ctx.json_body()
    if not isinstance(body, dict):
        raise bad_request("rewind body must be a JSON object")
    turns = body.get("turns")
    if turns is None:
        raise bad_request("turns is required", detail="{\"turns\": N} drops the last N user turns")
    if isinstance(turns, bool) or not isinstance(turns, int):
        raise bad_request("turns must be an integer", turns=turns)
    if turns < 1:
        raise bad_request("turns must be 1 or greater", turns=turns)
    result = ctx.store.rewind(record["id"], turns)
    return 200, dict(result, status="ok", ws_id=record["id"])


# ---------------------------------------------------------------------------
# handlers: prompt submission (OTI-5738)
#
# One handler for both surfaces, because the console's `CoordinatorSendRequest`
# and the proxy's `SendRequest` are the same body in 1.8.3 (both: `message`
# required, optional `client_send_id` and `attachment_ids`), and the surface
# split is already enforced in `resolve_session`. Two handlers here would be two
# copies of the same validation with no rule to keep them in step.
# ---------------------------------------------------------------------------

# `client_send_id` is an opaque browser correlation token in 1.8.3's schema
# (`^[A-Za-z0-9_-]+$`, 1..128). Validated here because a caller that sends an
# object or a 500-character id is making a mistake the real server would answer
# 400 for, and a mock that accepted it would teach the wrong body.
_CLIENT_SEND_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def handle_send(ctx):
    """POST .../workstreams/{ws_id}/send — a prompt, and its canned reply.

    The whole of the mock's "conversation": append the user turn, append one
    assistant turn read from `MOCK_TURNSTONE_REPLY` (never from the message),
    move the token counters. No model call, no tool execution, no thinking time
    — and the session is back to `idle` in the same locked write, so a chat that
    polls detail afterwards sees a finished exchange rather than a spinner.

    Both turns land from one `state.send_turn()` call on purpose. Two separate
    appends would let a poll read a user message whose reply and token count had
    not arrived, which is a half-turn the real server never exposes; a test
    written against that would be testing the mock's scheduling.

    Approval-path tests: a session whose `meta.force_approval` is set (through
    `POST /v1/api/admin/mock/seed`) parks in `attention` with a pending approval
    cycle instead of returning to idle. That is a fixture knob on the *session*,
    not a field on this request, so the body stays exactly the shape 1.8.3
    declares — a mock-only field here would be a body the real server would
    reject, and a test could then pass against it.

    Response: 1.8.3's `SendResponse`/`CoordinatorSendResponse` (`status` is the
    required key), plus the appended rows and the post-send counters. The extras
    are additive on a body the client ignores entirely — `TurnstoneClient.send`
    returns it and `turnstone_session.action_send` never reads a field, it
    re-reads detail — and they are what lets an assertion say "two rows appeared"
    without a second request.

    `attachment_ids` is accepted and answered honestly as *dropped*: no upload
    endpoint exists in this mock yet (`POST .../attachments` is unclaimed), so no
    id a caller can name is one this server holds, and
    `dropped_attachment_ids` is exactly the real field for "requested, could not
    reserve". Pretending to attach them would be the harness lying about a turn.
    """
    record = ctx.resolve_session()
    body = ctx.json_body()
    if not isinstance(body, dict):
        raise bad_request("send body must be a JSON object")

    message = body.get("message")
    if not isinstance(message, str):
        raise bad_request("message is required and must be a string")
    if not message.strip():
        raise bad_request("message must not be empty")

    client_send_id = body.get("client_send_id")
    if client_send_id is not None:
        if not isinstance(client_send_id, str) or not _CLIENT_SEND_ID.match(client_send_id):
            raise bad_request(
                "client_send_id must be 1-128 characters of letters, digits, dash or underscore",
                client_send_id=client_send_id,
            )

    attachment_ids = body.get("attachment_ids")
    if attachment_ids is not None:
        if not isinstance(attachment_ids, list) or not all(
            isinstance(a, str) for a in attachment_ids
        ):
            raise bad_request("attachment_ids must be a list of strings")
    else:
        attachment_ids = []

    if record.get("closed") or record.get("state") == "closed":
        # 409 is the status the published spec declares for a send that cannot
        # be accepted; the text is the mock's, and says what to do about it.
        raise conflict(
            "workstream is closed",
            ws_id=record["id"],
            detail="a closed session cannot take a prompt; start a new one",
        )

    force_approval = bool((record.get("meta") or {}).get("force_approval"))
    try:
        result = ctx.store.send_turn(
            record["id"],
            message,
            reply=ctx.config.reply,
            force_approval=force_approval,
        )
    except KeyError as exc:  # deleted between resolve_session and the write
        raise not_found("workstream not found", ws_id=record["id"]) from exc

    return 200, {
        "status": "ok",
        # Present-and-false rather than absent, as the spec sends it: a caller
        # reading `resp["deferred"]` should not have to default it.
        "deferred": False,
        "ws_id": record["id"],
        # Echoed so a browser can match the turn it just sent to the turn that
        # appears in the transcript (this mock is synchronous, so it can always
        # answer with them rather than making the caller poll for its own text).
        "client_send_id": client_send_id,
        "attached_ids": [],
        "dropped_attachment_ids": list(attachment_ids),
        "priority": None,
        "msg_id": None,
        "turns": [result["user"], result["assistant"]],
        "message_count": result["message_count"],
        "tokens": result["tokens"],
        "tool_calls": result["tool_calls"],
        "state": result["state"],
        "pending_approval_details": result["pending_approval_details"],
    }


# ---------------------------------------------------------------------------
# handlers: mock control plane
#
# These are NOT Turnstone endpoints. They live under /v1/api/admin/mock/ so they
# share the token check and the response envelope, and so their names cannot
# collide with a path the module calls. A test in another container (an Odoo
# TransactionCase) cannot import state.py, so HTTP is how it resets and seeds.
# ---------------------------------------------------------------------------


def handle_mock_reset(ctx):
    """POST /v1/api/admin/mock/reset — back to the post-startup shape."""
    return 200, state.STORE.reset()


def handle_mock_state(ctx):
    """GET /v1/api/admin/mock/state — everything, for assertions in a test."""
    return 200, state.STORE.snapshot()


def handle_mock_seed(ctx):
    """POST /v1/api/admin/mock/seed — create a workstream directly in state.

    Not the same thing as `POST /workstreams/new`: that is Turnstone's own
    creation endpoint (OTI-5734) with its console/route split and its response
    contract. This is a fixture helper, so it can do things a real server
    cannot — place a workstream in any state, plant a transcript before anyone
    opens chat, or pin a chosen `ws_id` so a test can name it in an assertion.

    Body (all optional but kind):
        kind, name, title, state, skill, persona, model_alias, parent_ws_id,
        ws_id, tokens, tool_calls, context_ratio, activity, activity_state,
        messages, pending_approval_details, meta, append, force_approval

    `messages` are normalised by `state.normalise_message`, so a test can seed
    the loose shape (a bare `{\"name\": ..., \"arguments\": {...}}` tool call,
    content parts, a `text` key) and read back the one spelling the addon parses.
    The response is the stored record, so an assertion reads what will be served
    rather than what was sent.

    `force_approval: true` sets a flag on the session (under `meta`) so every
    later `POST .../send` parks it in `attention` with a pending approval cycle
    instead of returning to idle — the approval-path switch, OTI-5738. It lives
    on the *session* rather than on the send body deliberately: the send body has
    to keep the shape 1.8.3 declares, or a test would pass against a field the
    real server rejects. `pending_approval_details` seeds one approval now;
    `force_approval` is what makes the next prompt produce one.

    `append: true` plants `messages` onto an EXISTING workstream instead of
    creating one (then `kind` is ignored and a pinned `ws_id` is the target):
    "seed a transcript before opening chat" on a session that
    `POST /workstreams/new` already made. The in-process equivalent is
    `state.STORE.plant_transcript(ws_id, messages)`; this is the same helper for
    a test in another container, which cannot import state.py.
    """
    body = ctx.json_body()
    if not isinstance(body, dict):
        raise bad_request("seed body must be a JSON object")
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        raise bad_request("messages must be a list")
    force_approval = body.get("force_approval")
    if force_approval is not None and not isinstance(force_approval, bool):
        raise bad_request("force_approval must be true or false")

    if body.get("append"):
        target = body.get("ws_id")
        if not target:
            raise bad_request("append needs the ws_id to plant the transcript on")
        target = str(target)
        try:
            rows = state.STORE.plant_transcript(target, messages, replace=False)
            if force_approval is not None:
                updated = state.STORE.get(target)
                updated["meta"] = dict(updated.get("meta") or {},
                                       force_approval=force_approval)
                state.STORE.save(updated)
        except KeyError as exc:
            raise not_found("workstream not found", ws_id=target) from exc
        return 200, dict(state.STORE.get(target), messages=rows)

    kind = body.get("kind") or "interactive"
    if kind not in ("coordinator", "interactive"):
        raise bad_request("kind must be 'coordinator' or 'interactive'")

    explicit_id = body.get("ws_id")
    if explicit_id is not None:
        explicit_id = str(explicit_id)
        if not re.match(r"^[A-Za-z0-9._:-]+$", explicit_id):
            raise bad_request("ws_id may use letters, digits, dot, colon, dash, underscore only")
        if state.STORE.exists(explicit_id):
            raise conflict("workstream already exists", ws_id=explicit_id)

    record = state.STORE.create_workstream(
        ws_id=explicit_id,
        kind=kind,
        name=body.get("name"),
        title=body.get("title"),
        state=body.get("state"),
        skill=body.get("skill"),
        persona=body.get("persona"),
        model_alias=body.get("model_alias"),
        parent_ws_id=body.get("parent_ws_id"),
        messages=messages,
        meta=body.get("meta"),
        tokens=body.get("tokens") or 0,
        tool_calls=body.get("tool_calls") or 0,
        context_ratio=body.get("context_ratio") or 0.0,
        activity=body.get("activity"),
        activity_state=body.get("activity_state"),
    )
    pending = body.get("pending_approval_details")
    if pending:
        if not isinstance(pending, list):
            raise bad_request("pending_approval_details must be a list")
        record["pending_approval_details"] = pending
        # A pending approval is what the module maps to "attention"
        # (`sync_from_server`: pending wins over the raw state), so a seeded
        # approval with an idle state would be a contradiction the addon would
        # paper over. Only defaulted when the caller did not ask for a state —
        # a test that wants "attention but no approval" keeps its choice.
        if body.get("state") is None:
            record["state"] = "attention"
    if force_approval is not None:
        # On the session, not on the approval: this is what makes a *future* send
        # produce an approval (see `handle_send`). Recorded even when a pending
        # approval is being seeded now, because a test that resolves one and
        # sends again expects the next turn to gate too.
        record["meta"] = dict(record.get("meta") or {}, force_approval=force_approval)
    if pending or force_approval is not None:
        record = state.STORE.save(record)
    return 201, record


def handle_mock_requests(ctx):
    """GET /v1/api/admin/mock/requests — what arrived, in order.

    The single most useful debugging view in this harness: "one send became
    five", "the client never called detail", "the body was empty" are all
    answered by reading this rather than by adding logging to the addon.
    Never records the token.
    """
    limit = ctx.int_arg("limit", 100)
    entries = state.STORE.request_log(limit=limit, path_prefix=ctx.arg("path_prefix"),
                                     method=ctx.arg("method"))
    return 200, {
        "total": state.STORE.request_count(),
        "returned": len(entries),
        "requests": entries,
    }


def handle_mock_routes(ctx):
    """GET /v1/api/admin/mock/routes — the route table this process actually has.

    README.md lists the same paths, but a document drifts and a running server
    cannot. `implemented` says whether a request would reach real behaviour or
    the 501 stub, and `owner` names the ticket that fills the ones that would
    not, so "why is this 501" is answered without reading server.py.
    """
    return 200, {
        "count": len(ROUTES),
        "implemented": sum(1 for r in ROUTES if r.implemented),
        "routes": [
            {
                "method": r.method,
                "path": r.template,
                "surface": r.surface,
                "implemented": r.implemented,
                "owner": r.owner or None,
                "note": r.note or None,
            }
            for r in ROUTES
        ],
    }


# ---------------------------------------------------------------------------
# the route table
#
# Every path turnstone_client.py calls, in one place. An entry that raises 501
# via reserved() is a path whose shape is fixed here but whose behaviour belongs
# to a later ticket in story OTI-698; the README repeats this list for readers
# who will never open this file.
# ---------------------------------------------------------------------------

ROUTES = [
    # -- outside /v1/api, as the client calls it ---------------------------
    Route("GET", "/health", handle_health, public=lambda cfg: cfg.health_public,
          note="console health; the client calls it at the root, not under /v1/api"),

    # -- identity ----------------------------------------------------------
    Route("GET", "/v1/api/auth/whoami", handle_whoami,
          note="action_test_connection prints username and permission count"),

    # -- catalogue ---------------------------------------------------------
    Route("GET", "/v1/api/admin/skills", handle_skills,
          note="ADMIN endpoint: the public one omits kind"),
    Route("GET", "/v1/api/admin/personas", handle_personas),
    Route("GET", "/v1/api/models", handle_models,
          note="inert in the addon today, declared because the client has a method"),
    Route("GET", "/v1/api/admin/schedules",
          reserved("", "scheduled tasks list"),
          note="Schedules screen; nothing in story OTI-698 claims this yet"),
    Route("GET", "/v1/api/admin/schedules/{task_id}/runs",
          reserved("", "scheduled task run history"),
          note="Schedules screen; nothing in story OTI-698 claims this yet"),

    # -- workstream creation (console/route split) -------------------------
    # Two routes, two handlers: the console can only make a coordinator and the
    # proxy can only make an interactive session, exactly as the real API does.
    Route("POST", "/v1/api/workstreams/new", handle_create_workstream_console,
          surface="console",
          note="console only, and only ever coordinators: takes no kind and no model"),
    Route("POST", "/v1/api/route/workstreams/new", handle_create_workstream_route,
          surface="route",
          note="the only surface accepting kind; a coordinator here is refused"),

    # -- cluster-wide reads ------------------------------------------------
    Route("GET", "/v1/api/cluster/overview", handle_cluster_overview,
          note="summary strip on Live Activity"),
    Route("GET", "/v1/api/cluster/workstreams", handle_cluster_workstreams,
          note="the feed used for children and Live Activity: page/per_page/state/search"),
    Route("GET", "/v1/api/cluster/ws/{ws_id}/detail", handle_session_detail,
          session=True,
          note="the one surface returning persisted + live + messages for any node"),

    # -- transcript --------------------------------------------------------
    # Both surfaces, because a session of either kind has a transcript and the
    # only place the pairing is decided is `resolve_session` — duplicating it in
    # the handler is how a mock starts accepting an API Turnstone rejects.
    # (1.8.3's published spec lists the console path only; see
    # `handle_session_history` for why the twin is declared and what would
    # retire it.)
    *both_surfaces("GET", "/v1/api/workstreams/{ws_id}/history",
                   handle_session_history,
                   note="the transcript detail also carries, with the real `limit` query"),

    # -- per-session control, on both surfaces ------------------------------
    *both_surfaces("POST", "/v1/api/workstreams/{ws_id}/send", handle_send,
                   note="appends the user turn and one canned assistant reply, from "
                        "MOCK_TURNSTONE_REPLY; no model is called"),
    *both_surfaces("DELETE", "/v1/api/workstreams/{ws_id}/send",
                   reserved("", "cancel a queued message"), owner="",
                   note="client has the method; no ticket claims it yet"),
    *both_surfaces("POST", "/v1/api/workstreams/{ws_id}/approve",
                   reserved("", "approval resolution"), owner="",
                   note="needed by approval-path tests; no ticket claims it yet"),
    *both_surfaces("POST", "/v1/api/workstreams/{ws_id}/cancel",
                   reserved("", "cancel the running turn"), owner=""),
    *both_surfaces("POST", "/v1/api/workstreams/{ws_id}/retry",
                   reserved("", "retry the last turn"), owner=""),
    *both_surfaces("POST", "/v1/api/workstreams/{ws_id}/rewind",
                   handle_session_rewind,
                   note="turns=N drops the last N user turns; the only way a "
                        "transcript gets shorter, which OTI-5744's guards need"),
    *both_surfaces("POST", "/v1/api/workstreams/{ws_id}/close",
                   reserved("", "close the workstream"), owner="",
                   note="needed by reopen/close tests; no ticket claims it yet"),

    # -- coordinator-only extras -------------------------------------------
    Route("GET", "/v1/api/workstreams/{ws_id}/children",
          reserved("", "child workstreams"),
          surface="console", session=True,
          note="coordinator-only in Turnstone; _children() uses the cluster feed instead"),
    Route("GET", "/v1/api/workstreams/{ws_id}/attachments",
          reserved("", "attachment list"), surface="console", session=True),
    Route("POST", "/v1/api/workstreams/{ws_id}/attachments",
          reserved("", "multipart attachment upload"), surface="console", session=True,
          note="coordinator-only; turnstone_client upload_attachment builds the body"),
    Route("DELETE", "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}",
          reserved("", "attachment delete"), surface="console", session=True),

    # -- mock control plane (not Turnstone) --------------------------------
    Route("POST", "/v1/api/admin/mock/reset", handle_mock_reset,
          note="MOCK ONLY: reset all state, for a test in another container"),
    Route("GET", "/v1/api/admin/mock/state", handle_mock_state,
          note="MOCK ONLY: full state snapshot"),
    Route("POST", "/v1/api/admin/mock/seed", handle_mock_seed,
          note="MOCK ONLY: create a workstream/transcript directly, with a pinned id"),
    Route("GET", "/v1/api/admin/mock/requests", handle_mock_requests,
          note="MOCK ONLY: the request log, for asserting what Odoo sent"),
    Route("GET", "/v1/api/admin/mock/routes", handle_mock_routes,
          note="MOCK ONLY: this table, with which entries are stubs and who owns them"),
]

# path -> methods, for a 405 that names what the path does accept.
_METHODS_BY_PATH = {}
for _route in ROUTES:
    _METHODS_BY_PATH.setdefault(_route.template, set()).add(_route.method)


def _prepare_path(path):
    """Strip the query and any trailing slash, so `/detail/` matches `/detail`."""
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path


def find_route(method, path):
    """The route for this method, or a 404/405 HttpError.

    Path resolution happens before authentication on purpose: the route table is
    public (it is in README.md), so answering 404 for an unknown path leaks
    nothing, and doing it this way means "unknown path" and "bad token" are each
    testable without the other in the way.
    """
    path = _prepare_path(path)
    allowed = set()
    for route in ROUTES:
        if not route.match(path):
            continue
        if route.method == method:
            return route
        allowed.add(route.method)
    if allowed:
        raise HttpError(
            405, "method not allowed",
            headers={"Allow": ", ".join(sorted(allowed))},
            path=path, allowed=sorted(allowed),
        )
    raise not_found("not found", path=path,
                    hint="see docker/mock-turnstone/README.md for implemented paths")


def check_auth(authorization, route, config):
    """Raise 401 unless the request carries the configured bearer token.

    `hmac.compare_digest` rather than `==` so the check does not leak prefix
    length through timing — this is a mock, but a mock that teaches the wrong
    pattern gets copied.
    """
    if route.is_public(config):
        return
    expected = "Bearer %s" % config.token
    presented = authorization or ""
    if not hmac.compare_digest(presented.strip(), expected):
        raise HttpError(
            401, "missing or invalid bearer token",
            headers={"WWW-Authenticate": 'Bearer realm="turnstone-mock"'},
            hint="send 'Authorization: Bearer $MOCK_TURNSTONE_TOKEN'",
        )


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


class MockRequestHandler(BaseHTTPRequestHandler):
    """One request in, one JSON body out. No templates, no static files."""

    # Keep-alive is worth having: a test driver makes dozens of calls and
    # urllib's default is one connection per request anyway.
    protocol_version = "HTTP/1.1"
    server_version = "MockTurnstone/0.1"
    sys_version = ""

    # -- output ------------------------------------------------------------

    def _send_json(self, status, payload, headers=None):
        """Send one JSON body.

        `payload` is serialised exactly as given — a top-level list stays a list,
        because the addon's catalogue methods branch on `isinstance(data, list)`
        and an envelope here would be read as "no skills".
        """
        body = json.dumps(payload, indent=None, separators=(",", ":"), default=str)
        body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Mock-Turnstone", MOCK_VERSION)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return body

    def _read_body(self):
        """Read exactly Content-Length bytes, or refuse.

        Always drained, even for a 501 stub: a body left in the socket becomes
        the next request's headers on a keep-alive connection, which produces a
        baffling failure somewhere else entirely.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise bad_request("invalid Content-Length") from exc
        if length > MAX_BODY_BYTES:
            raise HttpError(413, "request body too large", limit=MAX_BODY_BYTES)
        raw = self.rfile.read(length) if length > 0 else b""
        # Kept for the request log, which is written after the handler ran and
        # cannot re-read the socket.
        self._raw_body_cache = raw
        self._body_consumed = True
        return raw

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self):
        started = time.perf_counter()
        self._raw_body_cache = b""
        self._body_consumed = False
        self._extra_headers = {}
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query, keep_blank_values=True)
        # HEAD answers whatever GET answers, minus the body. Anything else has to
        # match exactly, so an accidental POST to a read path gets a 405 that
        # names the methods the path does accept.
        lookup_method = "GET" if self.command == "HEAD" else self.command
        status, payload = 500, {"error": "handler produced no response"}
        try:
            config = self.server.config
            route = find_route(lookup_method, path)
            check_auth(self.headers.get("Authorization"), route, config)
            raw_body = self._read_body()
            ctx = Context(self, lookup_method, _prepare_path(path), query, raw_body,
                          route, config)
            if route.session:
                ctx.resolve_session()
            result = route.handler(ctx)
            # Handlers return (status, payload); a bare dict is a convenience for
            # the one-liners that always mean 200.
            if isinstance(result, tuple):
                status, payload = result
            else:
                status, payload = 200, result
        except HttpError as exc:
            status, payload = exc.status, exc.payload
            self._extra_headers = dict(exc.headers)
            if status >= 500:
                _logger.warning("%s %s -> %s %s", self.command, path, status,
                                exc.payload.get("error"))
        except KeyError as exc:
            # A pinned seed id that is already taken, or a record that vanished
            # between resolution and save. Both are the caller's mistake.
            status, payload = 409, {"error": str(exc)}
        except ValueError as exc:
            # Bad seed input and bad JSON both land here; both are the caller's.
            status, payload = 400, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never leak a traceback over the socket
            _logger.exception("unhandled error for %s %s", self.command, path)
            status, payload = 500, {"error": "mock server error: %s" % exc}
        finally:
            if not self._body_consumed:
                # A request body nobody read is still in the socket, and on a
                # keep-alive connection it would be parsed as the NEXT request's
                # headers — a failure that points at the wrong request entirely.
                # Closing is cheaper than draining a body we refused to trust.
                self._extra_headers["Connection"] = "close"
                self.close_connection = True
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            state.STORE.log_request({
                "method": self.command,
                "path": path,
                "query": {k: v[0] if len(v) == 1 else v for k, v in query.items()},
                "status": status,
                "ms": elapsed_ms,
                # The body is recorded because the question this log answers is
                # "what did Odoo actually send", but truncated, and the token is
                # never part of it.
                "body": self._body_preview(),
            })
            self._send_json(status, payload, self._extra_headers)
            _logger.info("%s %s -> %s (%sms)", self.command, path, status, elapsed_ms)

    def _body_preview(self):
        """A short preview of the request body for the request log."""
        raw = self._raw_body_cache or b""
        if not raw:
            return ""
        text = raw[:2000].decode("utf-8", "replace")
        return text + ("…" if len(raw) > 2000 else "")

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def log_message(self, fmt, *args):
        """Silence BaseHTTPRequestHandler's own stderr line; we log our own."""
        _logger.debug("%s - %s", self.address_string(), fmt % args)


class MockServer(ThreadingHTTPServer):
    """Threaded, because a poll and a send really do overlap.

    A single-threaded mock would hide the concurrency the session-persistence
    investigation (OTI-5729) found: two pollers and a send in flight at once.
    `daemon_threads` keeps Ctrl-C working while a slow handler sleeps.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler=MockRequestHandler, config=None):
        super().__init__(address, handler)
        self.store = state.STORE
        self.config = config or Config.from_env()


def make_server(config=None):
    """Build a server bound to the configured address, without serving.

    Separated from `main()` so a test can start one on port 0 in a thread and
    read `server.server_address` for the real port — and so two servers with
    different tokens can exist in one process.
    """
    cfg = config or Config.from_env()
    return MockServer((cfg.host, cfg.port), config=cfg)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # Flags are deliberately minimal: everything that shapes behaviour is an env
    # var, because the compose service and the container healthcheck can only
    # pass env. These two exist so that a person who types `--help` is not left
    # with a running server and no explanation — argv used to be ignored.
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    unknown = [a for a in argv if a not in ("--version",)]
    if unknown:
        print("mock-turnstone: unknown argument(s): %s\n" % " ".join(unknown), file=sys.stderr)
        print("configure it with environment variables; try --help", file=sys.stderr)
        return 2
    config = Config.from_env()
    if "--version" in argv:
        print(MOCK_VERSION)
        return 0
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    server = make_server(config)
    host, port = server.server_address[:2]
    # Printed to stdout rather than logged: whoever started it is waiting for
    # this line, and "which port did it pick" is the first thing they need.
    print(config.describe(), flush=True)
    print("mock-turnstone bound to %s:%s — paths: docker/mock-turnstone/README.md"
          % (host, port), flush=True)

    def stop(signum=None, frame=None):
        # shutdown() blocks until serve_forever returns and must not run on the
        # thread inside serve_forever, which is where a signal handler runs.
        _logger.info("shutting down on signal %s", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        _logger.info("mock-turnstone stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
