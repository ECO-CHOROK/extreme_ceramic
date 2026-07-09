from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ResPartnerReservation(models.Model):
    _inherit = "res.partner"

    property_reservation_location_id = fields.Many2one(
        "stock.location",
        company_dependent=True,
        string="Customer Reservation Location",
        domain="[('usage', '=', 'internal')]",
    )

    def _get_or_create_reservation_location(self, parent_location, company):
        self.ensure_one()
        partner = self.with_company(company)
        location_name = partner.name or partner.display_name
        if partner.property_reservation_location_id:
            location = partner.property_reservation_location_id
            if location.name != location_name:
                location.name = location_name
            return location

        location = self.env["stock.location"].create(
            {
                "name": location_name,
                "usage": "internal",
                "location_id": parent_location.id,
                "company_id": company.id,
            }
        )
        partner.property_reservation_location_id = location
        return location


class SaleOrderLineV2Reservation(models.Model):
    _inherit = "sale.order.line"

    def _prepare_procurement_values(self):
        values = super()._prepare_procurement_values()
        order = self.order_id
        if order.reservation_state == "reserved" and order.reservation_id:
            reservation_location = (
                order.reservation_id.internal_picking_id.location_dest_id
                or order.partner_id.property_reservation_location_id
            )
            if reservation_location:
                # Route the delivery's source location to the customer's
                # reservation location instead of the standard warehouse
                # stock location, without altering anything else about the
                # standard confirm -> stock rule -> move creation flow.
                values["location_id"] = reservation_location.id
        return values


class StockRuleReservation(models.Model):
    _inherit = "stock.rule"

    def _get_custom_move_fields(self):
        fields_list = super()._get_custom_move_fields()
        fields_list.append("location_id")
        return fields_list


