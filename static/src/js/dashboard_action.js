/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";

// Base component class
export class MaVieDashboardAction extends Component {
    get page() {
        return 'ventes';
    }
    get iframeSrc() {
        const ts = new Date().getTime();
        return `/mavie/dashboard?page=${this.page}&t=${ts}`;
    }
}
MaVieDashboardAction.template = "mavie_dashboard.IframeView";

// Component for Sales
export class MaVieDashboardActionSales extends MaVieDashboardAction {
    get page() {
        return 'ventes';
    }
}
MaVieDashboardActionSales.template = "mavie_dashboard.IframeView";

// Component for Stock
export class MaVieDashboardActionStock extends MaVieDashboardAction {
    get page() {
        return 'stock';
    }
}
MaVieDashboardActionStock.template = "mavie_dashboard.IframeView";

// Register action components
registry.category("actions").add("mavie_dashboard.action_sales", MaVieDashboardActionSales);
registry.category("actions").add("mavie_dashboard.action_stock", MaVieDashboardActionStock);