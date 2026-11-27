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

`--help` prints the module docstring and `--version` prints the mock's version.
There are no other flags — everything that shapes behaviour is an environment
variable, because a compose service and a container healthcheck can only pass
env. An unrecognised argument exits 2 with a message rather than being silently
ignored, so a typo does not leave you with a server running the wrong shape.

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

Route table as of OTI-5736: **19 of 37 declared paths do something, the rest
answer 501 naming their owner** (14 unclaimed, 2 owned by OTI-5738 in this
story). The live table is always available from the running process:

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
| POST | `/v1/api/workstreams/new` | **console create, coordinators only.** `name`, `skill`, `persona`, `initial_message`. Returns `ws_id`, `node_id`, `kind`, the rest of the row, and the whole record under `workstream`. There is no `kind` field on this endpoint: send one and you get a coordinator anyway (it is recorded under `meta.kind` so the mistake is visible) |
| POST | `/v1/api/route/workstreams/new` | **proxy create, interactive only.** Accepts `kind` (default `interactive`), `model`, `judge_model`, `client_type` and the auto-approve fields. Refuses `kind: "coordinator"` with a 400 naming the console path. Same response shape as the console |
| GET | `/v1/api/cluster/workstreams` | The cluster feed: `workstreams[]`, `total`, `page`, `per_page`. Query: `page`, `per_page`, `state` (exact), `search` (id/name/title, case-insensitive). Rows carry `id` **and** `ws_id`, `parent_ws_id`, `model_alias` **and** `model`, `node` **and** `node_id`, counters — and **no** transcript |
| GET | `/v1/api/cluster/ws/{ws_id}/detail` | One session of either kind: `persisted` (row: `ws_id`, kind, parent, persona, node, state, title, model), `live` (state, tokens, tool_calls, context_ratio, activity, activity_state, `pending_approval_details`) and `messages` — always present, `[]` when empty. `message_limit` bounds the tail when you ask for one |
| GET | `/v1/api/workstreams/{ws_id}/history` | **A coordinator's transcript**, oldest first: `ws_id`, `messages`, and `cursor`/`handoff_token` (both `null`; they belong to the SSE handoff this mock has none of). `limit` bounds the tail (max 500) |
| GET | `/v1/api/route/workstreams/{ws_id}/history` | The same transcript for an **interactive** session, through its own surface. Identical body |
| POST | `/v1/api/workstreams/{ws_id}/rewind` (+ `/route/` twin) | Drop the last N **user turns** and everything answering them: `{"turns": N}`, clamped to what the session has. Answers `{"status":"ok","dropped":…,"remaining":…}` |
| POST | `/v1/api/admin/mock/reset` | **mock only.** Back to the post-startup shape: no workstreams, ids restart at `ws-000001`, catalogue re-seeded, request log cleared |
| GET | `/v1/api/admin/mock/state` | **mock only.** Full state snapshot for assertions |
| POST | `/v1/api/admin/mock/seed` | **mock only.** Create a workstream directly, with any state, transcript, counters and a pinned `ws_id`. `append: true` instead plants `messages` onto an existing `ws_id` |
| GET | `/v1/api/admin/mock/requests` | **mock only.** The request log: method, path, query, status, ms, truncated body |
| GET | `/v1/api/admin/mock/routes` | **mock only.** The route table above, live from the running process, with `implemented` and `owner` per entry |

### Declared, answering 501 until their ticket lands

Path shapes are fixed here now so a later ticket adds behaviour instead of
reshaping URLs. The `owner` in the 501 body is the ticket.

| Method | Path | Owner |
|---|---|---|
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

## Transcripts (OTI-5736)

A session's conversation is `record["messages"]`, oldest first, and it comes
back from three places that all read the same list: `detail` under `messages`,
`history` on the session's own surface, and the seed/plant helpers. A session
with no turns holds `[]` — never `null`, never a missing key — because
`detail.get("messages") or []` cannot tell "absent" from "empty" and a missing
key is how a real sync wipes a good mirror.

### The message shape

