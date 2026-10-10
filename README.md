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
- **Agents** — saved configurations (server, kind, skill, persona, model, judge
  model, approval policy) that users start by name, so nobody assembles one by
  hand and every session of a kind is comparable.
- **Per-agent access control** — which Odoo groups may start which agent. This
  is the module's RBAC gate.
- **Audit log** — append-only record of what each Odoo user did here.
- **File attachments** — images, PDF and text/CSV/JSON, on coordinator sessions.
- **Multiple servers** — connect to more than one Turnstone, with per-connection
  group restrictions.

## Agents and access

An administrator defines an agent once under **Turnstone → Agents**; users pick
it from the chat. The alternative — assembling server/kind/skill/persona/model
per session — guarantees nobody does it consistently, and gets the kind/skill
pairing wrong in a way that fails silently.

`group_ids` on the agent decides who may start it. Empty means any Turnstone
User. It only ever narrows: Turnstone User is still required, and the
connection's own restriction applies as well. Agents a user may not start are
not listed, and calling the endpoint directly is refused and logged as
`access_denied`.

## Audit

Turnstone keeps its own audit of tool calls and verdicts, but it only ever sees
one identity — the server API token. "Which of our people approved that restart"
is a question only this log can answer, which is why it exists.

Append-only by construction: `write` and `unlink` both raise, no view offers
either, and managers get no write bit in the ACLs. Users see their own trail;
managers see everyone's.

Logged: session create and adopt, messages, tool approvals and denials, stop,
retry, rewind, close, attachments added and removed, and refused access.

## Files

Verified against Turnstone 1.8.3, by uploading each type:

| type | result |
|---|---|
| `image/*` | accepted, `kind: image` |
| `application/pdf` | accepted, `kind: pdf` |
| `text/plain`, `text/csv`, `application/json` | accepted, `kind: text` |
| `.xlsx`, `.docx` | **rejected** — "Unsupported file type" |

Two constraints worth knowing before designing around this:

- **Attachments are coordinator-only.** Uploading to an interactive session
  answers `{"error": "coordinator not found"}`. The chat only shows the attach
  control when the session can actually take one.
- **Office formats are not supported.** Spreadsheets and Word documents have to
  be converted before they reach Turnstone — see "A file-handling MCP" below.

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

**The console/route split applies to session CREATION too.** Turnstone states it
outright: *"coordinator workstreams must be created on the console via
POST /v1/api/workstreams/new"*. So coordinators go to the console, which only
ever makes coordinators and takes no `kind`; interactive sessions go to
`POST /v1/api/route/workstreams/new`, the only surface that accepts `kind`. The
console create endpoint takes no `model`, so a coordinator runs on the node
default — its children can still be given one.

**The new-session form asks which kind you want**, defaulting to the chosen
skill's own kind. This is not a nicety: ask an interactive session to fan out
and it has no `spawn_*` tools, so it explains that it cannot delegate and
quietly does the work in a single session instead. That looks like success,
which is worse than an error.

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

**A subagent is a session you can walk into and back out of.** The chat keeps a
navigation stack, so opening a subagent shows a back link to where you came
from; picking something from the sidebar instead clears it, because that is a
jump rather than a descent.

The sidebar nests subagents under their parent and keeps them there whatever
their state. Both halves matter: a coordinator closes its children when it
finishes, so a plain "state != closed" list drops the very session you are
looking at — you land on a conversation that appears nowhere — and it would also
make the tree flicker into existence only while you happened to be standing on a
child.

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

## A file-handling MCP (not built)

Turnstone natively handles the formats a model can read directly — images, PDF,
plain text. It does not handle the formats a business actually mails around:
xlsx, docx, and anything else needing extraction before a model can reason over
it.

The gap is narrower than "file handling" in general, which matters for scoping.
What is missing is a conversion and inspection surface:

- sniff a real type rather than trusting the extension
- xlsx/csv to structured rows, with sheet and range selection
- docx/pptx to text
- PDF text extraction where the PDF is not already text (scanned pages)
- bounded size and page limits, because an agent will happily ask for all of it

The honest shape is a small MCP server beside `kube-mcp`, registered with
Turnstone the same way, reachable only from Turnstone pods, with each conversion
a separately-named tool so tool policy can gate them — the same reasoning as
`docs/SRE.md` in the kube fork. It is deliberately NOT built here: it is a
deployment in the GitOps repo, not an Odoo module.

Until it exists, converting an xlsx to CSV before attaching it works today and
needs nothing new.

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
- `res.users.groups_id` is now `group_ids`. This one bites late: expressions like
  `allowed and (user.groups_id & allowed)` short-circuit while no record has
  restrictions set, so the rename stays invisible until the first group is
  assigned.
- XML attributes need values — `<input multiple>` must be `multiple="multiple"`.

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
- in the browser: walking parent -> subagent -> back -> other subagent -> back,
  with the sidebar tree stable throughout and the panel buttons correctly
  reading "open" for an attached subagent and "attach" for a new one

Not yet done:
- The agent-writes-to-Odoo round trip is tested against synthetic transcripts
  rather than a live Odoo MCP server pointed at this instance — the cluster
  cannot reach a laptop.
- Polling, not streaming. Turnstone exposes SSE; this polls while an agent works.
- No automated test suite; the verification above was driven by hand.
- Polling rather than streaming (see above).
