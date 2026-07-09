from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class ReservationCashIn(models.Model):
    _name = "reservation.cash.in"
    _description = "Operational Cash Collection"
    _order = "date desc, id desc"

    name = fields.Char(default="New", readonly=True, copy=False)
    sale_reservation_id = fields.Many2one(
        "sale.customer.reservation", string="Reservation", required=True, ondelete="cascade"
    )
    sale_order_id = fields.Many2one(related="sale_reservation_id.sale_order_id", store=True, readonly=True)
    partner_id = fields.Many2one(related="sale_reservation_id.partner_id", store=True)
    company_id = fields.Many2one(related="sale_reservation_id.company_id", store=True)
    amount = fields.Monetary(required=True)
    currency_id = fields.Many2one(related="sale_reservation_id.currency_id", store=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    payment_mode_id = fields.Many2one("reservation.payment.mode")
    is_cheque_lcn = fields.Boolean(related="payment_mode_id.is_cheque_lcn", store=True, readonly=True)
    journal_id = fields.Many2one(
        "account.journal", related="payment_mode_id.journal_id", store=True, readonly=True
    )
    cheque_lcn_date = fields.Date(string="Date Chèque/LCN")
    cheque_lcn_issuer = fields.Char(string="Émetteur")
    cheque_lcn_number = fields.Char(string="N° Chèque/LCN")
    reference = fields.Char()
    attachment_ids = fields.Many2many("ir.attachment", string="Attachments")
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("confirmed", "Confirmed"),
            ("to_account", "To Account"),
            ("accounted", "Accounted"),
            ("cancelled", "Cancelled"),
            ("refunded", "Refunded"),
        ],
        default="draft",
        required=True,
    )
    account_payment_id = fields.Many2one("account.payment", readonly=True, copy=False)
    account_move_id = fields.Many2one("account.move", readonly=True, copy=False)
    accounting_date = fields.Date(readonly=True, copy=False)
    accounted_by = fields.Many2one("res.users", readonly=True, copy=False)
    _NON_COUNTED_STATES = ("draft", "cancelled")

    def init(self):
        # Migration: rebind legacy cash-ins (reservation_id) to sale_reservation_id, then drop legacy column.
        cr = self.env.cr
        cr.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'reservation_cash_in' AND column_name = 'reservation_id'
            """
        )
        if not cr.fetchone():
            return

        # Link to existing sale reservations by partner/company when possible.
        cr.execute(
            """
            UPDATE reservation_cash_in AS rc
            SET sale_reservation_id = mapped.sale_reservation_id
            FROM (
                SELECT
                    rc2.id AS cash_in_id,
                    (
                        SELECT scr.id
                        FROM sale_customer_reservation scr
                        JOIN sale_order so ON so.id = scr.sale_order_id
                        WHERE so.partner_id = sr.partner_id
                          AND so.company_id = sr.company_id
                        ORDER BY scr.id DESC
                        LIMIT 1
                    ) AS sale_reservation_id
                FROM reservation_cash_in rc2
                JOIN stock_customer_reservation sr ON sr.id = rc2.reservation_id
                WHERE rc2.sale_reservation_id IS NULL
                  AND rc2.reservation_id IS NOT NULL
            ) AS mapped
            WHERE rc.id = mapped.cash_in_id
              AND mapped.sale_reservation_id IS NOT NULL
            """
        )

        # Create fallback sale reservations when no direct mapping exists.
        cr.execute(
            """
            SELECT rc.id, sr.partner_id, sr.company_id
            FROM reservation_cash_in rc
            JOIN stock_customer_reservation sr ON sr.id = rc.reservation_id
            WHERE rc.sale_reservation_id IS NULL
              AND rc.reservation_id IS NOT NULL
            """
        )
        rows = cr.fetchall()
        SaleOrder = self.env["sale.order"].sudo()
        SaleReservation = self.env["sale.customer.reservation"].sudo()
        for cash_in_id, partner_id, company_id in rows:
            order = SaleOrder.search(
                [("partner_id", "=", partner_id), ("company_id", "=", company_id)],
                order="id desc",
                limit=1,
            )
            if not order:
                order = SaleOrder.with_company(company_id).create(
                    {"partner_id": partner_id, "company_id": company_id}
                )
            reservation = SaleReservation.search(
                [("sale_order_id", "=", order.id)],
                order="id desc",
                limit=1,
            )
            if not reservation:
                reservation = SaleReservation.create(
                    {"sale_order_id": order.id, "reservation_state": "cancelled"}
                )
            cr.execute(
                "UPDATE reservation_cash_in SET sale_reservation_id = %s WHERE id = %s",
                (reservation.id, cash_in_id),
            )

        # Legacy link removed after migration.
        cr.execute("ALTER TABLE reservation_cash_in DROP COLUMN IF EXISTS reservation_id")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("reservation.cash.in") or "New"
        records = super().create(vals_list)
        for rec in records:
            rec._check_total_not_exceed_quotation_limit()
        return records

    @api.constrains("amount")
    def _check_amount(self):
        for rec in self:
            if rec.amount <= 0:
                raise UserError(_("Amount must be strictly positive."))

    @api.constrains(
        "payment_mode_id",
        "is_cheque_lcn",
        "cheque_lcn_date",
        "cheque_lcn_issuer",
        "cheque_lcn_number",
    )
    def _check_cheque_lcn_required_fields(self):
        for rec in self:
            if rec.is_cheque_lcn and (
                not rec.cheque_lcn_date
                or not rec.cheque_lcn_issuer
                or not rec.cheque_lcn_number
            ):
                raise UserError(
                    _(
                        "Cheque/LCN details are required: Date, Issuer and Number."
                    )
                )

    @api.constrains("sale_reservation_id", "amount", "state")
    def _check_total_not_exceed_quotation(self):
        for rec in self:
            rec._check_total_not_exceed_quotation_limit()

    def _check_total_not_exceed_quotation_limit(self, target_state=None):
        for rec in self:
            effective_state = target_state or rec.state
            if not rec.sale_reservation_id or effective_state in self._NON_COUNTED_STATES:
                continue
            reservation = rec.sale_reservation_id
            # Cash collection is tied to the reservation itself, not to
            # whichever order currently happens to carry it. The ceiling is
            # the reservation's amount frozen at creation time, and the
            # running total is scoped to the reservation so it stays correct
            # even if the reservation later ends up linked to more than one
            # order (e.g. an invoice-to-another-contact delivery split).
            sibling_total = sum(
                self.search(
                    [
                        ("sale_reservation_id", "=", reservation.id),
                        ("id", "!=", rec.id),
                        ("state", "not in", list(self._NON_COUNTED_STATES)),
                    ]
                ).mapped("amount")
            )
            allowed_total = reservation.amount_total or 0.0
            if sibling_total + rec.amount > allowed_total + 1e-6:
                raise UserError(
                    _(
                        "Total cash collections excluding New and Cancelled cannot exceed the quotation total."
                    )
                )

    def write(self, vals):
        if "state" in vals and vals["state"]:
            for rec in self:
                rec._check_total_not_exceed_quotation_limit(target_state=vals["state"])
        locked_fields = {"amount", "payment_mode_id", "date", "sale_reservation_id"}
        if locked_fields.intersection(vals):
            for rec in self:
                if rec.state in {"confirmed", "to_account", "accounted"}:
                    raise UserError(_("Critical fields are locked after confirmation."))
        result = super().write(vals)
        self._check_total_not_exceed_quotation_limit()
        return result

    def action_confirm(self):
        for rec in self:
            if rec.state != "draft":
                continue
            rec._check_total_not_exceed_quotation_limit(target_state="confirmed")
            rec.state = "confirmed"

    def action_to_account(self):
        for rec in self:
            if rec.state != "confirmed":
                raise UserError(_("Only confirmed cash collections can be marked to account."))
            rec._check_total_not_exceed_quotation_limit(target_state="to_account")
            rec.state = "to_account"

    def action_mark_to_account(self):
        # Backward-compatible alias kept for existing database button bindings.
        return self.action_to_account()

    def action_cancel(self):
        for rec in self:
            if rec.state == "accounted":
                raise UserError(_("Cannot cancel a cash collection already accounted."))
            rec.state = "cancelled"

    def action_reset_draft(self):
        for rec in self:
            if rec.state == "accounted":
                raise UserError(_("Cannot reset to draft a cash collection already accounted."))
            rec.state = "draft"

    def _check_can_account(self):
        if not self.env.user.has_group("stock_customer_reservation_cash.group_reservation_cash_manager"):
            raise AccessError(_("Only a manager can account a cash collection."))

    def _get_or_create_receivable_account(self, company):
        account = self.env["account.account"].search(
            [("account_type", "=", "asset_receivable"), ("company_ids", "in", [company.id])],
            limit=1,
        )
        if account:
            return account

        existing_codes = set(
            self.env["account.account"].search_read(
                [("company_ids", "in", [company.id])], ["code"], limit=5000
            )
            and [row["code"] for row in self.env["account.account"].search_read(
                [("company_ids", "in", [company.id])], ["code"], limit=5000
            )]
        )
        base_code = "411000"
        code = base_code
        i = 1
        while code in existing_codes:
            code = f"{base_code}{i}"
            i += 1

        return self.env["account.account"].create(
            {
                "name": _("Customers"),
                "code": code,
                "account_type": "asset_receivable",
                "company_ids": [(6, 0, [company.id])],
            }
        )

    def action_account(self):
        self._check_can_account()
        for rec in self:
            if rec.state != "to_account":
                raise UserError(_("State must be 'To Account'."))
            rec._check_total_not_exceed_quotation_limit()
            if rec.account_payment_id:
                raise UserError(_("This cash collection is already accounted."))
            if not rec.payment_mode_id:
                raise UserError(_("Set a payment mode before accounting."))
            journal = rec.payment_mode_id.journal_id
            if not journal:
                raise UserError(_("Set a journal on the payment mode."))
            method_line = rec.payment_mode_id.inbound_payment_method_line_id
            if not method_line:
                method_line = journal.inbound_payment_method_line_ids[:1]
            if not method_line:
                raise UserError(_("No inbound payment method is configured on the journal."))
            if hasattr(method_line, "payment_account_id") and not method_line.payment_account_id:
                payment_account = journal.default_account_id
                if not payment_account:
                    raise UserError(
                        _(
                            "The payment journal has no configured cash account. "
                            "Set a default account on the journal."
                        )
                    )
                method_line.payment_account_id = payment_account.id

            if not rec.partner_id.with_company(rec.company_id).property_account_receivable_id:
                receivable = self._get_or_create_receivable_account(rec.company_id)
                rec.partner_id.with_company(rec.company_id).property_account_receivable_id = receivable.id

            payment_vals = {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": rec.partner_id.id,
                "amount": rec.amount,
                "currency_id": rec.currency_id.id,
                "date": rec.date,
                "journal_id": journal.id,
                "company_id": rec.company_id.id,
                "memo": rec.reference or rec.name,
                "payment_method_line_id": method_line.id,
            }

            try:
                payment = self.env["account.payment"].create(payment_vals)
                payment.action_post()
                rec.write(
                    {
                        "account_payment_id": payment.id,
                        "accounting_date": fields.Date.context_today(self),
                        "accounted_by": self.env.user.id,
                        "state": "accounted",
                    }
                )
                continue
            except Exception as exc:
                msg = (str(exc) or "").lower()
                fallback_markers = (
                    "outstanding account",
                    "missing required account",
                    "compte",
                )
                if not any(marker in msg for marker in fallback_markers):
                    raise

            receivable_account = rec.partner_id.with_company(rec.company_id).property_account_receivable_id
            liquidity_account = method_line.payment_account_id or journal.default_account_id
            if not receivable_account or not liquidity_account:
                raise UserError(
                    _(
                        "Incomplete accounting configuration: missing customer or journal account."
                    )
                )

            try:
                move = self.env["account.move"].create(
                    {
                        "date": rec.date,
                        "ref": rec.reference or rec.name,
                        "journal_id": journal.id,
                        "company_id": rec.company_id.id,
                        "line_ids": [
                            (
                                0,
                                0,
                                {
                                    "name": rec.reference or rec.name,
                                    "account_id": liquidity_account.id,
                                    "debit": rec.amount,
                                    "credit": 0.0,
                                    "partner_id": rec.partner_id.id,
                                },
                            ),
                            (
                                0,
                                0,
                                {
                                    "name": rec.reference or rec.name,
                                    "account_id": receivable_account.id,
                                    "debit": 0.0,
                                    "credit": rec.amount,
                                    "partner_id": rec.partner_id.id,
                                },
                            ),
                        ],
                    }
                )
            except Exception as move_exc:
                raise UserError(_("Automatic accounting failed: %s") % move_exc) from move_exc
            move.action_post()
            rec.write(
                {
                    "account_move_id": move.id,
                    "accounting_date": fields.Date.context_today(self),
                    "accounted_by": self.env.user.id,
                    "state": "accounted",
                }
            )

    def action_mark_accounted(self):
        # Backward-compatible alias kept for existing database button bindings.
        return self.action_account()
