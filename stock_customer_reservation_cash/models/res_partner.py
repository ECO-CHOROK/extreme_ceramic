from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    reservation_count = fields.Integer(string="Nb réservations", compute="_compute_reservation_and_cash_stats")
    reservation_total_amount = fields.Float(
        string="Total réservé", compute="_compute_reservation_and_cash_stats"
    )
    reservation_stat_display = fields.Char(string="Total réservé (nb)", compute="_compute_reservation_and_cash_stats")

    cash_in_count = fields.Integer(string="Nb encaissements", compute="_compute_reservation_and_cash_stats")
    cash_in_total_amount = fields.Float(string="Total encaissé", compute="_compute_reservation_and_cash_stats")
    cash_in_stat_display = fields.Char(string="Total encaissé (nb)", compute="_compute_reservation_and_cash_stats")
    reservation_transfer_count = fields.Integer(
        string="Nb transferts réservation", compute="_compute_reservation_and_cash_stats"
    )

    def _compute_reservation_and_cash_stats(self):
        stats = {
            partner.id: {
                "reservation_count": 0,
                "reservation_total": 0.0,
                "cash_in_count": 0,
                "cash_in_total": 0.0,
                "reservation_transfer_count": 0,
            }
            for partner in self
        }

        if self.ids:
            reservations = self.env["sale.customer.reservation"].search(
                [("partner_id", "in", self.ids), ("reservation_state", "!=", "cancelled")]
            )
            reservations_all = self.env["sale.customer.reservation"].search([("partner_id", "in", self.ids)])
            transfer_count_by_partner = {}
            all_pickings = reservations_all.mapped("internal_picking_id") | reservations_all.mapped(
                "cancellation_picking_id"
            )
            for picking in all_pickings:
                partner_id = picking.partner_id.id
                if not partner_id:
                    continue
                transfer_count_by_partner.setdefault(partner_id, set()).add(picking.id)
            for reservation in reservations:
                partner_id = reservation.partner_id.id
                if partner_id not in stats:
                    continue
                stats[partner_id]["reservation_count"] += 1
                stats[partner_id]["reservation_total"] += reservation.amount_total or 0.0

            cash_ins = self.env["reservation.cash.in"].search(
                [("partner_id", "in", self.ids), ("state", "not in", ["draft", "cancelled"])]
            )
            for cash_in in cash_ins:
                partner_id = cash_in.partner_id.id
                if partner_id not in stats:
                    continue
                stats[partner_id]["cash_in_count"] += 1
                stats[partner_id]["cash_in_total"] += cash_in.amount or 0.0

            for partner_id, picking_ids in transfer_count_by_partner.items():
                if partner_id in stats:
                    stats[partner_id]["reservation_transfer_count"] = len(picking_ids)

        for partner in self:
            values = stats[partner.id]
            partner.reservation_count = values["reservation_count"]
            partner.reservation_total_amount = values["reservation_total"]
            partner.cash_in_count = values["cash_in_count"]
            partner.cash_in_total_amount = values["cash_in_total"]
            partner.reservation_transfer_count = values["reservation_transfer_count"]

            partner.reservation_stat_display = (
                f"{values['reservation_total']:.2f} ({values['reservation_count']})"
            )
            partner.cash_in_stat_display = f"{values['cash_in_total']:.2f} ({values['cash_in_count']})"

    def action_view_partner_reservations(self):
        self.ensure_one()
        action = self.env.ref("stock_customer_reservation_cash.action_sale_customer_reservation").read()[0]
        action["domain"] = [
            ("partner_id", "=", self.id),
            ("reservation_state", "!=", "cancelled"),
        ]
        action["context"] = {
            "search_default_partner_id": self.id,
            "default_partner_id": self.id,
        }
        return action

    def action_view_partner_cash_ins(self):
        self.ensure_one()
        action = self.env.ref("stock_customer_reservation_cash.action_reservation_cash_in").read()[0]
        action["domain"] = [
            ("partner_id", "=", self.id),
            ("state", "not in", ["draft", "cancelled"]),
        ]
        action["context"] = {
            "search_default_partner_id": self.id,
            "default_partner_id": self.id,
        }
        return action

    def action_view_partner_reservation_transfers(self):
        self.ensure_one()
        reservations = self.env["sale.customer.reservation"].search([("partner_id", "=", self.id)])
        pickings = reservations.mapped("internal_picking_id") | reservations.mapped("cancellation_picking_id")
        if not pickings:
            return False
        action = self.env.ref("stock.action_picking_tree_all").read()[0]
        action["name"] = "Transferts de réservation"
        action["domain"] = [("id", "in", pickings.ids)]
        action["context"] = {"default_partner_id": self.id}
        if len(pickings) == 1:
            action["view_mode"] = "form"
            action["res_id"] = pickings.id
        return action
