/** @odoo-module **/
import { Component, useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const REFRESH_MS = 60000;

export class BioTimeDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({ data: null, syncing: false });
        onWillStart(() => this.load());
        this._timer = setInterval(() => this.load(), REFRESH_MS);
        onWillUnmount(() => clearInterval(this._timer));
    }

    async load() {
        try {
            this.state.data = await this.orm.call("biotime.server", "get_dashboard_data", []);
        } catch (_e) {
            /* keep the last known data on transient failures */
        }
    }

    notify(message, type = "info") {
        this.notification.add(message, { type, title: "BioTime" });
    }

    async sync(kind) {
        if (this.state.syncing) {
            return;
        }
        this.state.syncing = true;
        try {
            this.notify(await this.orm.call("biotime.server", kind, []));
            await this.load();
        } catch (e) {
            this.notify(e?.data?.message || String(e), "danger");
        }
        this.state.syncing = false;
    }

    openAttAction(domain) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "hr.attendance",
            views: [[false, "list"], [false, "form"]],
            name: "BioTime Attendance",
            domain: domain || [["bt_server_id", "!=", false]],
        });
    }

    kpiClick(kpi) {
        const links = this.state.data?.links || {};
        if (kpi === "employees") {
            this.action.doAction({
                type: "ir.actions.act_window", res_model: "hr.employee",
                views: [[false, "list"], [false, "form"]], name: "Employees",
            });
        } else if (kpi === "present") {
            this.openAttAction(links.today_domain);
        } else if (kpi === "late") {
            this.openAttAction(links.late_domain);
        } else {
            this.action.doAction({
                type: "ir.actions.act_window", res_model: "hr.employee",
                views: [[false, "list"], [false, "form"]], name: "Absent Today",
                domain: links.absent_domain || [],
            });
        }
    }

    trendMax() {
        const rows = this.state.data?.trend || [];
        return Math.max(1, ...rows.map((r) => r.present));
    }
}

BioTimeDashboard.template = "biotime_connect_pro.BioTimeDashboard";

registry.category("actions").add("biotime_dashboard", BioTimeDashboard);
