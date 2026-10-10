# odoo_turnstone_integration

An Odoo 19 module that puts a Turnstone agent chat inside Odoo, and links back
to whatever the agent touched.

Turnstone is the agentic harness — it runs the agent loop, holds durable session
state, and gates every tool call behind a policy. **This module runs no agent
logic of its own.** It is the UI and UX layer, and every decision is made by
asking Turnstone over its HTTP API.

## What it gives you

- **Agent Chat** — an OWL client action for talking to Turnstone agents.
- **Tool approvals in-line** — an agent that stops for a human stops here, with
  the tool name, its arguments and any judge verdict shown. Approve or deny
  without leaving Odoo.
- **Linked records** — when an agent writes to Odoo through the Odoo MCP server,
  the session shows what it created or changed and links straight to it.
- **Sessions as Odoo records** — searchable, groupable, access-controlled like
  anything else.
- **Scheduled jobs** — read-only view of Turnstone's cron tasks and run history.
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
| Turnstone / **User** | Chat, and see **only their own** sessions |
| Turnstone / **Administrator** | Configure servers, hold API tokens, see **every** session |

Administrator is a real privilege, not just a config role: a transcript contains
whatever the agent read, and the agent reads with the **server token's**
authority rather than the chatting user's.

Per-connection `allowed_group_ids` narrows access further. It can only narrow —
Turnstone User is still required.

Verified against a non-manager user: cannot see others' sessions, cannot read
`api_token`, cannot write connections, and cannot drive another user's session
through the controllers.

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

Working and exercised end to end against a live Turnstone: bootstrap, catalog,
session create, send, transcript mirroring, and an agent reply that ran real
tools.

Not yet done:
- The agent-writes-to-Odoo round trip is tested against synthetic transcripts
  rather than a live Odoo MCP server pointed at this instance.
- Polling, not streaming. Turnstone exposes SSE; this polls while an agent works.
- No tests directory yet.
