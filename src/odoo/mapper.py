from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.odoo.client import OdooClient

logger = logging.getLogger(__name__)


@dataclass
class BatchOdooData:
    partners: dict[int, dict] = field(default_factory=dict)
    products: dict[int, dict] = field(default_factory=dict)
    variants: dict[int, dict] = field(default_factory=dict)


async def fetch_batch_data(
    client: OdooClient, orders: list[dict[str, Any]]
) -> BatchOdooData:
    partner_ids: set[int] = set()
    line_product_ids: set[int] = set()

    for order in orders:
        if order.get("partner_id"):
            pid = order["partner_id"]
            partner_ids.add(pid[0] if isinstance(pid, (list, tuple)) else pid)

        if order.get("partner_shipping_id"):
            sid = order["partner_shipping_id"]
            partner_ids.add(sid[0] if isinstance(sid, (list, tuple)) else sid)

    all_lines: list[dict] = []
    order_ids = [o["id"] for o in orders]
    if order_ids:
        all_lines = await client.search_read(
            "sale.order.line",
            [["order_id", "in", order_ids]],
            [
                "order_id",
                "product_id",
                "product_template_id",
                "product_uom_qty",
                "price_unit",
                "price_subtotal",
                "price_total",
                "discount",
                "name",
            ],
        )

    for line in all_lines:
        if line.get("product_id"):
            pid = line["product_id"]
            line_product_ids.add(pid[0] if isinstance(pid, (list, tuple)) else pid)

    batch = BatchOdooData()

    if partner_ids:
        partners_data = await client.read(
            "res.partner",
            list(partner_ids),
            [
                "name",
                "email",
                "phone",
                "street",
                "street2",
                "city",
                "state_id",
                "zip",
                "country_id",
                "vat",
            ],
        )
        batch.partners = {p["id"]: p for p in partners_data}

    if line_product_ids:
        products_data = await client.read(
            "product.product",
            list(line_product_ids),
            ["name", "default_code", "barcode", "product_tmpl_id"],
        )
        batch.products = {p["id"]: p for p in products_data}

        tmpl_ids = set()
        for p in products_data:
            if p.get("product_tmpl_id"):
                tid = p["product_tmpl_id"]
                tmpl_ids.add(tid[0] if isinstance(tid, (list, tuple)) else tid)

        if tmpl_ids:
            templates_data = await client.read(
                "product.template",
                list(tmpl_ids),
                ["name", "default_code"],
            )
            batch.variants = {t["id"]: t for t in templates_data}

    # Indexar lines por order_id
    lines_by_order: dict[int, list[dict]] = {}
    for line in all_lines:
        oid = line["order_id"]
        oid = oid[0] if isinstance(oid, (list, tuple)) else oid
        lines_by_order.setdefault(oid, []).append(line)

    # Guardar lines indexadas para uso posterior
    batch._lines_by_order = lines_by_order  # type: ignore[attr-defined]

    return batch


def _extract_id(val: Any) -> int | None:
    if isinstance(val, (list, tuple)) and val:
        return val[0]
    if isinstance(val, int) and val:
        return val
    return None


def _format_partner(partner: dict | None) -> dict:
    if not partner:
        return {}
    state = partner.get("state_id")
    country = partner.get("country_id")
    return {
        "name": partner.get("name", ""),
        "email": partner.get("email") or "",
        "phone": partner.get("phone") or "",
        "tax_id": partner.get("vat") or "",
        "address": {
            "street": partner.get("street") or "",
            "street2": partner.get("street2") or "",
            "city": partner.get("city") or "",
            "state": state[1] if isinstance(state, (list, tuple)) and state else "",
            "zip": partner.get("zip") or "",
            "country": country[1] if isinstance(country, (list, tuple)) and country else "",
        },
    }


def _resolve_sku(product: dict | None, template: dict | None, odoo_db: str, product_id: int) -> str:
    if product and product.get("default_code"):
        return product["default_code"]
    if product and product.get("barcode"):
        return product["barcode"]
    if template and template.get("default_code"):
        return template["default_code"]
    return f"ODOO-{odoo_db}-{product_id}"


def map_order_to_webhook_payload(
    order: dict[str, Any],
    batch: BatchOdooData,
    odoo_db: str,
    connection_id: str,
) -> dict[str, Any]:
    partner_id = _extract_id(order.get("partner_id"))
    shipping_id = _extract_id(order.get("partner_shipping_id"))

    customer = _format_partner(batch.partners.get(partner_id) if partner_id else None)
    shipping = _format_partner(batch.partners.get(shipping_id) if shipping_id else None)

    lines: list[dict] = getattr(batch, "_lines_by_order", {}).get(order["id"], [])
    items = []
    for line in lines:
        qty = line.get("product_uom_qty", 0)
        if qty == 0:
            continue

        prod_id = _extract_id(line.get("product_id"))
        product = batch.products.get(prod_id) if prod_id else None

        tmpl_id = None
        if product and product.get("product_tmpl_id"):
            tmpl_id = _extract_id(product["product_tmpl_id"])
        template = batch.variants.get(tmpl_id) if tmpl_id else None

        sku = _resolve_sku(product, template, odoo_db, prod_id or 0)

        items.append(
            {
                "sku": sku,
                "name": line.get("name", ""),
                "quantity": qty,
                "unit_price": line.get("price_unit", 0),
                "subtotal": line.get("price_subtotal", 0),
                "total": line.get("price_total", 0),
                "discount_percent": line.get("discount", 0),
                "odoo_product_id": prod_id,
            }
        )

    return {
        "source": "odoo",
        "connection_id": connection_id,
        "odoo_db": odoo_db,
        "order": {
            "id": order["id"],
            "name": order.get("name", ""),
            "state": order.get("state", ""),
            "date_order": order.get("date_order", ""),
            "write_date": order.get("write_date", ""),
            "amount_untaxed": order.get("amount_untaxed", 0),
            "amount_tax": order.get("amount_tax", 0),
            "amount_total": order.get("amount_total", 0),
            "currency": _extract_name(order.get("currency_id")),
            "note": order.get("note") or "",
        },
        "customer": customer,
        "shipping_address": shipping if shipping else customer,
        "items": items,
    }


def _extract_name(val: Any) -> str:
    if isinstance(val, (list, tuple)) and len(val) > 1:
        return val[1]
    return ""
