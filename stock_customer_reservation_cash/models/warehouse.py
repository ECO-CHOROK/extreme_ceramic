from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    reservation_parent_location_id = fields.Many2one(
        "stock.location",
        domain="[('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        string="Customer Reservations Parent Location",
    )
    reserved_location_id = fields.Many2one(
        "stock.location",
        domain="[('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        string="Customer Reserved Location",
    )
    reservation_internal_picking_type_id = fields.Many2one(
        "stock.picking.type",
        domain="[('warehouse_id', '=', id), ('code', '=', 'internal')]",
        string="Reservation Transfer Type",
    )
    reservation_release_picking_type_id = fields.Many2one(
        "stock.picking.type",
        domain="[('warehouse_id', '=', id), ('code', '=', 'internal')]",
        string="Release Transfer Type",
    )
