# Mock Turnstone server

A stand-in for a Turnstone console, so this addon can be exercised without one.
It is **stdlib only** — no pip install, no requirements file, no vendored
packages — and it performs **no agent work**: no model call, no tool execution,
no timing simulation. It answers the HTTP paths
`addons/turnstone_integration/models/turnstone_client.py` actually calls, with
the response shapes that client parses, and keeps everything in memory.

It exists because the addon has no automated tests and no way to prove
behaviour without a live Turnstone, and because the failure modes that matter
most (an empty message tail, a 200 body containing `{"error": …}`, a create that
returns no `ws_id`) are impossible to trigger against a healthy real server.
See `discoveries/session-persistence-investigation.md` in the project KB for the
shapes that investigation had to simulate by hand — this server is the
reusable version of that stub.

## Run it

```sh
python docker/mock-turnstone/server.py
```

No arguments, no `.env`, no install. It binds `0.0.0.0:8099`, prints the two
lines you need (config summary, then the bound address), and answers:

```sh
curl -H 'Authorization: Bearer mock-token' http://localhost:8099/health
# {"status":"ok","versions":["mock-turnstone/0.1"],"mock":true,...}
```

`python3` and `python` both work; it is written for Python 3.9+ (no match
statements, no third-party imports).

### In-process, for a test that can import it

`server.py` is importable, and `make_server()` returns a bound-but-not-serving
`HTTPServer`, so a test can run one on an ephemeral port in a background thread
and read back the real port. Configuration is per server, so two mocks with
different tokens can exist in one process:

```python
import sys, threading
sys.path.insert(0, "docker/mock-turnstone")
import server

cfg = server.Config({"MOCK_TURNSTONE_HOST": "127.0.0.1", "MOCK_TURNSTONE_PORT": "0",
                     "MOCK_TURNSTONE_TOKEN": "test-token"})
srv = server.make_server(cfg)
threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                 daemon=True).start()
base_url = "http://127.0.0.1:%d" % srv.server_address[1]
...
srv.shutdown()
```

State is process-wide (`state.STORE`), not per server: two in-process servers
share one store. That is deliberate — the store is the thing a test resets, and
having two would make `state.reset()` ambiguous. An Odoo test case runs in a
different container and uses `POST /v1/api/admin/mock/reset` instead.

## Configuration

All read from the environment **at startup**, so restart after changing one.

| Variable | Default | Meaning |
|---|---|---|
| `MOCK_TURNSTONE_HOST` | `0.0.0.0` | bind address. `0.0.0.0` so an Odoo container can reach it; use `127.0.0.1` to keep it off your network |
| `MOCK_TURNSTONE_PORT` | `8099` | bind port. Deliberately not `8069` (Odoo) |
| `MOCK_TURNSTONE_TOKEN` | `mock-token` | the bearer token **every** request must present (except `/health` when made public, below) |
| `MOCK_TURNSTONE_HEALTH_PUBLIC` | `0` | `1` lets `GET /health` skip the token check, for a container `HEALTHCHECK`. Default keeps health token-protected like everything else |
| `MOCK_TURNSTONE_PERMISSIONS` | 7 mock scopes | comma-separated permissions reported by `/v1/api/auth/whoami`; set it to pretend the token is weaker than it is |
| `MOCK_TURNSTONE_LOG_LEVEL` | `INFO` | one line per request goes to stderr at `INFO`; `WARNING` silences the access log, `DEBUG` adds BaseHTTPRequestHandler's own line |

The token is never printed and never logged. Point an Odoo `turnstone.connection`
at this server with base URL `http://mock-turnstone:8099` (compose) or
`http://localhost:8099` (host) — the client appends `/v1/api` itself, so do
**not** include it.

## Authentication and error contract

* Missing or wrong bearer token → **401** `{"error": "missing or invalid bearer token"}`
  with a `WWW-Authenticate` header.
* Unknown path → **404** `{"error": "not found", "path": …}`.
* Known path, wrong method → **405** `{"error": "method not allowed", "allowed": [...]}`
  with an `Allow` header.
* Malformed JSON body → **400** `{"error": "invalid JSON body: …"}`.
* A path declared here whose behaviour belongs to a later ticket → **501**
  `{"error": "not implemented in the mock yet: …", "owner": "OTI-####"}`. 501 and
  not 404 on purpose: the URL is right, the behaviour is not there yet, and the
  body names the ticket that fills it.
* Server-side surprise → **500** `{"error": "mock server error: …"}`.

**Path resolution happens before authentication.** An unknown path answers 404
even with no token; a known one answers 401. The route table is published below,
so this leaks nothing, and it means "unknown path" and "bad token" are each
testable without the other in the way.

