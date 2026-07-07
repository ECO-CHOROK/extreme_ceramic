#!/usr/bin/env python3
import json
import os
import sys
import time
import xmlrpc.client
from pathlib import Path


def load_env(path: Path) -> None:
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def main():
    root = Path(__file__).resolve().parent
    load_env(root / ".env")
    url = os.environ["ODOO_URL"].rstrip("/")
    db = os.environ["ODOO_DB"]
    login = os.environ["ODOO_ADMIN_LOGIN"]
    password = os.environ["ODOO_ADMIN_PASSWORD"]

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, login, password, {})
    if not uid:
        raise RuntimeError("Authentication failed")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def call(model, method, *args, **kwargs):
        return models.execute_kw(db, uid, password, model, method, list(args), kwargs or {})

    stamp = int(time.time())
    tag = f"BCREC{stamp}"
    sol_fields = call("sale.order.line", "fields_get", [], attributes=["type"])
    pol_fields = call("purchase.order.line", "fields_get", [], attributes=["type"])
    picking_fields = call("stock.picking", "fields_get", [], attributes=["type"])
    user_fields = call("res.users", "fields_get", [], attributes=["type"])
    sol_tax_field = next((f for f in ("tax_ids", "tax_id", "taxes_id", "taxes_ids") if f in sol_fields), None)
    pol_tax_field = next((f for f in ("tax_ids", "tax_id", "taxes_id", "taxes_ids") if f in pol_fields), None)
    picking_moves_field = "move_ids_without_package" if "move_ids_without_package" in picking_fields else "move_ids"
    user_groups_field = "groups_id" if "groups_id" in user_fields else ("group_ids" if "group_ids" in user_fields else None)

    # Company and settings
    company_id = call("res.company", "search", [], limit=1)[0]
    call("res.company", "write", [company_id], {"x_bc_amount_display_mode": "ht"})

    # Taxes (reuse existing)
    sale_tax_ids = call("account.tax", "search", [["type_tax_use", "=", "sale"], ["active", "=", True]], limit=1)
    purchase_tax_ids = call("account.tax", "search", [["type_tax_use", "=", "purchase"], ["active", "=", True]], limit=1)

    # Partners
    customer_id = call("res.partner", "create", {"name": f"{tag} Customer"})
    vendor_id = call("res.partner", "create", {"name": f"{tag} Vendor", "supplier_rank": 1})

    # Product
    categ_id = call("product.category", "search", [], limit=1)[0]
    product_tmpl_id = call(
        "product.template",
        "create",
        {
            "name": f"{tag} P1",
            "type": "consu",
            "categ_id": categ_id,
            "list_price": 100.0,
            "standard_price": 70.0,
            "sale_ok": True,
            "purchase_ok": True,
            "taxes_id": [(6, 0, sale_tax_ids)] if sale_tax_ids else False,
            "supplier_taxes_id": [(6, 0, purchase_tax_ids)] if purchase_tax_ids else False,
        },
    )
    product_id = call("product.product", "search", [["product_tmpl_id", "=", product_tmpl_id]], limit=1)[0]

    # Sale order + delivery
    so_line_vals = {
        "product_id": product_id,
        "product_uom_qty": 10,
        "price_unit": 100,
        "discount": 10,
    }
    if sol_tax_field and sale_tax_ids:
        so_line_vals[sol_tax_field] = [(6, 0, sale_tax_ids)]
    so_id = call(
        "sale.order",
        "create",
        {
            "partner_id": customer_id,
            "order_line": [(0, 0, so_line_vals)],
        },
    )
    call("sale.order", "action_confirm", [so_id])
    so = call("sale.order", "read", [so_id], fields=["name", "picking_ids"])[0]
    so_picking_id = (so.get("picking_ids") or [None])[0]

    # Purchase order + receipt
    po_line_vals = {
        "product_id": product_id,
        "product_qty": 8,
        "price_unit": 70,
        "date_planned": "2026-02-25 10:00:00",
    }
    if pol_tax_field and purchase_tax_ids:
        po_line_vals[pol_tax_field] = [(6, 0, purchase_tax_ids)]
    po_id = call(
        "purchase.order",
        "create",
        {
            "partner_id": vendor_id,
            "order_line": [(0, 0, po_line_vals)],
        },
    )
    call("purchase.order", "button_confirm", [po_id])
    po = call("purchase.order", "read", [po_id], fields=["name", "picking_ids"])[0]
    po_picking_id = (po.get("picking_ids") or [None])[0]

    # Internal transfer for D4 will be created via UI to avoid model-field differences in this DB.
    internal_picking_id = None

    # Batch transfers (one outgoing/one incoming if possible)
    batch_out_id = None
    batch_in_id = None
    if so_picking_id:
        batch_out_id = call("stock.picking.batch", "create", {"name": f"{tag} OUT", "picking_ids": [(6, 0, [so_picking_id])]})
    if po_picking_id:
        batch_in_id = call("stock.picking.batch", "create", {"name": f"{tag} IN", "picking_ids": [(6, 0, [po_picking_id])]})

    # Security users/group
    group_ids = call("res.groups", "search", [["name", "=", "Voir montants origine (BC) sur transferts"]], limit=1)
    if not group_ids:
        # fallback by partial name (translation/context issues)
        group_ids = call("res.groups", "search", [["name", "ilike", "BC"], ["name", "ilike", "transfert"]], limit=1)
    group_id = group_ids[0] if group_ids else None

    base_user_group = call("res.groups", "search", [["name", "=", "Internal User"]], limit=1)
    user_type_group = call("res.groups", "search", [["name", "=", "Employee"]], limit=1)
    default_groups = [g for g in [*(base_user_group or []), *(user_type_group or [])] if g]

    users = {}
    try:
        for key, with_group in [("usera", True), ("userb", False)]:
            login_val = f"{tag.lower()}.{key}"
            vals = {
                "name": f"{tag} {key.upper()}",
                "login": login_val,
                "password": "Test1234!",
                "company_id": company_id,
                "company_ids": [(6, 0, [company_id])],
            }
            if user_groups_field and (default_groups or (with_group and group_id)):
                gids = list(default_groups)
                if with_group and group_id:
                    gids.append(group_id)
                vals[user_groups_field] = [(6, 0, gids)]
            user_id = call("res.users", "create", vals)
            users[key] = {"id": user_id, "login": login_val, "password": "Test1234!"}
    except Exception as user_exc:
        users = {"error": str(user_exc)}

    result = {
        "tag": tag,
        "company_id": company_id,
        "product_tmpl_id": product_tmpl_id,
        "product_id": product_id,
        "customer_id": customer_id,
        "vendor_id": vendor_id,
        "sale_order_id": so_id,
        "sale_order_name": so["name"],
        "sale_picking_id": so_picking_id,
        "purchase_order_id": po_id,
        "purchase_order_name": po["name"],
        "purchase_picking_id": po_picking_id,
        "internal_picking_id": internal_picking_id,
        "batch_out_id": batch_out_id,
        "batch_in_id": batch_in_id,
        "group_id": group_id,
        "users": users,
    }
    out = root / "recette_context.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"Created recette data context in {out.name} (tag={tag})")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
