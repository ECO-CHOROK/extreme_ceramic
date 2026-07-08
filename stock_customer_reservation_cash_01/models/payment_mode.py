from odoo import fields, models


class ReservationPaymentMode(models.Model):
    _name = "reservation.payment.mode"
    _description = "Reservation Payment Mode"

    name = fields.Char(required=True)
    code = fields.Char()
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    journal_id = fields.Many2one(
        "account.journal", domain="[('company_id', '=', company_id), ('type', 'in', ['cash', 'bank'])]"
    )
    inbound_payment_method_line_id = fields.Many2one(
        "account.payment.method.line",
        domain="[('journal_id', '=', journal_id), ('payment_type', '=', 'inbound')]",
    )
    clearing_policy = fields.Selection(
        [
            ("immediate", "Immediate"),
            ("needs_clearance", "Needs Clearance"),
        ],
        default="immediate",
        required=True,
    )
    active = fields.Boolean(default=True)
    is_cheque_lcn = fields.Boolean(string="Chèque/LCN")

    _sql_constraints = [
        (
            "reservation_payment_mode_code_company_uniq",
            "unique(code, company_id)",
            "The code must be unique per company.",
        ),
    ]
