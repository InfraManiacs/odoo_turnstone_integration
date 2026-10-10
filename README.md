# odoo_turnstone_integration

An Odoo 19 module that puts a Turnstone agent chat inside Odoo, and links back
to whatever the agent touched.

Turnstone is the agentic harness — it runs the agent loop, holds durable session
state, and gates every tool call behind a policy. **This module runs no agent
logic of its own.** It is the UI and UX layer, and every decision is made by
asking Turnstone over its HTTP API.

## What it gives you

- **Agent Chat** — an OWL client action for talking to Turnstone agents, with
  agent output rendered as Markdown rather than dumped as raw text.
- **Tool approvals in-line** — an agent that stops for a human stops here, with
  the tool name, its arguments and any judge verdict shown. Approve or deny
  without leaving Odoo.
- **Subagents** — when a coordinator spawns children, they appear nested with
  live state and tool counts, and each can be opened in its own chat.
- **Steering** — stop a turn in flight, retry the last message, rewind a turn,
  or queue a message while the agent is still working.
- **Attach to running work** — join a workstream that was started outside Odoo:
  a scheduled run, a job from another system, or a subagent.
- **Live Activity** — what the whole Turnstone cluster is doing right now,
  nested parent/child, with the stuck and waiting ones first.
- **Linked records** — when an agent writes to Odoo through the Odoo MCP server,
  the session shows what it created or changed and links straight to it.
- **Sessions as Odoo records** — searchable, groupable, access-controlled like
  anything else.
- **Scheduled jobs** — Turnstone's cron tasks and run history, and a run can be
  opened to see exactly what that firing did.
- **Multiple servers** — connect to more than one Turnstone, with per-connection
  group restrictions.

## Quick start

```sh
cp .env.example .env            # only if 8069 is taken
docker compose up -d
docker compose exec odoo odoo --config=/etc/odoo/odoo.conf \
    -d turnstone --init turnstone_integration --stop-after-init
```

Then http://localhost:8069 — database `turnstone`, login `admin` / `admin`.

Add a server under **Turnstone → Configuration → Servers**: the console URL
(e.g. `https://turnstone.example.com`, no `/v1/api`) and an API token. Hit
**Test Connection** — it reports the identity and permission count the token
actually has, which is the fastest way to catch a token that is valid but
under-scoped.

## Permissions

| Group | Can |
|---|---|
| Turnstone / **User** | Chat, see **only their own** sessions, and attach to subagents **of their own sessions** |
| Turnstone / **Administrator** | Configure servers, hold API tokens, see **every** session, use Live Activity, attach to **any** workstream |

Administrator is a real privilege, not just a config role: a transcript contains
whatever the agent read, and the agent reads with the **server token's**
authority rather than the chatting user's.

Per-connection `allowed_group_ids` narrows access further. It can only narrow —
Turnstone User is still required.

Verified against a non-manager user: cannot see others' sessions, cannot read
`api_token`, cannot write connections, cannot drive another user's session
through the controllers, cannot open Live Activity or Schedules, and **cannot
attach to a workstream that is not a subagent of a session they own**.

That last one matters more than it looks. Attaching hands over a whole
transcript, and a transcript contains whatever the agent read — using the
**server token's** authority, not the caller's. Knowing a workstream id must not
be enough to read it.

## Design notes

**The API token never reaches the browser.** The OWL client calls this module's
Odoo controllers; the controllers call Turnstone server-side. The token is a
bearer credential for a harness that can reach Odoo, a Kubernetes cluster and
anything else its MCP servers expose — in the browser it would hand every chat
user the full authority of the server token regardless of their Odoo groups.

**Sessions are created against the route endpoint, not the console one.**
`POST /v1/api/workstreams/new` only ever creates `coordinator` sessions, which
delegate to spawned children instead of running tools themselves. Only
`POST /v1/api/route/workstreams/new` accepts `kind`, so the client asks for
`interactive`.

**An interactive session's history is not on the console.** It lives on whichever
node the rendezvous hash placed it on, so `/workstreams/{id}/history` returns
nothing for it. This module reads `/cluster/ws/{id}/detail`, the one surface that
returns the persisted row, the live block and the message tail together.

**The transcript is replaced, not appended.** Turnstone can rewind or retry a
turn, which removes messages. Diffing an append-only mirror against a mutable
source is how you end up showing a reply that was taken back.

