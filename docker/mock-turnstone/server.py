#!/usr/bin/env python3
"""A dependency-free mock of the Turnstone HTTP API.

The addon has no automated tests and cannot be exercised without a Turnstone
server, so this process stands in for one. It speaks the exact paths
`addons/turnstone_integration/models/turnstone_client.py` calls, holds its state
in memory (`state.py`), and performs **no agent work at all**: no model call, no
tool execution, no timing simulation. A canned reply is a string read from the
environment (OTI-5738); that is the whole of it.

Run it with no arguments and no `.env`:

    python docker/mock-turnstone/server.py

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
            "%d permissions, health %s)"
            % (
                self.host,
                self.port,
                len(self.permissions),
                "public" if self.health_public else "token-protected",
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
        """
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
        messages, pending_approval_details, meta
    """
    body = ctx.json_body()
    if not isinstance(body, dict):
        raise bad_request("seed body must be a JSON object")
    kind = body.get("kind") or "interactive"
    if kind not in ("coordinator", "interactive"):
        raise bad_request("kind must be 'coordinator' or 'interactive'")
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        raise bad_request("messages must be a list")

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
    Route("POST", "/v1/api/workstreams/new",
          reserved("OTI-5734", "coordinator creation on the console"),
          surface="console",
          note="console only, and only ever coordinators: takes no kind and no model"),
    Route("POST", "/v1/api/route/workstreams/new",
          reserved("OTI-5734", "interactive creation through the routing proxy"),
          surface="route",
          note="the only surface accepting kind; console would hand back a coordinator"),

    # -- cluster-wide reads ------------------------------------------------
    Route("GET", "/v1/api/cluster/overview", handle_cluster_overview,
          note="summary strip on Live Activity"),
    Route("GET", "/v1/api/cluster/workstreams",
          reserved("OTI-5734", "cluster workstream feed"),
          note="feed used for children and Live Activity; row shape lands with the list ticket"),
    Route("GET", "/v1/api/cluster/ws/{ws_id}/detail",
          reserved("OTI-5734", "session detail"),
          session=True,
          note="the one surface returning persisted + live + messages for any node"),

    # -- transcript --------------------------------------------------------
    Route("GET", "/v1/api/workstreams/{ws_id}/history",
          reserved("OTI-5736", "console session history"),
          surface="console", session=True,
          note="console-only: an interactive session's history lives on its node"),
    Route("GET", "/v1/api/route/workstreams/{ws_id}/history",
          reserved("OTI-5736", "routed session history"),
          surface="route", session=True,
          note="declared for symmetry; the client reads history via cluster detail"),

    # -- per-session control, on both surfaces ------------------------------
    *both_surfaces("POST", "/v1/api/workstreams/{ws_id}/send",
                   reserved("OTI-5738", "prompt submission"), owner="OTI-5738",
                   note="appends the user turn and one canned assistant reply"),
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
                   reserved("OTI-5736", "drop the last N turns"), owner="OTI-5736",
                   note="needed to prove the mirror follows a server-side rewind"),
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
    config = Config.from_env()
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
