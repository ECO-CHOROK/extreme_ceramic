from odoo import _, api, fields, models
from odoo.tools.float_utils import float_round


class SaleCustomerReservation(models.Model):
    _name = "sale.customer.reservation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Customer Reservation from Quotation"
    _order = "create_date desc, id desc"

    name = fields.Char(default="New", readonly=True, copy=False, tracking=True)
    sale_order_id = fields.Many2one(
        "sale.order", required=True, ondelete="cascade", index=True, tracking=True
    )
    # Additional orders that later split off from sale_order_id (e.g. an
    # invoice-to-another-contact delivery backorder). These stay traceable to
    # this reservation even though they are separate sale.order records.
    linked_sale_order_ids = fields.Many2many(
        "sale.order",
        "sale_customer_reservation_order_rel",
        "reservation_id",
        "sale_order_id",
        string="Additional Linked Orders",
        copy=False,
    )
    all_sale_order_ids = fields.Many2many(
        "sale.order",
        compute="_compute_all_sale_order_ids",
        string="All Linked Orders",
    )
    # Snapshot of who actually made the reservation. Set once at creation and
    # never re-synced: a later "invoice to another contact" swap on the sale
    # order must not change who this reservation belongs to.
    partner_id = fields.Many2one("res.partner", string="Reservation Customer", copy=False, tracking=True)
    company_id = fields.Many2one(related="sale_order_id.company_id", store=True, tracking=True)
    currency_id = fields.Many2one(related="sale_order_id.currency_id", store=True, tracking=True)
    # Snapshot of the order total at the moment the reservation was created.
    # Deliberately frozen: it must not move if the order (or a split-off
    # backorder) is edited afterward, since the physical stock reservation
    # itself is already locked in at this point.
    amount_total = fields.Monetary(copy=False, currency_field="currency_id", tracking=True)
    reservation_state = fields.Selection(
        [("reserved", "Reserved"), ("cancelled", "Cancelled")],
        default="reserved",
        copy=False,
        tracking=True,
    )
    internal_picking_id = fields.Many2one("stock.picking", readonly=True, copy=False, tracking=True)
    cancellation_picking_id = fields.Many2one("stock.picking", readonly=True, copy=False, tracking=True)
    reservation_date = fields.Datetime(readonly=True, copy=False, tracking=True)
    cancellation_date = fields.Datetime(readonly=True, copy=False, tracking=True)
    cash_in_ids = fields.One2many("reservation.cash.in", "sale_reservation_id")
    amount_received_operational = fields.Monetary(
        compute="_compute_cash_amounts", currency_field="currency_id", store=True
    )
    amount_due = fields.Monetary(compute="_compute_cash_amounts", currency_field="currency_id", store=True)
    cash_in_count = fields.Integer(compute="_compute_cash_amounts")

    def init(self):
        # Keep reservation history by allowing multiple reservations per quotation.
        self.env.cr.execute(
            "ALTER TABLE sale_customer_reservation DROP CONSTRAINT IF EXISTS sale_order_unique"
        )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("sale.customer.reservation") or "New"
            if vals.get("sale_order_id"):
                order = self.env["sale.order"].browse(vals["sale_order_id"])
                # Freeze the reservation's customer and value at creation
                # time. Neither should drift afterward: not when the order's
                # partner is later reassigned (invoice-to-another-contact),
                # and not when the order's lines are edited or split.
                if not vals.get("partner_id"):
                    vals["partner_id"] = order.partner_id.id
                if not vals.get("amount_total"):
                    vals["amount_total"] = order.amount_total
        return super().create(vals_list)

    @api.depends("linked_sale_order_ids", "sale_order_id")
    def _compute_all_sale_order_ids(self):
        for rec in self:
            rec.all_sale_order_ids = rec.sale_order_id + rec.linked_sale_order_ids

    @api.depends("amount_total", "cash_in_ids.amount", "cash_in_ids.state")
    def _compute_cash_amounts(self):
        accepted_states = {"confirmed", "to_account", "accounted"}
        for rec in self:
            received = sum(ci.amount for ci in rec.cash_in_ids if ci.state in accepted_states)
            rec.amount_received_operational = float_round(received, precision_rounding=rec.currency_id.rounding)
            rec.amount_due = rec.amount_total - rec.amount_received_operational
            rec.cash_in_count = len(rec.cash_in_ids)

    def action_view_cash_in(self):
        self.ensure_one()
        tree_view = self.env.ref("stock_customer_reservation_cash.view_reservation_cash_in_tree")
        form_view = self.env.ref("stock_customer_reservation_cash.view_reservation_cash_in_form")
        return {
            "type": "ir.actions.act_window",
            "name": _("Cash Collections"),
            "res_model": "reservation.cash.in",
            "view_mode": "list,form",
            "views": [(tree_view.id, "list"), (form_view.id, "form")],
            "domain": [("sale_reservation_id", "=", self.id)],
            "context": {"default_sale_reservation_id": self.id},
        }
