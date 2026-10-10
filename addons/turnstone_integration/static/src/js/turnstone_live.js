/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

/**
 * What Turnstone is doing right now, across the whole cluster.
 *
 * The question this answers is "is anything running, and is anything stuck" —
 * for scheduled jobs nobody is watching as much as for chat sessions. Rows are
 * nested by parent_ws_id so a coordinator and the subagents it spawned read as
 * one unit of work rather than six unrelated lines.
 *
 * Manager-only, enforced server side: the feed covers every workstream on the
 * server regardless of who started it.
 */
export class TurnstoneLive extends Component {
    static template = "turnstone_integration.TurnstoneLive";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            error: null,
            overview: null,
            rows: [],
            connectionId: null,
            connectionName: "",
            filter: "",
            autoRefresh: true,
        });
        this.handle = null;

        onWillStart(() => this.load());
        onWillUnmount(() => this.stop());
    }

    async load() {
        const res = await rpc("/turnstone/live", {
            state: this.state.filter || undefined,
        });
        if (res.error) {
            this.state.error = res.error;
            this.state.loading = false;
            this.stop();
            return;
        }
        this.state.error = null;
        this.state.overview = res.overview;
        this.state.connectionId = res.connection_id;
        this.state.connectionName = res.connection_name;
        this.state.rows = this.nest(res.workstreams || []);
        this.state.loading = false;
        this.schedule();
    }

    /** Order parents first, then their children, with a depth for indenting. */
    nest(rows) {
        const byId = new Map(rows.map((r) => [r.ws_id, r]));
        const children = new Map();
        const roots = [];
        for (const r of rows) {
            // A parent that is not in the feed (closed, or filtered out) means
            // the child is effectively a root here — better shown than hidden.
            if (r.parent_ws_id && byId.has(r.parent_ws_id)) {
                if (!children.has(r.parent_ws_id)) {
                    children.set(r.parent_ws_id, []);
                }
                children.get(r.parent_ws_id).push(r);
            } else {
                roots.push(r);
            }
        }
        const out = [];
        const walk = (row, depth) => {
            out.push({ ...row, depth });
            for (const child of children.get(row.ws_id) || []) {
                walk(child, depth + 1);
            }
        };
        const busy = (r) => (r.state === "attention" ? 0 : r.state === "thinking" ? 1 : 2);
        roots.sort((a, b) => busy(a) - busy(b));
        roots.forEach((r) => walk(r, 0));
        return out;
    }

    schedule() {
        this.stop();
        if (!this.state.autoRefresh) {
            return;
        }
        // 5s: this is a whole-cluster call, and nobody is reading it
        // keystroke-by-keystroke.
        this.handle = setTimeout(() => this.load(), 5000);
    }

    stop() {
        if (this.handle) {
            clearTimeout(this.handle);
            this.handle = null;
        }
    }

    toggleAutoRefresh() {
        this.state.autoRefresh = !this.state.autoRefresh;
        this.schedule();
    }

    async setFilter(value) {
        this.state.filter = value;
        await this.load();
    }

    /** Open it in the chat, attaching it to Odoo first if it is new to us. */
    async open(row) {
        if (row.odoo_session_id) {
            this.action.doAction({
                type: "ir.actions.client",
                tag: "turnstone_integration.chat",
                params: { session_id: row.odoo_session_id },
            });
            return;
        }
        const res = await rpc("/turnstone/session/adopt", {
            connection_id: this.state.connectionId,
            ws_id: row.ws_id,
        });
        if (res.error) {
            this.notification.add(res.error, { type: "danger", title: _t("Turnstone") });
            return;
        }
        this.action.doAction({
            type: "ir.actions.client",
            tag: "turnstone_integration.chat",
            params: { session_id: res.session.id },
        });
    }

    stateBadge(state) {
        return {
            idle: "text-bg-success",
            thinking: "text-bg-info",
            running: "text-bg-info",
            attention: "text-bg-warning",
            error: "text-bg-danger",
        }[state] || "text-bg-secondary";
    }

    percent(ratio) {
        return Math.round((ratio || 0) * 100);
    }
}

registry.category("actions").add("turnstone_integration.live", TurnstoneLive);