Every row is normalised by `state.normalise_message()` on the way in — whatever
wrote it (a create's `initial_message`, a seed, a plant, a rewind survivor), so
there is exactly one spelling and a test cannot pass against a shape the addon
does not parse:

| Key | Always | Notes |
|---|---|---|
| `role` | yes | `user` / `assistant` / `tool` / `system`. Anything else is **refused**, not stored: `turnstone_message._render` silently *skips* an unknown role, so accepting one would make a seeded turn vanish from the mirror with no error anywhere |
| `content` | yes | A string, or a list of `{"type":"text","text":…}` parts. A bare string inside a part list is wrapped into a part, because the parser reads `part.get("text")` and would otherwise read the turn as `""` |
| `timestamp` | yes | Epoch seconds by default (stepping 1 ms per row so an untimed fixture still reads in order), or a value you supply. The addon ignores it; a chat UI shows one |
| `tool_calls` | when present | Canonical OpenAI: `[{"id","type":"function","function":{"name","arguments"}}]` with `arguments` a **JSON string**. A loose `{name, arguments}` is upgraded and a dict `arguments` is dumped, because `turnstone_session._sync_linked_records` json.loads the string to find the model name it links the call to |
| `tool_call_id` | on a `tool` result | `call_id` is accepted on input and normalised to `tool_call_id`, the spelling both parsers prefer |

The body key is `content`, not `text`: `turnstone_message._render` reads
`msg.get("content")`. `text` works as an input alias and is never written.

```sh
T='Authorization: Bearer mock-token'; B=http://localhost:8099

# A console create makes a COORDINATOR (see the split below), so plant and read
# it on the console path. For an interactive session use
# /v1/api/route/workstreams/new and the /route/ twins instead.
curl -X POST -H "$T" $B/v1/api/workstreams/new -d '{"name":"Planted"}'

# Plant a transcript on a session you already created
curl -X POST -H "$T" $B/v1/api/admin/mock/seed \
  -d '{"append":true,"ws_id":"ws-000001","messages":[
        {"role":"user","content":"hello"},
        {"role":"assistant","content":"canned",
         "tool_calls":[{"name":"odoo_create","arguments":{"model":"res.partner"}}]},
        {"role":"tool","content":"{\"id\": 7}","call_id":"call-1-0"}]}'

curl -H "$T" $B/v1/api/workstreams/ws-000001/history     # the same rows detail carries
curl -X POST -H "$T" $B/v1/api/workstreams/ws-000001/rewind -d '{"turns":1}'
# {"dropped":3,"remaining":0,"turns":1,"status":"ok","ws_id":"ws-000001"}
```

### Grounded in the real server, not guessed

The paths, the `limit`/`turns` parameters and the two response shapes were read
from the published `openapi.json` of a real Turnstone 1.8.3 (it is served
without auth), so the mock cannot quietly model an API that does not exist:
`WorkstreamHistoryResponse` = `{ws_id, messages, cursor, handoff_token}`,
`RewindRequest` = `{turns}` and `StatusResponse` = `{status}`.

Two deliberate divergences, both about the harness being *more* useful than the
real server, and both recorded where they live in the code:

* **The tail defaults to the whole transcript.** The real `detail` defaults to
  `message_limit=20` and `history` to `limit=100`. A silent 20-row default would
  truncate exactly the fixtures this mock exists to serve, so a tail is applied
  only when you ask for one. OTI-5740 owns reproducing the bounded tail as a
  *failure mode*, on purpose.
* **`history` answers on both surfaces.** 1.8.3's spec lists the console path
  only. The route twin exists so an interactive session's transcript is
  readable at all; the pairing is still decided in `resolve_session`, so the
  console path refuses an interactive session exactly as before. If OTI-5763
  confirms interactive history is only served through cluster detail, the twin
  should be withdrawn rather than kept for symmetry.

### Rewind is how a transcript gets shorter

`rewind` drops the last N **user turns** and everything answering them — it
finds the Nth `user` row from the end and truncates there, rather than deleting
the last N rows, which would leave half a turn behind. `turns` is clamped to
what the session has, exactly as the real endpoint documents. This exists
because a mirror that only ever grows cannot be shown to follow the server:
OTI-5744's guards are written against a shorter list that is not a prefix of
ours, and a test needs to cause one on demand.

The status body also carries `dropped` and `remaining`, which the real server
does not send — additive on a response nothing parses strictly, so an assertion
can say "three rows went, none left" without a second read.

## Creating workstreams, and the split at creation time

Two endpoints, two surfaces, two handlers — the same split as above, decided
once per surface rather than again per request:

| Endpoint | Makes | Takes |
|---|---|---|
| `POST /v1/api/workstreams/new` (console) | a **coordinator**, always | `name`, `skill`, `persona`, `initial_message` — no `kind`, no `model` |
| `POST /v1/api/route/workstreams/new` (proxy) | an **interactive** session | `kind` (default `interactive`), `model`, `judge_model`, `client_type`, `auto_approve`, `auto_approve_tools`, `name`, `skill`, `persona`, `initial_message` |

```sh
T='Authorization: Bearer mock-token'; B=http://localhost:8099

# A coordinator. Note there is no kind field to get wrong.
curl -X POST -H "$T" $B/v1/api/workstreams/new \
  -d '{"name":"Coord","skill":"mock-coordinator","initial_message":"plan the work"}'
# {"ws_id":"ws-000001","node_id":"node-mock-1","kind":"coordinator",...}

# An interactive session, which is the only kind the proxy can make.
curl -X POST -H "$T" $B/v1/api/route/workstreams/new \
  -d '{"kind":"interactive","name":"Session","model":"mock-model","client_type":"web"}'

# Asking the proxy for a coordinator is a 400 that names the right endpoint.
curl -X POST -H "$T" $B/v1/api/route/workstreams/new -d '{"kind":"coordinator"}'
# {"error":"kind 'coordinator' cannot be created through the routing proxy",
#  "detail":"coordinator workstreams must be created on the console via POST /v1/api/workstreams/new"}
```

Both answer **201** with `ws_id` and `node_id` at the top level, because that is
literally what `turnstone_session._ensure_started` writes onto the Odoo row — a
create response without `ws_id` is failure mode 1 in the persistence
investigation (the user's message silently goes nowhere), so the mock never
produces one. Everything the endpoint cannot act on (`client_type`,
`judge_model`, the auto-approve lists) is kept on the record under `meta`, so
"what did Odoo ask for" is answerable without pretending the mock honours it.

`initial_message` becomes the first **user** turn of the stored transcript, and
the state stays `idle`: this server runs no model, so it will not claim to be
`thinking` about a reply that is never coming. The assistant side is OTI-5738's
canned reply.

## Listing and detail

```sh
curl -H "$T" "$B/v1/api/cluster/workstreams?per_page=50&page=1"
curl -H "$T" "$B/v1/api/cluster/workstreams?state=attention"
curl -H "$T" "$B/v1/api/cluster/workstreams?search=coord"
curl -H "$T" $B/v1/api/cluster/ws/ws-000001/detail
```

`per_page` and `page` are applied **after** `state` and `search`, so `total` is
how many workstreams match rather than how many the page holds. Order is
creation order, oldest first, which is the order ids are handed out in — an
assertion can name a position instead of sorting. A page past the end is an
empty list with a correct `total`, not a 404.

Feed rows are summaries: `id` and `ws_id` both carry the id, `model_alias` and
`model` both carry the alias, `node` and `node_id` both carry the node, because
the addon reads one spelling in `turnstone_session._children` and the other in
`controllers/main.py`. `messages` is deliberately absent from a row — 200 rows
of transcript would be enormous, and detail is where the transcript lives.

Detail is the one endpoint that answers for **either** kind (it is how the addon
reads an interactive session, whose history lives on its node and not on the
console). `messages` is present and `[]` when there are no turns: a missing key
is what makes the Odoo client treat a healthy session as an empty conversation
and wipe its mirror, so the mock never omits it.

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
  `cluster/ws/{id}/detail` with state/kind/model/counters. **Done**: see
  "Creating workstreams" and "Listing and detail" above. The detail endpoint
  returns `messages` from whatever a create's `initial_message` or a seed
  planted; the message *shapes* are OTI-5736's.
* **OTI-5736** — transcripts: `detail` carries `messages`, `history` mirrors it
  on both surfaces, and `state.py` normalises every row and can plant one.
  **Done**: see "Transcripts" above. `rewind` came with it, because it is the
  only way a transcript gets shorter.
* **OTI-5738** — prompt post appends the user turn plus one canned assistant
  turn and moves the token counters.
* **OTI-5740** — switchable failure modes (500, slow, malformed JSON, empty
  history, `{"error": "coordinator not found"}`) so error handling can be tested
  on purpose.