Every error body is an object with an `error` key because that is what the Odoo
client looks for when a body arrives at HTTP 200 (`turnstone_session._sync_from_server`
treats such a body as an empty session today — see OTI-5744). Responses are sent
exactly as the handler wrote them: a top-level JSON list stays a list, because
`TurnstoneClient.skills()`/`personas()` branch on `isinstance(data, list)`.

## Implemented paths

Route table as of this ticket: **11 of 37 declared paths do something, the rest
answer 501 naming their owner** (16 unclaimed, 12 owned by a later ticket in
this story). The live table is always available from the running process:

```sh
curl -H 'Authorization: Bearer mock-token' http://localhost:8099/v1/api/admin/mock/routes
```

### Working now

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | **outside** `/v1/api`, which is where the client calls it. Returns `status`, `versions` (printed by `action_test_connection`), `node_id`, `uptime_s`, and a `mock: true` marker |
| GET | `/v1/api/auth/whoami` | `username`, `user_id`, `permissions` (comma-joined, as `action_test_connection` splits it), `auth_type`, `scopes` |
| GET | `/v1/api/admin/skills` | bare JSON **list**. The admin endpoint, not `/skills`, because the public one omits `kind` and kind decides whether a session can delegate |
| GET | `/v1/api/admin/personas` | bare JSON list |
| GET | `/v1/api/models` | `{"models": [...]}`. Inert in the addon today (aliases come from the cluster feed) but the client has the method, so the mock answers it |
| GET | `/v1/api/cluster/overview` | `workstreams`, `nodes`, `states.{idle,thinking,attention,error,closed}`, `aggregate.{tokens,tool_calls,total_tool_calls,total_tokens}` — the keys `turnstone_live.xml` renders |
| POST | `/v1/api/admin/mock/reset` | **mock only.** Back to the post-startup shape: no workstreams, ids restart at `ws-000001`, catalogue re-seeded, request log cleared |
| GET | `/v1/api/admin/mock/state` | **mock only.** Full state snapshot for assertions |
| POST | `/v1/api/admin/mock/seed` | **mock only.** Create a workstream directly, with any state, transcript, counters and a pinned `ws_id` |
| GET | `/v1/api/admin/mock/requests` | **mock only.** The request log: method, path, query, status, ms, truncated body |
| GET | `/v1/api/admin/mock/routes` | **mock only.** The route table above, live from the running process, with `implemented` and `owner` per entry |

### Declared, answering 501 until their ticket lands

Path shapes are fixed here now so a later ticket adds behaviour instead of
reshaping URLs. The `owner` in the 501 body is the ticket.

| Method | Path | Owner |
|---|---|---|
| POST | `/v1/api/workstreams/new` | OTI-5734 — console creation, coordinators only (takes no `kind`, no `model`) |
| POST | `/v1/api/route/workstreams/new` | OTI-5734 — routing proxy creation, the only surface accepting `kind` |
| GET | `/v1/api/cluster/workstreams` | OTI-5734 — the cluster feed (children, Live Activity) |
| GET | `/v1/api/cluster/ws/{ws_id}/detail` | OTI-5734 — persisted + live + messages for any node |
| GET | `/v1/api/workstreams/{ws_id}/history` | OTI-5736 — console history |
| GET | `/v1/api/route/workstreams/{ws_id}/history` | OTI-5736 — declared for symmetry; the client reads history via cluster detail |
| POST | `/v1/api/workstreams/{ws_id}/rewind` and its `/route/` twin | OTI-5736 — needed to prove the mirror follows a server-side rewind |
| POST | `/v1/api/workstreams/{ws_id}/send` and its `/route/` twin | OTI-5738 — prompt post and canned reply |
| GET | `/v1/api/admin/schedules` | unclaimed — Schedules screen, no ticket yet |
| GET | `/v1/api/admin/schedules/{task_id}/runs` | unclaimed |
| DELETE | `/v1/api/workstreams/{ws_id}/send` (+ `/route/`) | unclaimed — cancel a queued message |
| POST | `/v1/api/workstreams/{ws_id}/approve` (+ `/route/`) | unclaimed — approval resolution |
| POST | `/v1/api/workstreams/{ws_id}/cancel` (+ `/route/`) | unclaimed |
| POST | `/v1/api/workstreams/{ws_id}/retry` (+ `/route/`) | unclaimed |
| POST | `/v1/api/workstreams/{ws_id}/close` (+ `/route/`) | unclaimed |
| GET | `/v1/api/workstreams/{ws_id}/children` | unclaimed — coordinator-only in Turnstone; `_children()` reads the cluster feed instead |
| GET/POST/DELETE | `/v1/api/workstreams/{ws_id}/attachments[...]` | unclaimed — coordinator-only, multipart upload |

