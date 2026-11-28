/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useRef, useState, useEffect } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { user } from "@web/core/user";
import { browser } from "@web/core/browser/browser";
import { renderMarkdown } from "@turnstone_integration/js/markdown";

/**
 * Where the chat remembers what this user was looking at.
 *
 * Keyed by user id, and the user id is stored inside the value too: Odoo does
 * not clear localStorage on logout, so on a shared browser the next person to
 * sign in must not inherit the previous one's conversation. Per-user keys are
 * what makes that impossible, and the uid in the payload catches a store that
 * was copied or hand-edited into another user's key.
 */
const STORAGE_VERSION = 1;

const storageKey = (uid) =>
    uid ? `turnstone_integration.chat.v${STORAGE_VERSION}.uid:${uid}` : null;

function positiveInt(value) {
    const n = Number(value);
    return Number.isInteger(n) && n > 0 ? n : null;
}

/** Read the remembered session for `uid`, or null. Never throws. */
function readStoredSession(uid, storage) {
    const key = storageKey(uid);
    if (!key || !storage) {
        return null;
    }
    try {
        const raw = storage.getItem(key);
        if (!raw) {
            return null;
        }
        const data = JSON.parse(raw);
        if (!data || Number(data.uid) !== Number(uid)) {
            return null;
        }
        const sessionId = positiveInt(data.session_id);
        if (!sessionId) {
            return null;
        }
        return {
            sessionId,
            agentId: positiveInt(data.agent_id),
            agentName: typeof data.agent_name === "string" ? data.agent_name : null,
            connectionId: positiveInt(data.connection_id),
        };
    } catch {
        // Corrupt JSON, or storage disabled: remembering a session is a
        // convenience and must never be the reason the chat fails to open.
        return null;
    }
}

/** Write (or, with a null value, clear) the remembered session. Never throws. */
function writeStoredSession(uid, storage, value) {
    const key = storageKey(uid);
    if (!key || !storage) {
        return;
    }
    try {
        if (!value) {
            storage.removeItem(key);
            return;
        }
        storage.setItem(
            key,
            JSON.stringify({
                uid,
                version: STORAGE_VERSION,
                session_id: value.sessionId,
                agent_id: value.agentId ?? null,
                agent_name: value.agentName ?? null,
                connection_id: value.connectionId ?? null,
            })
        );
    } catch {
        // Private mode or a full quota. Losing the pointer costs one empty
        // picker; throwing here would cost the whole chat.
    }
}

/**
 * What to tell the user when a send does not complete.
 *
 * `rpc()` does not return a body for a call that never finished: it *rejects*,
 * with a `ConnectionLostError` (dropped connection, non-JSON answer) or an
 * `RPCError` (a JSON-RPC error body: an uncaught server exception, an expired
 * session). Both carry something worth showing, so use the most specific part
 * each one has rather than one vague sentence for every failure.
 *
 * Deliberately silent about where the text went: `failedSend` knows whether it
 * went back into the open composer or was held for another session, and says so.
 */
function sendFailureMessage(error) {
    if (error && error.url) {
        // ConnectionLostError: no response at all, which does not say whether
        // the server acted on the request, so do not claim it did or did not.
        return _t("The connection to the server was lost, so this message may not have been sent.");
    }
    // RPCError: `data.message` is the server's own text ("Session expired");
    // `message` is the JSON-RPC short form. `data.debug` is a full traceback
    // and must never be shown to a user.
    const detail = (error && error.data && error.data.message) || (error && error.message);
    return detail
        ? _t("The message could not be sent (%s).", detail)
        : _t("The message could not be sent.");
}

/**
 * Turnstone chat client.
 *
 * Talks only to this module's Odoo controllers, never to Turnstone directly —
 * the API token stays server-side (see controllers/main.py).
 *
 * State lives in Turnstone, not here. Every action round-trips and replaces the
 * local session object with whatever the server returned, so a reply that was
 * rewound or a tool that was denied shows up correctly rather than being merged
 * into a stale local copy.
 */
export class TurnstoneChat extends Component {
    static template = "turnstone_integration.TurnstoneChat";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");
        this.action = useService("action");