**Coordinators and interactive sessions are driven through different APIs.**
A coordinator is hosted by the console (`/workstreams/{id}/...`); an interactive
session lives on a node and is reached through the routing proxy
(`/route/workstreams/{id}/...`). Using the wrong one does not degrade: the proxy
returns HTTP 404 for a coordinator and the console returns
`{"error": "coordinator not found"}` for an interactive session. Every
per-session call therefore takes the session's `kind`.

**Subagents are persisted, not read live.** They are discovered from Turnstone's
cluster feed via `parent_ws_id` — `/workstreams/{id}/children` is coordinator-only
and 404s otherwise — but that feed lists only workstreams Turnstone still holds.
A child that finishes drops out of it, so reading live makes the subagent panel
count up to two and then empty itself. Each sync upserts what it sees and marks
the rest finished, because "two subagents ran and both completed" is the useful
answer.

**Agent output is rendered escape-first.** `static/src/js/markdown.js` escapes
every character before any transform runs, so the only markup in the output is
markup it wrote; DOMPurify (already bundled by `web`) runs as a second,
independent layer. This text is written by a model that reads untrusted input —
pod logs, email bodies, web pages — so "the agent would not emit a script tag"
is not a security argument.

**The chat stays live for as long as it is open.** Polling only while the agent
is "thinking" is not enough — an approval can land, a subagent can finish, a
scheduled job can move, or someone can steer the same session from the Turnstone
console, all while this session reads as idle. The loop runs continuously and
only changes pace: fast mid-turn, slower when idle, stopped when the tab is
hidden and resumed immediately when it comes back. Requests never stack behind a
slow sync, and a transient failure has to happen three times before it bothers
the user.

Turnstone does expose SSE, and it is tempting. But the API token must stay
server-side, so streaming would mean holding an Odoo worker open per viewer.
Adaptive polling through the existing proxy is the honest trade at this scale.

**Linked records are parsed from the transcript**, because the agent's writes
reach Odoo over JSON-RPC through the Odoo MCP server — Odoo has no idea they came
from an agent. A tool call to `mcp__odoo__odoo_create` names the model, and the
matching tool result carries the id. Where the id lives differs per operation and
getting it wrong produces a link that looks right and points at the wrong record:

| tool | model from | id from |
|---|---|---|
| `odoo_create` | call arguments | **result** |
| `odoo_write` | call arguments | **call arguments** (result is `true`) |
| `odoo_message_post` | call arguments | **call arguments** (result is the new `mail.message` id) |

Read-only tools (`odoo_search_read`, `odoo_read`) never produce a link.

## Odoo 19 notes

Written against 19.0 and tested on `odoo:19.0-20260926`. Things that changed from
18 and bit during development:

- `res.groups.category_id` is gone — groups now carry `privilege_id` pointing at
  a `res.groups.privilege`, which itself points at the `ir.module.category`.
- `res.groups.users` is now `user_ids`.
- `_sql_constraints` is no longer supported; use `models.Constraint`.
- `@route(type="json")` is a deprecated alias for `type="jsonrpc"`.
- Search views reject `<group string=...>` and `expand=...`; use
  `<group name="group_by">`.
- `t-esc` is deprecated in favour of `t-out`.

## Development

```sh
docker compose logs -f odoo
docker compose exec odoo odoo --config=/etc/odoo/odoo.conf \
    -d turnstone -u turnstone_integration --stop-after-init    # reload module
docker compose exec odoo odoo shell --config=/etc/odoo/odoo.conf -d turnstone
```

The container runs with `--dev=reload,qweb,xml`, so Python and template edits
reload without a restart; asset and model changes need the `-u` above.

The compose file is **development only** — it binds a host port, uses a trivial
database password and mounts the addon read-write.

## Status

Exercised end to end against a live Turnstone **and driven in a real browser**,
including:

- chat with an agent that ran real cluster tools and answered
- adopting a coordinator started **outside** Odoo, watching it spawn two
  subagents, approving its tool call from Odoo, and reading the final answer
- Live Activity listing 19 workstreams across the cluster, correctly marking
  which ones Odoo already knows
- the permission matrix above, as a non-manager user
- in the browser: Markdown tables, lists, bold and inline code rendering from a
  real agent reply; live status moving Working -> Ready in both the header and
  the sidebar with no interaction; collapsed tool results; and the linked-record
  chip opening `res.partner` with the breadcrumb intact

Not yet done:
- The agent-writes-to-Odoo round trip is tested against synthetic transcripts
  rather than a live Odoo MCP server pointed at this instance — the cluster
  cannot reach a laptop.
- Polling, not streaming. Turnstone exposes SSE; this polls while an agent works.
- No automated test suite; the verification above was driven by hand.
- Polling rather than streaming (see above).
