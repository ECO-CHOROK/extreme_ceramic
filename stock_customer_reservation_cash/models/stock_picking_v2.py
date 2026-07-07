from odoo import _, fields, models
from odoo.exceptions import UserError


class StockPickingV2Reservation(models.Model):
    _inherit = "stock.picking"

    def button_validate(self):
        self.ensure_one()
        if (
            not self.env.context.get("skip_delivery_contact_wizard")
            and self.picking_type_code == "outgoing"
            and self.sale_id
            and self.state not in ("done", "cancel")
        ):
            return {
                "type": "ir.actions.act_window",
                "name": _("Delivery Validation"),
                "res_model": "delivery.contact.wizard",
                "view_mode": "form",
                "target": "new",
                "context": {"default_picking_id": self.id},
            }
        return super().button_validate()

    def action_validate_with_delivery_contact(self, invoice_to_other=False, contact_id=False):
        self.ensure_one()
        if self.picking_type_code != "outgoing" or not self.sale_id:
            return self.with_context(skip_delivery_contact_wizard=True).button_validate()

        order = self.sale_id
        original_partner = order.partner_id
        old_qty_by_line = {line.id: line.product_uom_qty for line in order.order_line.filtered(lambda l: not l.display_type)}

        if invoice_to_other:
            if not contact_id:
                raise UserError(_("Select a contact when 'Invoice to another contact' is checked."))
            new_partner = self.env["res.partner"].browse(contact_id)
            order.write(
                {
                    "partner_id": new_partner.id,
                    "partner_invoice_id": new_partner.id,
                }
            )
            self.partner_id = new_partner.id

        self._autofill_qty_done_if_empty()
        result = self.with_context(skip_delivery_contact_wizard=True, skip_backorder=True).button_validate()
        self._sync_sale_order_after_delivery(order, original_partner, old_qty_by_line)
        return result

    def _autofill_qty_done_if_empty(self):
        for move in self.move_ids.filtered(lambda m: m.state not in ("done", "cancel")):
            if move.quantity > 0:
                continue
            if move.product_uom_qty > 0:
                move.quantity = move.product_uom_qty

    def _sync_sale_order_after_delivery(self, order, original_partner, old_qty_by_line):
        delivered_by_line = {}
        for move in self.move_ids.filtered(lambda m: m.state == "done" and m.sale_line_id):
            delivered_by_line.setdefault(move.sale_line_id.id, 0.0)
            delivered_by_line[move.sale_line_id.id] += move.quantity or 0.0

        remaining_lines_vals = []
        for line in order.order_line.filtered(lambda l: not l.display_type):
            old_qty = old_qty_by_line.get(line.id, line.product_uom_qty)
            delivered = delivered_by_line.get(line.id, 0.0)
            remaining = max(old_qty - delivered, 0.0)

            if delivered > 0:
                line.product_uom_qty = delivered
            else:
                line.unlink()

            if remaining > 0:
                remaining_lines_vals.append(
                    (
                        0,
                        0,
                        {
                            "product_id": line.product_id.id,
                            "product_uom_qty": remaining,
                            "product_uom_id": line.product_uom_id.id,
                            "price_unit": line.price_unit,
                            "name": line.name,
                            "discount": line.discount,
                            "tax_ids": [(6, 0, line.tax_ids.ids)],
                        },
                    )
                )

        if not remaining_lines_vals:
            return

        new_order = self.env["sale.order"].create(
            {
                "partner_id": original_partner.id,
                "partner_invoice_id": original_partner.id,
                "partner_shipping_id": original_partner.id,
                "company_id": order.company_id.id,
                "pricelist_id": order.pricelist_id.id,
                "payment_term_id": order.payment_term_id.id if order.payment_term_id else False,
                "warehouse_id": order.warehouse_id.id if order.warehouse_id else False,
                "origin": order.name,
                "order_line": remaining_lines_vals,
            }
        )
        order.message_post(body=_("Backorder created on the new sales order %s.") % new_order.name)
        new_order.message_post(body=_("Sales order created from backorder of %s.") % order.name)