"Unclaimed" means no ticket in story OTI-698 declares it yet. If a test needs
one, say so on the harness ticket rather than adding a silent path here.

## The console/route split, and why it matters here

Turnstone runs two per-session surfaces and they are **not** interchangeable:

* a **coordinator** lives on the console: `/v1/api/workstreams/{id}/...`
* an **interactive session** lives on a node, reached through the routing proxy:
  `/v1/api/route/workstreams/{id}/...`

Using the wrong one does not degrade gracefully, and the shapes are different
enough that a mock returning a generic 404 would hide the bug:

| Situation | Status | Body |
|---|---|---|
| console path, interactive session | 404 | `{"error": "coordinator not found"}` — the exact body the real console returns |
| route path, coordinator | 404 | `{"error": "workstream not found", "detail": "the routing proxy hosts interactive sessions only"}` |
| unknown id, any surface | 404 | `{"error": "workstream not found"}` |

The split is enforced in one place (`Context.resolve_session`), not per handler,
so a later handler cannot accidentally opt out of it.

## Mock-only control plane

These are **not** Turnstone endpoints and no Odoo code calls them. They sit under
`/v1/api/admin/mock/` so they share the token check and the error envelope, and
so their names cannot collide with a real path. They exist because an Odoo
`TransactionCase` runs in another container and cannot import `state.py`: HTTP is
how a test resets and seeds.

```sh
T='Authorization: Bearer mock-token'
B=http://localhost:8099

# Start each test from a known shape.
curl -X POST -H "$T" $B/v1/api/admin/mock/reset

# Plant a session with a transcript before anyone opens chat, and pin its id.
curl -X POST -H "$T" $B/v1/api/admin/mock/seed \
  -d '{"kind":"interactive","name":"Seeded","ws_id":"ws-test-1",
       "messages":[{"role":"user","content":"hello"},
                   {"role":"assistant","content":"canned reply"}]}'

# A coordinator, for the split tests.
curl -X POST -H "$T" $B/v1/api/admin/mock/seed -d '{"kind":"coordinator","name":"Coord"}'

# An approval waiting on a human, which the module maps to "attention".
curl -X POST -H "$T" $B/v1/api/admin/mock/seed \
  -d '{"kind":"coordinator","pending_approval_details":[{"cycle_id":"c1"}]}'

# What did Odoo actually send, and in what order?
curl -H "$T" "$B/v1/api/admin/mock/requests?path_prefix=/v1/api/route&method=POST"
```

`seed` accepts, all optional except `kind`: `kind`, `name`, `title`, `state`,
`skill`, `persona`, `model_alias`, `parent_ws_id`, `ws_id`, `tokens`,
`tool_calls`, `context_ratio`, `activity`, `activity_state`, `messages`,
`pending_approval_details`, `meta`. A seeded `pending_approval_details` defaults
the state to `attention` unless you pass an explicit `state`. A pinned `ws_id`
that is already taken answers **409** rather than replacing the existing
workstream — and the sequential generator skips ids you have pinned, so a later
create cannot collide with one.

The request log is capped at 500 entries (the counter keeps counting past the
cap), because a chat polls every couple of seconds for as long as a test runs.
Bodies are truncated at 2000 characters. The bearer token is never recorded.

## What this server deliberately does not do

* **No agent behaviour.** No model call, no tool execution, no delegation, no
  timing. A reply is a fixed string (OTI-5738, from the environment), which is
  the point: a test asserting on a transcript needs a reply it can predict.
* **No persistence.** State dies with the process, by design — a test that
  leaks state into the next one is a test that fails in a month for no reason.
  Nothing is written to disk anywhere.
* **No TLS, no multi-tenancy, no rate limiting.** It is a test double on a
  laptop or a compose network; treat every request as trusted apart from the
  token.

## Files

* `server.py` — route table, dispatch, auth, the working handlers, `main()`.
* `state.py` — the in-memory store, documented record shapes, `reset()`, seed and
  request-log helpers. **This is the module later tickets extend**; the record
  and message shapes are written down at the top of it.
* `README.md` — this file.

## Next in the story

This ticket (OTI-5733) is the core: bind/auth/404/route table. Then:

* **OTI-5734** — workstream creation on both surfaces, the cluster feed, and
  `cluster/ws/{id}/detail` with state/kind/model/counters.
* **OTI-5736** — transcripts: `detail` carries `messages`, a history endpoint
  mirrors it, and `state.py` gains the seed helper for pre-planted transcripts.
* **OTI-5738** — prompt post appends the user turn plus one canned assistant
  turn and moves the token counters.
* **OTI-5740** — switchable failure modes (500, slow, malformed JSON, empty
  history, `{"error": "coordinator not found"}`) so error handling can be tested
  on purpose.
