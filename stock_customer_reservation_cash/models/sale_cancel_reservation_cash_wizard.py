from odoo import _, fields, models
from odoo.exceptions import UserError


class SaleCancelReservationCashWizard(models.TransientModel):
    _name = "sale.cancel.reservation.cash.wizard"
    _description = "Sale Cancel Reservation Cash Wizard"

    order_ids = fields.Many2many(
        "sale.order",
        "sale_cancel_cash_wiz_order_rel",
        "wizard_id",
        "order_id",
        string="Orders",
        readonly=True,
    )
    reservation_ids = fields.Many2many(
        "sale.customer.reservation",
        "sale_cancel_cash_wiz_res_rel",
        "wizard_id",
        "reservation_id",
        string="Reservations",
        readonly=True,
    )
    cash_in_ids = fields.Many2many(
        "reservation.cash.in",
        "sale_cancel_cash_wiz_ci_rel",
        "wizard_id",
        "cash_in_id",
        string="Cash Collections",
        readonly=True,
    )
    decision = fields.Selection(
        [
            ("cancel_non_accounted", "Cancel non-accounted cash collections"),
            ("keep", "Do not change cash collections"),
            ("relink", "Relink cash collections to another reservation/order"),
        ],
        string="Decision",
        default="keep",
        required=True,
    )
    target_reservation_id = fields.Many2one("sale.customer.reservation", string="Target reservation")
    target_sale_order_id = fields.Many2one("sale.order", string="Target quotation/order")

    def _get_or_create_target_reservation(self):
        self.ensure_one()
        if self.target_reservation_id:
            return self.target_reservation_id
        if not self.target_sale_order_id:
            raise UserError(_("Select a target reservation or a target quotation/order."))
        reservation = self.target_sale_order_id.reservation_ids[:1]
        if reservation:
            return reservation
        return self.env["sale.customer.reservation"].create(
            {
                "sale_order_id": self.target_sale_order_id.id,
                "reservation_state": "cancelled",
                "cancellation_date": fields.Datetime.now(),
            }
        )

    def action_confirm(self):
        self.ensure_one()
        cash_ins = self.cash_in_ids
        if self.decision == "cancel_non_accounted":
            blocked = cash_ins.filtered(lambda c: c.state in ("accounted", "refunded"))
            if blocked:
                raise UserError(
                    _(
                        "Some cash collections are already accounted/refunded and cannot be cancelled. "
                        "Choose another decision."
                    )
                )
            cash_ins.filtered(lambda c: c.state != "cancelled").write({"state": "cancelled"})
        elif self.decision == "relink":
            target_reservation = self._get_or_create_target_reservation()
            if target_reservation in self.reservation_ids:
                raise UserError(_("Target reservation must be different from the cancelled reservations."))
            cash_ins.write({"sale_reservation_id": target_reservation.id})

        self.order_ids.with_context(skip_reservation_cash_cancel_wizard=True).action_cancel()
        return {"type": "ir.actions.act_window_close"}
