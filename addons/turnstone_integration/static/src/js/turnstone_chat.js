/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useRef, useState, useEffect } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { renderMarkdown } from "@turnstone_integration/js/markdown";

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
            adoptWsId: "",
            isManager: false,
        });

        // Rendered markdown is cached per message id + body so a poll that
        // changes nothing does not re-parse the whole transcript every 2s.
        this._mdCache = new Map();

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
            const requested = this.props.action?.params?.session_id;
            if (requested) {
                await this.openSession(requested);
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

    async openSession(sessionId, { push = false } = {}) {
        if (push && this.state.session && this.state.session.id !== sessionId) {
            this.navStack.push({ id: this.state.session.id, name: this.state.session.name });
        }
        this.stopPolling();
        const res = await rpc("/turnstone/session/get", { session_id: sessionId, sync: true });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = res.session;
        this.loadChildren();
        await this.loadAttachments();
        this.maybePoll();
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
            // No session open: still keep the sidebar current, so work started
            // elsewhere (a schedule, another tab) appears on its own.
            await this.refreshSidebar();
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
        const res = await rpc("/turnstone/session/new_from_agent", { agent_id: agent.id });
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
    }

    async send() {
        const body = this.state.draft.trim();
        if (!body || this.state.sending || !this.state.session) {
            return;
        }
        this.state.sending = true;
        this.state.draft = "";
        const res = await rpc("/turnstone/session/send", {
            session_id: this.state.session.id,
            body,
        });
        this.state.sending = false;
        if (res.error) {
            this.state.draft = body; // give the text back rather than losing it
            return this.fail(res.error);
        }
        this.state.session = res.session;
        this.state.sessions = res.sessions || this.state.sessions;
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
        const res = await rpc("/turnstone/session/approve", {
            session_id: this.state.session.id,
            approved,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = res.session;
        this.maybePoll();
    }

    async cancelGeneration() {
        const res = await rpc("/turnstone/session/cancel", {
            session_id: this.state.session.id,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = res.session;
        this.stopPolling();
    }

    async closeSession() {
        const res = await rpc("/turnstone/session/close", {
            session_id: this.state.session.id,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = null;
        this.state.sessions = res.sessions || [];
        this.stopPolling();
    }

    async retry() {
        const res = await rpc("/turnstone/session/retry", {
            session_id: this.state.session.id,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = res.session;
        this.maybePoll();
    }

    async rewind(turns = 1) {
        const res = await rpc("/turnstone/session/rewind", {
            session_id: this.state.session.id,
            turns,
        });
        if (res.error) {
            return this.fail(res.error);
        }
        this.state.session = res.session;
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