        this.state = useState({
            loading: true,
            error: null,
            connections: [],
            sessions: [],
            session: null,
            draft: "",
            sending: false,
            showNewForm: false,
            catalog: { skills: [], personas: [], models: [] },
            newSession: { connection_id: null, name: "", skill: "", persona: "", kind: "interactive", model: "" },
            showTools: true,
            children: [],
            showChildren: false,
            expandedTools: {},
            agents: [],
            mode: "agent",          // "agent" picker, or "custom" full form
            attachments: [],
            uploading: false,
            // Names the action currently in flight, so its button can lock and
            // spin. Without this a slow approve invites a second click, and the
            // second one carries a cycle_id the server has already consumed.
            busy: null,
            adoptWsId: "",
            isManager: false,
        });

        // Rendered markdown is cached per message id + body so a poll that
        // changes nothing does not re-parse the whole transcript every 2s.
        this._mdCache = new Map();

        // localStorage is the only place this survives a reload, and it is
        // namespaced by the signed-in user (see storageKey).
        this.storage = browser.localStorage;
        this.uid = user.userId;
        // Mirror of what was last written, so polling the same session does
        // not rewrite storage on every tick.
        this._stored = null;

        this.threadRef = useRef("thread");
        this.pollHandle = null;
        this.pollInterval = 2000;   // while a turn is in flight
        this.idleInterval = 6000;   // while idle, so external changes still show
        this.inFlight = false;
        this.consecutiveFailures = 0;
        this.hidden = false;
        // Where "back" goes, so descending into a subagent and returning is one
        // click rather than hunting the parent in the sidebar.
        this.navStack = [];
        // Failed sends whose session was no longer open when the failure landed
        // (see restoreDraft / pickUpPendingDraft).
        this._pendingDrafts = new Map();

        // A background tab should not keep polling, but coming back to it
        // should feel instant rather than waiting out the next interval.
        this.onVisibility = () => {
            this.hidden = document.hidden;
            if (this.hidden) {
                this.stopPolling();
            } else {
                this.stopPolling();
                this.tick();
            }
        };
        document.addEventListener("visibilitychange", this.onVisibility);

        onWillStart(async () => {
            await this.bootstrap();
            // Keep the sidebar live even before a session is opened, so a job
            // that starts elsewhere shows up without a manual reload.
            this.maybePoll();
            // An explicit request wins (the Sessions list, an agent form): the
            // user is jumping somewhere on purpose. Otherwise reopen what this
            // user was last looking at, so Agent Chat does not greet them with
            // an empty screen and a pile of identically named rows.
            const requested = this.props.action?.params?.session_id;
            if (requested) {
                await this.openSession(requested);
            } else {
                await this.restoreSession();
            }
        });

        onWillUnmount(() => {
            this.stopPolling();
            document.removeEventListener("visibilitychange", this.onVisibility);
        });

