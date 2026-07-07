from odoo import _, fields, models
from odoo.exceptions import UserError


class DeliveryContactWizard(models.TransientModel):
    _name = "delivery.contact.wizard"
    _description = "Delivery Validation Wizard with Billing Contact"

    picking_id = fields.Many2one("stock.picking", required=True, readonly=True)
    invoice_to_other = fields.Boolean(string="Invoice to another contact")
    contact_id = fields.Many2one("res.partner", string="Contact")

    def action_confirm(self):
        self.ensure_one()
        if self.invoice_to_other and not self.contact_id:
            raise UserError(_("Select a contact when 'Invoice to another contact' is checked."))
        self.picking_id.action_validate_with_delivery_contact(
            invoice_to_other=self.invoice_to_other, contact_id=self.contact_id.id
        )
        return {"type": "ir.actions.act_window_close"}
