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