        // Keep the newest turn in view, but only when the transcript actually
        // grew — otherwise every poll yanks the scroll position back down while
        // someone is reading earlier output.
        // Scroll on new turns AND when the last turn grows — an agent's final
        // message arrives in one poll as a large block, which would otherwise
        // land below the fold.
        useEffect(
            (count, lastLen, state) => {
                const sig = `${count}:${lastLen}:${state}`;
                if (sig !== this._lastSig) {
                    this._lastSig = sig;
                    this.scrollToBottom();
                }
            },
            () => {
                const msgs = this.state.session?.messages ?? [];
                return [
                    msgs.length,
                    msgs.length ? (msgs[msgs.length - 1].body || "").length : 0,
                    this.state.session?.state ?? "",
                ];
            }
        );
    }

    // -- data ----------------------------------------------------------------

    async bootstrap() {
        const res = await rpc("/turnstone/bootstrap", {});
        if (res.error) {
            this.state.error = res.error;
            this.state.loading = false;
            return;
        }
        this.state.connections = res.connections || [];
        this.state.agents = res.agents || [];
        // With no saved agents there is nothing to pick, so go straight to the
        // full form rather than showing an empty chooser.
        this.state.mode = this.state.agents.length ? "agent" : "custom";
        this.state.sessions = res.sessions || [];
        this.state.showTools = res.show_tool_calls !== false;
        this.state.isManager = !!res.is_manager;
        this.idleInterval = Math.max(this.pollInterval * 3, 5000);
        this.pollInterval = res.poll_interval || 2000;
        this.state.newSession.connection_id =
            (this.state.connections.find((c) => c.is_default) || this.state.connections[0])?.id ??
            null;
        this.state.loading = false;
    }

    async openSession(sessionId, { push = false, silent = false } = {}) {
        if (push && this.state.session && this.state.session.id !== sessionId) {
            this.navStack.push({ id: this.state.session.id, name: this.state.session.name });
        }
        this.stopPolling();
        const res = await rpc("/turnstone/session/get", { session_id: sessionId, sync: true });
        if (res.error) {
            if (silent) {
                // A restore the user did not ask for must not toast: the
                // caller decides what to do next.
                return false;
            }
            this.fail(res.error);
            return false;
        }
        this.state.session = res.session;
        this.loadChildren();
        await this.loadAttachments();
        this.maybePoll();
        this.rememberSession();
        this.pickUpPendingDraft(this.state.session.id);
        return true;
    }

    // -- what this browser remembers --------------------------------------

    /**
     * Remember the open session, so reopening Agent Chat (a reload, or the
     * menu) lands on the same conversation instead of a blank screen.
     *
     * The configured agent is stored next to the session because it is the
     * fallback key: every session of one agent is filed under that agent's
     * name (see `session_new_from_agent`), so once the stored row is gone the
     * conversation the user meant is the newest session of that same agent.
     */
    rememberSession() {
        const s = this.state.session;
        if (!s) {
            return;
        }
        const stored = {
            sessionId: s.id,
            agentId: positiveInt(s.agent_id),
            agentName: s.agent_name || null,
            connectionId: positiveInt(s.connection_id),
        };
        const previous = this._stored;
        if (
            previous &&
            previous.sessionId === stored.sessionId &&
            previous.agentId === stored.agentId &&
            previous.agentName === stored.agentName &&
            previous.connectionId === stored.connectionId
        ) {
            return; // polling this same session again: nothing to write
        }
        this._stored = stored;
        writeStoredSession(this.uid, this.storage, stored);
    }

    /** Forget the remembered session — there is no longer one to reopen. */
    forgetSession() {
        this._stored = null;
        writeStoredSession(this.uid, this.storage, null);
    }

    /**
     * Reopen what this user was last looking at, on mount.
     *
     * Resolution order, worst case last:
     *   1. the stored session, if the server still has it (a closed session
     *      too — it keeps its transcript, it is only hidden from the sidebar);
     *   2. the newest listed session with the remembered agent;
     *   3. the newest listed session at all;
     *   4. the empty state.
     *
     * The stored id is dropped in favour of whatever step 2 finds, so the next
     * reload starts from there. Nothing on this path toasts or reports an
     * error: a stored id that no longer exists is expected (the row was
     * deleted, or the server's mirror was rebuilt), not something the user has
     * to dismiss before they can chat. A transient failure lands them on the
     * newest session of the same agent rather than an empty screen, which is
     * the better of the two — the pointer is a convenience, not data.
     */
    async restoreSession() {
        if (this.state.error) {
            return; // bootstrap already failed and said so; do not pile on
        }
        const stored = readStoredSession(this.uid, this.storage);
        if (!stored) {
            return;
        }
        this._stored = stored;
        if (await this.openSession(stored.sessionId, { silent: true })) {
            return;
        }
        const fallback = this.fallbackSession(stored);
        if (!fallback) {
            this.forgetSession();
            return;
        }
        // Opening the fallback remembers it, replacing the pointer that failed.
        if (!(await this.openSession(fallback.id, { silent: true }))) {
            this.forgetSession();
        }
    }

    /**
     * The session to reopen when the remembered one is gone.
     *
     * Candidates come only from this user's listing, which the server already
     * limits by record rules: nothing here can reach another user's session.
     * Subagents are skipped — the row nested under its parent is not what
     * "reopen my chat" means — and a session with a workstream beats a draft,
     * because a draft has no conversation to restore.
     */
    fallbackSession(stored) {
        const rows = (this.state.sessions || []).filter((s) => !s.parent_ws_id);
        const named = stored.agentName || stored.agentId
            ? rows.filter(
                  (s) =>
                      (stored.agentId && s.agent_id === stored.agentId) ||
                      (stored.agentName && s.name === stored.agentName)
              )
            : [];
        const pool = named.length ? named : rows;
        return pool.find((s) => s.ws_id) || pool[0] || null;
    }

    /**
     * Subagents, read off the synced session rather than fetched separately.
     *
     * They are persisted server-side during sync precisely because Turnstone's
     * live feed drops a child once it finishes — fetching live here would make
     * the panel count up and then empty itself.
     */
    loadChildren() {
        const s = this.state.session;
        this.state.children = (s && s.subagents) || [];
        if (this.state.children.length) {
            this.state.showChildren = true;
        }
    }

    /**
     * Keep the view live for as long as it is open.
     *
     * Polling only while the agent is "thinking" is not enough: an approval can
     * land, a subagent can finish, a scheduled job can move, or someone can
     * steer the same session from the Turnstone console — all while this session
     * reads as idle. So the loop runs continuously and only changes pace.
     *
     * Turnstone does expose SSE, but the API token must stay server-side, so
     * streaming would mean holding an Odoo worker open per viewer. Adaptive
     * polling through the existing proxy is the honest trade here.
     */
    pollDelay() {
        const s = this.state.session;
        if (!s) {
            return this.idleInterval;
        }
        if (s.state === "thinking") {
            return this.pollInterval;        // mid-turn: keep up with output
        }
        if (s.state === "attention") {
            return this.pollInterval * 2;    // someone is being waited on
        }
        if (s.state === "closed") {
            return 0;                        // nothing more will happen
        }
        return this.idleInterval;            // idle, but still watch for changes
    }

    maybePoll() {
        this.stopPolling();
        if (this.hidden) {
            return;  // resumed by the visibilitychange handler
        }
        const delay = this.pollDelay();
        if (!delay) {
            return;
        }
        this.pollHandle = setTimeout(() => this.tick(), delay);
    }

    async tick() {
        if (this.hidden) {
            return this.maybePoll();
        }
        if (!this.state.session) {
            // No session open: keep the sidebar current, so work started
            // elsewhere (a schedule, another tab) appears on its own.
            await this.refreshSidebar();
            // The remembered session is usually already open. When it is not,
            // it is because a restore found nothing and the empty state was
            // shown deliberately — so do not keep retrying it on every tick.
            return this.maybePoll();
        }
        if (this.inFlight) {
            // A slow sync must not stack requests behind itself.
            return this.maybePoll();
        }
        this.inFlight = true;
        try {
            const res = await rpc("/turnstone/session/get", {
                session_id: this.state.session.id,
                sync: true,
            });
            if (res.error) {
                this.consecutiveFailures++;
                // Transient server or network trouble should not clear the
                // conversation or spam a toast on every tick.
                if (this.consecutiveFailures >= 3) {
                    this.fail(res.error);
                    this.consecutiveFailures = 0;
                }
                return;
            }
            this.consecutiveFailures = 0;
            this.state.session = res.session;
            this.loadChildren();
            this.rememberSession();
            await this.refreshSidebar();
        } finally {
            this.inFlight = false;
            this.maybePoll();
        }
    }

    stopPolling() {
        if (this.pollHandle) {
            clearTimeout(this.pollHandle);
            this.pollHandle = null;
        }
    }

    async refreshSidebar() {
        const res = await rpc("/turnstone/bootstrap", {
            current_session_id: this.state.session?.id,
        });
        if (!res.error) {
            this.state.sessions = res.sessions || [];
        }
    }

    /**
     * Sidebar rows, with subagents nested under the parent they belong to.
     *
     * A subagent opened from the panel is a session like any other, but showing
     * it as a flat peer of its own parent loses the only thing that explains
     * why it exists.
     */
    get sessionTree() {
        const rows = this.state.sessions || [];
        const byWs = new Map(rows.filter((r) => r.ws_id).map((r) => [r.ws_id, r]));
        const kids = new Map();
        const roots = [];
        for (const r of rows) {
            if (r.parent_ws_id && byWs.has(r.parent_ws_id)) {
                if (!kids.has(r.parent_ws_id)) {
                    kids.set(r.parent_ws_id, []);
                }
                kids.get(r.parent_ws_id).push(r);
            } else {
                roots.push(r);
            }
        }
        const out = [];
        const walk = (row, depth) => {
            out.push({ ...row, depth });
            for (const k of kids.get(row.ws_id) || []) {
                walk(k, depth + 1);
            }
        };
        roots.forEach((r) => walk(r, 0));
        return out;
    }

    /** Choosing from the sidebar is a jump, not a descent. */
    async pickSession(sessionId) {
        this.navStack = [];
        await this.openSession(sessionId);
    }

    /** Back to the session we came from, or to this one's parent. */
    async goBack() {
        const target = this.backTarget;
        if (!target) {
            return;
        }
        this.navStack.pop();
        await this.openSession(target.id, { push: false });
    }

    get backTarget() {
        if (this.navStack.length) {
            return this.navStack[this.navStack.length - 1];
        }
        const s = this.state.session;
        if (s && s.parent_session_id) {
            return { id: s.parent_session_id, name: s.parent_name };
        }
        return null;
    }

    /**
     * Run an action with its button locked.
     *
     * Every one of these round-trips to Turnstone and can take seconds. A
     * button that still looks clickable during that time WILL be clicked again,
     * and for approvals the second click carries a spent cycle_id and comes
     * back HTTP 409 "stale cycle_id" — an alarming error for something that
     * actually worked the first time.
     */
    async withBusy(name, fn) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = name;
        try {
            return await fn();
        } finally {
            this.state.busy = null;
        }
    }

    fail(message) {
        this.state.sending = false;
        this.stopPolling();
        this.notification.add(message, { type: "danger", title: _t("Turnstone") });
    }

    // -- actions -------------------------------------------------------------

    async toggleNewForm() {
        this.state.showNewForm = !this.state.showNewForm;
        if (this.state.showNewForm && this.state.newSession.connection_id) {
            const res = await rpc("/turnstone/catalog", {
                connection_id: this.state.newSession.connection_id,
            });
            if (res.error) {
                return this.fail(res.error);
            }
            this.state.catalog = {
                skills: res.skills || [],
                personas: res.personas || [],
                models: res.models || [],
            };
        }
    }

    /**
     * Follow the skill's own kind when one is picked.
     *
     * Turnstone tags every skill interactive or coordinator, and getting this
     * wrong does not fail loudly: ask an interactive session to fan out and it
     * has no spawn_* tools, so it explains that it cannot delegate and quietly
     * does the work itself in one session. That is a worse outcome than an
     * error, because it looks like it worked.
     */
    /**
     * Skills Turnstone will actually expose to the selected kind.
     *
     * Offering a coordinator skill to an interactive session is how you get a
     * silent failure, so it is simply not in the list.
     */
    get skillChoices() {
        const kind = this.state.newSession.kind;
        return (this.state.catalog.skills || []).filter(
            (s) => !s.kind || s.kind === "any" || s.kind === kind
        );
    }

    get personaChoices() {
        const kind = this.state.newSession.kind;
        return (this.state.catalog.personas || []).filter(
            (p) => !p.applies_to_kinds || p.applies_to_kinds.includes(kind)
        );
    }

    /** Clear a skill that the new kind would not accept. */
    onKindChange() {
        if (!this.skillChoices.some((s) => s.name === this.state.newSession.skill)) {
            this.state.newSession.skill = "";
        }
        if (!this.personaChoices.some((p) => p.name === this.state.newSession.persona)) {
            this.state.newSession.persona = "";
        }
        if (this.state.newSession.kind === "coordinator") {
            this.state.newSession.model = "";
        }
    }

    onSkillChange() {
        const skill = this.state.catalog.skills.find(
            (s) => s.name === this.state.newSession.skill
        );
        if (skill && (skill.kind === "coordinator" || skill.kind === "interactive")) {
            this.state.newSession.kind = skill.kind;
        }
    }

    /** Start from a saved agent — the path most users should take. */
    async startAgent(agent) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = "agent:" + agent.id;
        let res;
        try {
            res = await rpc("/turnstone/session/new_from_agent", { agent_id: agent.id });
        } finally {
            this.state.busy = null;
        }
        if (res.error) {
            return this.fail(res.error);
        }
        this.navStack = [];
        this.state.session = res.session;
        this.state.sessions = res.sessions || this.state.sessions;
        this.state.showNewForm = false;
        this.loadChildren();
        await this.loadAttachments();
        this.maybePoll();
        this.rememberSession();
    }

    async loadAttachments() {
        const s = this.state.session;
        if (!s || !s.supports_attachments || !s.ws_id) {
            this.state.attachments = [];
            return;
        }
        const res = await rpc("/turnstone/session/attachments", { session_id: s.id });
        this.state.attachments = res.error ? [] : res.attachments || [];
    }

    /**
     * Attach a file.
     *
     * Turnstone takes images, PDFs and text/JSON/CSV. It rejects xlsx and docx
     * outright, so those are caught here with something actionable rather than
     * a raw "Unsupported file type".
     */
    async onFilesPicked(ev) {
        const files = [...(ev.target.files || [])];
        ev.target.value = "";
        if (!files.length || !this.state.session) {
            return;
        }
        this.state.uploading = true;
        try {
            for (const file of files) {
                const b64 = await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(String(reader.result).split(",")[1]);
                    reader.onerror = reject;
                    reader.readAsDataURL(file);
                });
                const res = await rpc("/turnstone/session/attachment/add", {
                    session_id: this.state.session.id,
                    filename: file.name,
                    content: b64,
                    mime_type: file.type || undefined,
                });
                if (res.error) {
                    this.fail(res.error);
                    continue;
                }
                this.state.attachments = res.attachments || [];
            }
        } finally {
            this.state.uploading = false;
        }
    }

    async removeAttachment(id) {
        const res = await rpc("/turnstone/session/attachment/remove", {
            session_id: this.state.session.id,
            attachment_id: id,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.attachments = res.attachments || [];
    }

    async createSession() {
        const n = this.state.newSession;
        const res = await rpc("/turnstone/session/new", {
            connection_id: n.connection_id,
            name: n.name || undefined,
            skill: n.skill || undefined,
            persona: n.persona || undefined,
            model: n.model || undefined,
            kind: n.kind,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = res.session;
        this.state.sessions = res.sessions || this.state.sessions;
        this.state.showNewForm = false;
        this.state.newSession.name = "";
        this.rememberSession();
    }

    /**
     * Put a message that did not reach Turnstone back in the composer.
     *
     * The text was cleared before the call, so a send that fails in any way has
     * to give it back or the user's turn exists nowhere — it was never sent and
     * they cannot see it any more.
     *
     * Two cases must not be clobbered by that restore:
     *  - the session changed while the call was in flight (a poll cannot do it,
     *    but navigating or the sidebar can): the text belongs to the
     *    conversation it was typed in, and putting it into another one is a
     *    message to the wrong agent. It is held for that session instead —
     *    `pickUpPendingDraft()` gives it back when the user returns, so the
     *    text is not lost either way;
     *  - the user typed something new in the meantime, which the send did not
     *    block: that text is at least as valuable, so the failed message is put
     *    back *in front of* it — oldest first, as in the transcript — rather
     *    than replacing it.
     */
    restoreDraft(sessionId, body) {
        const s = this.state.session;
        if (!s || s.id !== sessionId) {
            // Hold it against the conversation it was typed in. Bounded on
            // purpose: only a failed send lands here, and each one replaces its
            // session's entry, so this can never hold more than one message per
            // session that has an unsent one.
            this._pendingDrafts.set(sessionId, body);
            return false;
        }
        const typed = this.state.draft;
        this.state.draft = typed.trim() ? `${body}\n\n${typed}` : body;
        return true;
    }

    /**
     * Give back a message whose send failed while another session was open.
     *
     * Opening is the moment the user is looking at that conversation again, so
     * it is the moment the text belongs back in front of them. Merged with
     * whatever else is in the box rather than replacing it, same as above.
     */
    pickUpPendingDraft(sessionId) {
        const pending = this._pendingDrafts.get(sessionId);
        if (!pending) {
            return;
        }
        this._pendingDrafts.delete(sessionId);
        const typed = this.state.draft;
        this.state.draft = typed.trim() ? `${pending}\n\n${typed}` : pending;
    }

    /**
     * A send did not complete: give the text back, say so, and keep watching.
     *
     * `fail()` stops the poll loop, which is the right default for an action
     * that failed outright — retrying it on every interval would just repeat
     * the toast. A send is the exception: the turn may still have reached
     * Turnstone even though the answer did not come back (the investigation
     * recorded a transaction retry that POSTed the message and then failed), so
     * leaving the loop stopped hides the agent's reply until the user happens to
     * click something. Restart it here, at the same interval `maybePoll()`
     * already picks — the detection cadence is unchanged.
     */
    failedSend(sessionId, body, message) {
        const restored = this.restoreDraft(sessionId, body);
        // Say where the text went: a sentence that claims "back in the box" when
        // it was held for another session is worse than saying nothing.
        this.fail(
            restored
                ? `${message} ${_t("Your text is back in the box.")}`
                : `${message} ${_t(
                      "Your text is kept with the conversation you sent it from, and comes " +
                          "back when you reopen that session."
                  )}`
        );
        // fail() stops the loop; a send may have reached Turnstone anyway, so
        // keep watching for the reply.
        this.maybePoll();
    }

    async send() {
        const body = this.state.draft.trim();
        if (!body || this.state.sending || !this.state.session) {
            return;
        }
        // Remember which conversation the text belongs to: everything below
        // can find a different session in state.session.
        const sessionId = this.state.session.id;
        this.state.sending = true;
        this.state.draft = "";
        let res;
        try {
            res = await rpc("/turnstone/session/send", {
                session_id: sessionId,
                body,
            });
        } catch (error) {
            // The call never completed: a dropped connection, an HTTP 500, an
            // expired session, or a transaction retry that ran out. There is no
            // `res.error` on this path, so restoring the draft only in the
            // branch below would silently eat what the user typed.
            return this.failedSend(sessionId, body, sendFailureMessage(error));
        } finally {
            // Also reached when the RPC threw, so a failed send cannot leave the
            // composer stuck in its sending state.
            this.state.sending = false;
        }
        if (res.error) {
            // Give the text back rather than losing it.
            return this.failedSend(sessionId, body, res.error);
        }
        this.state.session = res.session;
        this.state.sessions = res.sessions || this.state.sessions;
        this.rememberSession();
        this.maybePoll();
    }

    onKeydown(ev) {
        // Enter sends, Shift+Enter newlines — the convention everywhere else.
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.send();
        }
    }

    async resolveApproval(approved) {
        if (!this.state.session) {
            return;
        }
        await this.withBusy(approved ? "approve" : "deny", async () => {
            const res = await rpc("/turnstone/session/approve", {
                session_id: this.state.session.id,
                approved,
            });
            if (res.error) {
                return this.fail(res.error);
            }
            this.state.session = res.session;
            this.maybePoll();
        });
    }

    async cancelGeneration() {
        await this.withBusy("stop", async () => {
            const res = await rpc("/turnstone/session/cancel", {
                session_id: this.state.session.id,
            });
            if (res.error) {
                return this.fail(res.error);
            }
            this.state.session = res.session;
            this.stopPolling();
        });
    }

    /**
     * Close the conversation. It stays in the sessions list (closed sessions
     * are filtered out of the sidebar only), so this is not "start the next
     * one": nothing is left open, and nothing should be reopened on reload.
     */
    async closeSession() {
        await this.withBusy("close", async () => {
        const res = await rpc("/turnstone/session/close", {
            session_id: this.state.session.id,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = null;
        this.state.sessions = res.sessions || [];
        this.stopPolling();
        this.forgetSession();
        });
    }

    async retry() {
        await this.withBusy("retry", async () => {
            const res = await rpc("/turnstone/session/retry", {
                session_id: this.state.session.id,
            });
            if (res.error) {
                return this.fail(res.error);
            }
            this.state.session = res.session;
            this.maybePoll();
        });
    }

    async rewind(turns = 1) {
        await this.withBusy("rewind", async () => {
            const res = await rpc("/turnstone/session/rewind", {
                session_id: this.state.session.id,
                turns,
            });
            if (res.error) {
                return this.fail(res.error);
            }
            this.state.session = res.session;
        });
    }

    /** Attach to a workstream started outside Odoo — a scheduled run, say. */
    async adopt(wsId) {
        const id = (wsId || this.state.adoptWsId || "").trim();
        if (!id) {
            return;
        }
        const res = await rpc("/turnstone/session/adopt", {
            connection_id:
                this.state.session?.connection_id || this.state.newSession.connection_id,
            ws_id: id,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = res.session;
        this.state.sessions = res.sessions || this.state.sessions;
        this.state.adoptWsId = "";
        this.loadChildren();
        this.maybePoll();
        this.rememberSession();
    }

    /** Open a subagent: adopt it if Odoo has never seen it, else just switch. */
    async openChild(child) {
        const from = this.state.session
            ? { id: this.state.session.id, name: this.state.session.name }
            : null;
        if (child.odoo_session_id) {
            await this.openSession(child.odoo_session_id, { push: true });
            return;
        }
        await this.adopt(child.ws_id);
        // adopt() replaces the session wholesale, so record the origin after it.
        if (from && this.state.session && this.state.session.id !== from.id) {
            this.navStack.push(from);
        }
    }

    /** Jump to an Odoo record the agent created or updated. */
    openLinkedRecord(link) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: link.res_model,
            res_id: link.res_id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    scrollToBottom() {
        const el = this.threadRef.el;
        if (el) {
            // After the patch lands, so the new node has a height.
            requestAnimationFrame(() => {
                el.scrollTop = el.scrollHeight;
            });
        }
    }

    // -- presentation --------------------------------------------------------

    /** Agent output is markdown; render it rather than making users read it raw. */
    renderBody(message) {
        const key = message.id + ":" + message.body.length;
        if (!this._mdCache.has(key)) {
            this._mdCache.set(key, renderMarkdown(message.body));
            // The cache is per-session-view and transcripts are rewritten
            // wholesale on rewind, so bound it rather than let it grow.
            if (this._mdCache.size > 400) {
                this._mdCache.delete(this._mdCache.keys().next().value);
            }
        }
        return this._mdCache.get(key);
    }

    toggleTool(messageId) {
        this.state.expandedTools[messageId] = !this.state.expandedTools[messageId];
    }

    /** First line of a tool result, for the collapsed summary. */
    toolSummary(body) {
        const first = (body || "").split("\n").find((l) => l.trim()) || "";
        return first.length > 110 ? first.slice(0, 110) + "\u2026" : first;
    }

    /**
     * Why the transcript below is not the whole conversation, or null when it is.
     *
     * The server already distinguishes the three states — complete, "kept a
     * partial transcript" (`sync_note`) and "the workstream is gone"
     * (`remote_gone`) — but they used to render identically: a short or empty
     * thread with no explanation, which reads as lost history. The distinction
     * is only useful if the person reading the thread is told it.
     *
     * Deliberately a line of text and not an alert or a dialog. The transcript
     * still renders and the composer still works; the notice explains what is
     * on screen, it does not ask anyone to acknowledge anything. It also stays
     * out of the way when there is nothing wrong — a healthy session returns
     * null, so a complete transcript renders exactly as it did before.
     *
     * `sync_note` is the server's own wording and is already translated there;
     * `title` is the client's own, so it is an `_t()` term like every other
     * label in this component.
     *
     * `title` is null when the text already says everything. The server's
     * remote-gone note is a complete sentence ("Turnstone no longer has this
     * session's workstream (ws-…): it was deleted or expired …"), so a heading
     * above it would only repeat it. The partial case is different: its note is
     * the evidence ("returned 200 of 200 rows, not as the beginning …"), and the
     * headline is the verdict that evidence supports.
     */
    get syncNotice() {
        const session = this.state.session;
        if (!session) {
            return null;
        }
        const note = (session.sync_note || "").trim();
        if (session.remote_gone) {
            // The 404 path writes a note as well, but `remote_gone` is the flag
            // that can outlive it (a row written before the note existed, a
            // field cleared by hand), and "the workstream is gone" must never
            // fall back to silence. So the flag decides, and the note is the
            // reason when there is one.
            //
            // A closed session keeps its transcript across the 404, and then the
            // server's note — which ends "so the transcript cannot be read any
            // more" — would sit above a transcript that is plainly readable.
            // Saying which copy this is resolves that: archived here, not lost.
            const kept = (session.messages || []).length > 0;
            const what = kept
                ? _t("The messages below are Odoo's own copy, kept when the server stopped reporting this session.")
                : _t("There is no transcript to show: nothing could be read back from the server.");
            return {
                kind: "gone",
                title: note ? null : _t("Turnstone no longer has this session"),
                text: note ? `${note} ${what}` : what,
            };
        }
        if (note) {
            // Covers both meanings the server gives this field: a response it
            // refused to trust (the stored transcript was kept) and one it
            // applied that may be a window over a longer conversation.
            return {
                kind: "partial",
                title: _t("This transcript may be incomplete"),
                text: note,
            };
        }
        return null;
    }

    stateBadge(state) {
        return {
            done: "text-bg-light",
            draft: "text-bg-secondary",
            idle: "text-bg-success",
            thinking: "text-bg-info",
            attention: "text-bg-warning",
            error: "text-bg-danger",
            closed: "text-bg-secondary",
        }[state] || "text-bg-secondary";
    }

    stateLabel(state) {
        return {
            draft: _t("Not started"),
            idle: _t("Ready"),
            thinking: _t("Working…"),
            attention: _t("Waiting for you"),
            error: _t("Error"),
            closed: _t("Closed"),
        }[state] || state;
    }

    operationLabel(op) {
        return { create: _t("Created"), write: _t("Updated"), message: _t("Posted to") }[op] || op;
    }

    get contextPercent() {
        return Math.round((this.state.session?.context_ratio || 0) * 100);
    }
}

registry.category("actions").add("turnstone_integration.chat", TurnstoneChat);