class SaleOrderV2Reservation(models.Model):
    _inherit = "sale.order"

    warehouse_view_location_id = fields.Many2one(
        related="warehouse_id.view_location_id",
        string="Warehouse Root Location",
        readonly=True,
    )
    reservation_source_location_id = fields.Many2one(
        "stock.location",
        string="Emplacement d'origine de réservation",
        domain="[('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        default=lambda self: self._default_reservation_source_location(),
        copy=False,
    )
    reservation_id = fields.Many2one("sale.customer.reservation", copy=False, readonly=True)
    # NOTE: this cannot be a plain One2many("sale.customer.reservation", "sale_order_id")
    # because a reservation can also be linked to an order via
    # sale.customer.reservation.linked_sale_order_ids (e.g. the "rest" order
    # created from a partial delivery backorder). A One2many only follows a
    # single inverse field, so it would silently miss those orders. This is
    # computed instead so it reflects both the order that originally created
    # the reservation and any order later linked to it.
    reservation_ids = fields.Many2many(
        "sale.customer.reservation",
        compute="_compute_reservation_ids",
        string="Reservations",
    )
    reservation_state = fields.Selection(
        [("none", "None"), ("reserved", "Reserved"), ("cancelled", "Cancelled")],
        default="none",
        copy=False,
        readonly=True,
    )
    reservation_count = fields.Integer(compute="_compute_reservation_count")
    reservation_transfer_count = fields.Integer(compute="_compute_reservation_transfer_count")
    cash_in_count = fields.Integer(compute="_compute_cash_in_stats")
    cash_in_total_amount = fields.Monetary(
        compute="_compute_cash_in_stats", currency_field="currency_id", store=False
    )
    cash_in_ratio_display = fields.Char(compute="_compute_cash_in_stats")
    reservation_credit_alert = fields.Boolean(compute="_compute_reservation_credit_alert")
    reservation_credit_alert_message = fields.Char(compute="_compute_reservation_credit_alert")

    @api.model
    def _default_reservation_source_location(self):
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        if not warehouse:
            return False
        picking_type = warehouse.reservation_internal_picking_type_id or warehouse.int_type_id
        return (picking_type.default_location_src_id or warehouse.lot_stock_id).id

    def _compute_reservation_ids(self):
        for order in self:
            order.reservation_ids = self.env["sale.customer.reservation"].search(
                ["|", ("sale_order_id", "=", order.id), ("linked_sale_order_ids", "in", order.id)]
            )

    def _compute_reservation_count(self):
        for order in self:
            order.reservation_count = len(order.reservation_ids)

    def _compute_reservation_transfer_count(self):
        for order in self:
            transfers = order.reservation_ids.mapped("internal_picking_id") | order.reservation_ids.mapped(
                "cancellation_picking_id"
            )
            order.reservation_transfer_count = len(transfers)

    def _compute_cash_in_stats(self):
        accepted_states = {"confirmed", "to_account", "accounted"}
        for order in self:
            cash_ins = order.reservation_ids.mapped("cash_in_ids")
            order.cash_in_count = len(cash_ins)
            total = sum(ci.amount for ci in cash_ins if ci.state in accepted_states)
            order.cash_in_total_amount = total
            order.cash_in_ratio_display = f"{total:.2f} / {order.amount_total:.2f}"

    def _compute_reservation_credit_alert(self):
        sale_model = self.env["sale.order"]
        reservation_model = self.env["sale.customer.reservation"]
        cash_in_model = self.env["reservation.cash.in"]
        payment_model = self.env["account.payment"]

        for order in self:
            order.reservation_credit_alert = False
            order.reservation_credit_alert_message = False

            if not order.partner_id or not order.company_id:
                continue

            commercial_partner = order.partner_id.commercial_partner_id.with_company(order.company_id)
            credit_limit = commercial_partner.credit_limit or 0.0
            if credit_limit <= 0:
                continue

            sale_orders = sale_model.search(
                [
                    ("company_id", "=", order.company_id.id),
                    ("state", "in", ["sale", "done"]),
                    ("partner_id", "child_of", commercial_partner.id),
                    ("reservation_state", "!=", "reserved"),
                ]
            )
            total_confirmed_without_reservation = sum(sale_orders.mapped("amount_total"))

            active_reservations = reservation_model.search(
                [
                    ("company_id", "=", order.company_id.id),
                    ("partner_id", "child_of", commercial_partner.id),
                    ("reservation_state", "=", "reserved"),
                ]
            )
            total_active_reservations = sum(active_reservations.mapped("amount_total"))

            cash_without_payment = cash_in_model.search(
                [
                    ("company_id", "=", order.company_id.id),
                    ("partner_id", "child_of", commercial_partner.id),
                    ("state", "not in", ["draft", "cancelled"]),
                    ("account_payment_id", "=", False),
                ]
            )
            total_cash_without_payment = sum(cash_without_payment.mapped("amount"))

            posted_payments = payment_model.search(
                [
                    ("company_id", "=", order.company_id.id),
                    ("partner_id", "child_of", commercial_partner.id),
                    ("state", "=", "posted"),
                    ("partner_type", "=", "customer"),
                    ("payment_type", "=", "inbound"),
                ]
            )
            total_payments = 0.0
            for payment in posted_payments:
                payment_currency = payment.currency_id or order.company_id.currency_id
                total_payments += payment_currency._convert(
                    payment.amount,
                    order.currency_id,
                    order.company_id,
                    payment.date or fields.Date.context_today(order),
                )

            exposure = (
                total_confirmed_without_reservation + total_active_reservations
            ) - (total_cash_without_payment + total_payments)

            if exposure + 1e-6 >= credit_limit:
                order.reservation_credit_alert = True
                order.reservation_credit_alert_message = _(
                    "Alerte crédit réservation: (BC sans réservation %.2f + Réservations actives %.2f) - "
                    "(Encaissements sans paiement %.2f + Paiements %.2f) = %.2f, limite %.2f."
                ) % (
                    total_confirmed_without_reservation,
                    total_active_reservations,
                    total_cash_without_payment,
                    total_payments,
                    exposure,
                    credit_limit,
                )

    @api.onchange("warehouse_id")
    def _onchange_warehouse_id_set_reservation_source(self):
        for order in self:
            warehouse = order.warehouse_id
            if not warehouse:
                order.reservation_source_location_id = False
                continue
            if (
                order.reservation_source_location_id
                and warehouse.view_location_id
                and self.env["stock.location"].search_count(
                    [
                        ("id", "=", order.reservation_source_location_id.id),
                        ("id", "child_of", warehouse.view_location_id.id),
                    ]
                )
            ):
                continue
            picking_type = warehouse.reservation_internal_picking_type_id or warehouse.int_type_id
            order.reservation_source_location_id = (
                picking_type.default_location_src_id or warehouse.lot_stock_id
            )

    @api.constrains("warehouse_id", "reservation_source_location_id")
    def _check_reservation_source_location_warehouse(self):
        for order in self:
            if not order.warehouse_id or not order.reservation_source_location_id:
                continue
            if order.reservation_source_location_id.company_id and (
                order.reservation_source_location_id.company_id != order.company_id
            ):
                raise UserError(_("The reservation source location must belong to the quotation company."))
            view_location = order.warehouse_id.view_location_id
            if not view_location:
                continue
            is_in_warehouse = bool(
                self.env["stock.location"].search_count(
                    [
                        ("id", "=", order.reservation_source_location_id.id),
                        ("id", "child_of", view_location.id),
                    ]
                )
            )
            if not is_in_warehouse:
                raise UserError(
                    _("The reservation source location must belong to the selected warehouse.")
                )

    def action_reserve_quotation(self):
        for order in self:
            if order.state not in ("draft", "sent"):
                raise UserError(_("Reservation is only possible on a draft or sent quotation."))
            stockable_lines = order.order_line.filtered(
                lambda l: not l.display_type and l.product_id and l.product_id.type in ("product", "consu")
            )
            if not stockable_lines:
                raise UserError(_("Add at least one line to the quotation before reserving."))

            if order.reservation_state == "reserved" and order.reservation_id:
                continue

            warehouse = self.env["stock.warehouse"].search(
                [("company_id", "=", order.company_id.id)], limit=1
            )
            if not warehouse:
                raise UserError(_("No warehouse found for the quotation company."))
            if not warehouse.reservation_parent_location_id:
                raise UserError(_("Configure the reservation parent location on the warehouse."))

            destination_location = order.partner_id._get_or_create_reservation_location(
                warehouse.reservation_parent_location_id, order.company_id
            )
            picking_type = warehouse.reservation_internal_picking_type_id or warehouse.int_type_id
            if not picking_type:
                raise UserError(_("No internal transfer type configured on the warehouse."))
            source_location = order.reservation_source_location_id
            if not source_location:
                raise UserError(_("Set the reservation source location on the quotation."))

            picking = self.env["stock.picking"].create(
                {
                    "picking_type_id": picking_type.id,
                    "location_id": source_location.id,
                    "location_dest_id": destination_location.id,
                    "partner_id": order.partner_id.id,
                    "origin": order.name,
                    "company_id": order.company_id.id,
                }
            )

            for line in stockable_lines:
                self.env["stock.move"].create(
                    {
                        "description_picking": line.product_id.display_name,
                        "product_id": line.product_id.id,
                        "product_uom_qty": line.product_uom_qty,
                        "quantity": line.product_uom_qty,
                        "product_uom": line.product_uom_id.id,
                        "location_id": source_location.id,
                        "location_dest_id": destination_location.id,
                        "picking_id": picking.id,
                        "company_id": order.company_id.id,
                    }
                )

            picking.action_confirm()
            picking.action_assign()
            picking.button_validate()

            reservation = self.env["sale.customer.reservation"].create(
                {
                    "sale_order_id": order.id,
                    "reservation_state": "reserved",
                    "internal_picking_id": picking.id,
                    "reservation_date": fields.Datetime.now(),
                }
            )
            order.reservation_id = reservation.id
            order.reservation_state = "reserved"
        return True

    def _cancel_single_reservation(self, reservation):
        self.ensure_one()
        original_picking = reservation.internal_picking_id
        reverse_picking = self.env["stock.picking"]
        if original_picking:
            # Use Odoo return workflow to preserve links between source and reverse transfer.
            wizard_ctx = dict(
                self.env.context,
                active_id=original_picking.id,
                active_ids=[original_picking.id],
                active_model="stock.picking",
            )
            return_wizard = self.env["stock.return.picking"].with_context(wizard_ctx).create(
                {"picking_id": original_picking.id}
            )
            if not return_wizard.product_return_moves:
                return_wizard._onchange_picking_id()
            if not return_wizard.product_return_moves:
                raise UserError(_("No return lines are available for this reservation transfer."))
            for line in return_wizard.product_return_moves.filtered(lambda l: l.move_id.state != "cancel"):
                qty = line.move_id.quantity or line.move_id.product_uom_qty
                if qty > 0:
                    line.quantity = qty

            create_res = return_wizard._create_return()
            new_picking = self.env["stock.picking"]

            if isinstance(create_res, dict):
                candidate = create_res.get("res_id")
            elif isinstance(create_res, (tuple, list)):
                candidate = create_res[0] if create_res else False
            else:
                candidate = create_res

            if hasattr(candidate, "_name") and getattr(candidate, "_name", "") == "stock.picking":
                new_picking = candidate
            elif isinstance(candidate, int):
                new_picking = self.env["stock.picking"].browse(candidate)
            elif isinstance(candidate, str) and candidate.isdigit():
                new_picking = self.env["stock.picking"].browse(int(candidate))

            reverse_picking = new_picking.exists()
            if not reverse_picking:
                raise UserError(_("Failed to create reverse reservation transfer."))
            reverse_picking.action_confirm()
            reverse_picking.action_assign()
            for move in reverse_picking.move_ids.filtered(lambda m: m.state not in ("done", "cancel")):
                if move.quantity <= 0 and move.product_uom_qty > 0:
                    move.quantity = move.product_uom_qty
            reverse_picking.button_validate()

        reservation.write(
            {
                "reservation_state": "cancelled",
                "cancellation_date": fields.Datetime.now(),
                "cancellation_picking_id": reverse_picking.id or False,
            }
        )

    def _cancel_order_reservations(self):
        for order in self:
            active_reservations = order.reservation_ids.filtered(lambda r: r.reservation_state == "reserved")
            for reservation in active_reservations:
                order._cancel_single_reservation(reservation)
            if active_reservations:
                order.reservation_state = "cancelled"
        return True

    def action_cancel_reservation(self):
        for order in self:
            if not order.reservation_ids.filtered(lambda r: r.reservation_state == "reserved"):
                continue
            order._cancel_order_reservations()
        return True

    def _get_order_reservation_cashins_for_cancel(self):
        reservations = self.mapped("reservation_ids").filtered(lambda r: r.reservation_state == "reserved")
        cash_ins = reservations.mapped("cash_in_ids").filtered(lambda c: c.state != "cancelled")
        return reservations, cash_ins

    def action_cancel(self):
        reservations, cash_ins = self._get_order_reservation_cashins_for_cancel()
        if (
            not self.env.context.get("skip_reservation_cash_cancel_wizard")
            and reservations
            and cash_ins
        ):
            return {
                "type": "ir.actions.act_window",
                "name": _("Cancellation and Cash Collections"),
                "res_model": "sale.cancel.reservation.cash.wizard",
                "view_mode": "form",
                "target": "new",
                "context": {
                    "default_order_ids": [(6, 0, self.ids)],
                    "default_reservation_ids": [(6, 0, reservations.ids)],
                    "default_cash_in_ids": [(6, 0, cash_ins.ids)],
                },
            }
        result = super().action_cancel()
        self._cancel_order_reservations()
        return result

    def action_view_reservation(self):
        self.ensure_one()
        if not self.reservation_ids:
            return False
        if len(self.reservation_ids) == 1:
            return {
                "type": "ir.actions.act_window",
                "name": _("Reservation"),
                "res_model": "sale.customer.reservation",
                "res_id": self.reservation_ids.id,
                "view_mode": "form",
            }
        return {
            "type": "ir.actions.act_window",
            "name": _("Reservations"),
            "res_model": "sale.customer.reservation",
            "view_mode": "list,form",
            "domain": [("sale_order_id", "=", self.id)],
        }

    def action_view_cash_in(self):
        self.ensure_one()
        tree_view = self.env.ref("stock_customer_reservation_cash.view_reservation_cash_in_tree")
        form_view = self.env.ref("stock_customer_reservation_cash.view_reservation_cash_in_form")
        # NOTE: reservation.cash.in.sale_order_id is a related field to
        # sale_reservation_id.sale_order_id, which only ever resolves to the
        # order that originally created the reservation. Filtering on it
        # directly would show nothing for an order that was later linked to
        # the same reservation (e.g. a partial-delivery backorder), even
        # though that order's reservation_id is set correctly. Filter via
        # self.reservation_ids (which does account for linked orders) instead.
        return {
            "type": "ir.actions.act_window",
            "name": _("Cash Collections"),
            "res_model": "reservation.cash.in",
            "view_mode": "list,form",
            "views": [(tree_view.id, "list"), (form_view.id, "form")],
            "domain": [("sale_reservation_id", "in", self.reservation_ids.ids)],
            "context": {"default_sale_reservation_id": self.reservation_id.id},
        }

    def action_view_reservation_transfers(self):
        self.ensure_one()
        pickings = self.reservation_ids.mapped("internal_picking_id") | self.reservation_ids.mapped(
            "cancellation_picking_id"
        )
        if not pickings:
            return False
        action = self.env.ref("stock.action_picking_tree_all").read()[0]
        action["name"] = _("Transferts de réservation")
        action["domain"] = [("id", "in", pickings.ids)]
        action["context"] = {"default_origin": self.name}
        if len(pickings) == 1:
            action["view_mode"] = "form"
            action["res_id"] = pickings.id
        return action

    def action_open_cash_in_wizard(self):
        self.ensure_one()
        if not self.reservation_id or self.reservation_state != "reserved":
            raise UserError(_("An active reservation is required to collect cash from the quotation."))
        form_view = self.env.ref("stock_customer_reservation_cash.view_reservation_cash_in_form")
        remaining = self.reservation_id.amount_due if self.reservation_id.amount_due > 0 else 0.0
        return {
            "type": "ir.actions.act_window",
            "name": _("Collect Cash"),
            "res_model": "reservation.cash.in",
            "view_mode": "form",
            "views": [(form_view.id, "form")],
            "target": "new",
            "context": {
                "default_sale_reservation_id": self.reservation_id.id,
                "default_amount": remaining,
            },
        }

    @api.model
    def _cleanup_legacy_stock_customer_reservation_menu(self):
        """Archive legacy inventory reservation menu/actions bound to removed model."""
        action_model = self.env["ir.actions.act_window"].sudo()
        menu_model = self.env["ir.ui.menu"].sudo()

        legacy_actions = action_model.search([("res_model", "=", "stock.customer.reservation")])
        if not legacy_actions:
            return True

        legacy_menus = menu_model.browse()
        for action in legacy_actions:
            legacy_menus |= menu_model.search([("action", "=", f"ir.actions.act_window,{action.id}")])

        if legacy_menus:
            legacy_roots = legacy_menus.mapped("parent_id").filtered(
                lambda m: m.name in ("Reservations Clients", "Customer Reservations")
                and m.parent_id
                and m.parent_id.name in ("Inventory", "Inventaire")
            )
            (legacy_menus | legacy_roots).write({"active": False})

        # Best-effort cleanup: remove broken action so direct old URLs stop resolving.
        try:
            legacy_actions.unlink()
        except Exception:
            pass
        return True

    @api.model
    def action_cleanup_legacy_stock_customer_reservation_menu(self):
        return self._cleanup_legacy_stock_customer_reservation_menu()
