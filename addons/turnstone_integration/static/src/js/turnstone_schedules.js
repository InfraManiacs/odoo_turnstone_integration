/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

/**
 * Read-only view of Turnstone's scheduled tasks.
 *
 * Deliberately read-only. A schedule fires an agent with real tool access on a
 * cron, so creating or editing one is a Turnstone administration act, done in
 * the Turnstone console where the skill, persona and tool policy are all in
 * view. Showing them here answers "what runs unattended, and did it work",
 * which is the question an Odoo user actually has.
 */
export class TurnstoneSchedules extends Component {
    static template = "turnstone_integration.TurnstoneSchedules";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            error: null,
            schedules: [],
            connectionId: null,
            expanded: null,
            runs: [],
            runsLoading: false,
        });

        onWillStart(async () => {
            const res = await rpc("/turnstone/schedules", {});
            if (res.error) {
                this.state.error = res.error;
            } else {
                this.state.schedules = res.schedules || [];
                this.state.connectionId = res.connection_id;
            }
            this.state.loading = false;
        });
    }

    /**
     * Open the workstream a scheduled run produced.
     *
     * This is the whole point of showing runs: a job fired at 06:00 with nobody
     * watching, and the question is what it did. Attaching it gives the full
     * transcript, its subagents, and anything it touched in Odoo.
     */
    async openRun(run) {
        const wsId = run.ws_id || run.workstream_id;
        if (!wsId) {
            this.notification.add(_t("That run recorded no workstream."), { type: "warning" });
            return;
        }
        const res = await rpc("/turnstone/session/adopt", {
            connection_id: this.state.connectionId,
            ws_id: wsId,
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

    async toggleRuns(task) {
        if (this.state.expanded === task.task_id) {
            this.state.expanded = null;
            this.state.runs = [];
            return;
        }
        this.state.expanded = task.task_id;
        this.state.runsLoading = true;
        this.state.runs = [];
        const res = await rpc("/turnstone/schedule/runs", {
            connection_id: this.state.connectionId,
            task_id: task.task_id,
        });
        this.state.runsLoading = false;
        if (res.error) {
            this.notification.add(res.error, { type: "danger", title: _t("Turnstone") });
            return;
        }
        this.state.runs = res.runs || [];
    }
}

registry.category("actions").add("turnstone_integration.schedules", TurnstoneSchedules);
