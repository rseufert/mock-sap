"""Creating the documents that follow a sales order.

A delivery, an invoice and the journal entry it posts. Three callers need
these - the BAPIs, the IDoc generators and an inbound delivery - and a
document created one way has to look like one created another, so the
creation lives here rather than in whichever layer needed it first.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Tuple

from . import db, store
from .odata import SapError
from .schema import ENTITY_TYPES


def _today() -> str:
    return _dt.datetime.utcnow().replace(hour=0, minute=0, second=0,
                                         microsecond=0).isoformat()


def sales_order(ctx, number: str):
    """Find a sales order by its number, padded or not."""
    so = ENTITY_TYPES["A_SalesOrder"]
    return (store.get(ctx.conn, so, {"SalesOrder": str(number).zfill(10)})
            or store.get(ctx.conn, so, {"SalesOrder": str(number)}))


def order_items(ctx, order) -> List[Any]:
    so = ENTITY_TYPES["A_SalesOrder"]
    return store.children(ctx.conn, order, so, so.nav("to_Item"))


def create_delivery(ctx, order, lines: List[Tuple[Any, float]]) -> str:
    """Create an outbound delivery for `lines`, each an order item and a quantity."""
    delivery = db.next_number(ctx.conn, "DELIVERY", 10)
    today = _today()
    weight = sum(quantity for _item, quantity in lines)
    store.insert(ctx.conn, ENTITY_TYPES["A_OutbDeliveryHeader"], {
        "DeliveryDocument": delivery, "DeliveryDocumentType": "LF",
        "ShippingPoint": "1710", "SalesOrganization": order["SalesOrganization"],
        "SoldToParty": order["SoldToParty"], "ShipToParty": order["SoldToParty"],
        "DeliveryDate": today, "ActualGoodsMovementDate": today,
        "OverallSDProcessStatus": "C", "OverallGoodsMovementStatus": "C",
        "TotalWeight": round(weight, 3), "WeightUnit": "KG",
    }, user=ctx.user)

    for item, quantity in lines:
        store.insert(ctx.conn, ENTITY_TYPES["A_OutbDeliveryItem"], {
            "DeliveryDocument": delivery,
            "DeliveryDocumentItem": item["SalesOrderItem"],
            "Material": item["Material"],
            "DeliveryDocumentItemText": item["SalesOrderItemText"],
            "ActualDeliveredQtyInOrderQtyUnit": quantity,
            "OrderQuantityUnit": item["RequestedQuantityUnit"],
            "Plant": item["Plant"], "StorageLocation": "1710",
            "ReferenceSDDocument": order["SalesOrder"],
            "ReferenceSDDocumentItem": item["SalesOrderItem"],
            "ItemGrossWeight": round(quantity, 3), "ItemWeightUnit": "KG",
        }, user=ctx.user)

    apply_delivery_status(ctx, order,
                          {item["SalesOrderItem"]: quantity for item, quantity in lines})
    return delivery


def apply_delivery_status(ctx, order, delivered: Dict[str, float]) -> str:
    """Move the order's delivery status to fully or partly delivered."""
    items = order_items(ctx, order)
    complete = bool(items)
    for item in items:
        wanted = float(item["RequestedQuantity"] or 0)
        if delivered.get(item["SalesOrderItem"], 0.0) + 1e-9 < wanted:
            complete = False
            break
    status = "C" if complete else "B"
    store.update(ctx.conn, ENTITY_TYPES["A_SalesOrder"],
                 {"SalesOrder": order["SalesOrder"]},
                 {"OverallDeliveryStatus": status}, user=ctx.user)
    return status


def post_journal_entry(ctx, header: Dict[str, Any],
                       lines: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    """Post a journal entry.  `lines` carry a signed amount: + debit, - credit.

    Returns (document, company code, fiscal year).  The caller is expected to
    have checked that the lines balance; `balance_of` is here for that.
    """
    company = str(header.get("CompanyCode") or "1710")
    posting = str(header.get("PostingDate") or _today())
    year = posting[:4]
    document = db.next_number(ctx.conn, "ACCOUNTINGDOCUMENT", 10)

    store.insert(ctx.conn, ENTITY_TYPES["A_JournalEntry"], {
        "AccountingDocument": document, "CompanyCode": company, "FiscalYear": year,
        "AccountingDocumentType": header.get("AccountingDocumentType") or "SA",
        "DocumentDate": header.get("DocumentDate") or posting,
        "PostingDate": posting, "FiscalPeriod": posting[5:7].zfill(3),
        "TransactionCurrency": header.get("TransactionCurrency") or "EUR",
        "AccountingDocumentHeaderText": header.get("HeaderText") or "",
        "ReferenceDocument": header.get("ReferenceDocument") or "",
    }, user=ctx.user)

    for index, line in enumerate(lines, start=1):
        amount = float(line.get("Amount") or 0)
        store.insert(ctx.conn, ENTITY_TYPES["A_JournalEntryItem"], {
            "AccountingDocument": document, "CompanyCode": company, "FiscalYear": year,
            "AccountingDocumentItem": str(index).zfill(6),
            "GLAccount": line.get("GLAccount") or "",
            "DebitCreditCode": "S" if amount >= 0 else "H",
            "AmountInTransactionCurrency": abs(round(amount, 2)),
            "TransactionCurrency": line.get("TransactionCurrency")
            or header.get("TransactionCurrency") or "EUR",
            "DocumentItemText": line.get("Text") or "",
            "CostCenter": line.get("CostCenter") or "",
            "ProfitCenter": line.get("ProfitCenter") or "",
            "Customer": line.get("Customer") or "",
            "Supplier": line.get("Supplier") or "",
        }, user=ctx.user)
    return document, company, year


def balance_of(lines: List[Dict[str, Any]]) -> float:
    """What the debits and credits come to.  Zero, or the document is not postable."""
    return round(sum(float(line.get("Amount") or 0) for line in lines), 2)


def create_billing_document(ctx, order, items=None, tax_rate: float = 0.19
                            ) -> Dict[str, str]:
    """Invoice a sales order, and post the journal entry that follows from it."""
    items = order_items(ctx, order) if items is None else items
    billing = db.next_number(ctx.conn, "BILLINGDOCUMENT", 10)
    today = _today()
    currency = order["TransactionCurrency"] or "EUR"
    net = round(sum(float(item["NetAmount"] or 0) for item in items), 2)
    tax = round(net * tax_rate, 2)

    store.insert(ctx.conn, ENTITY_TYPES["A_BillingDocument"], {
        "BillingDocument": billing, "BillingDocumentType": "F2",
        "SDDocumentCategory": "M", "SalesOrganization": order["SalesOrganization"],
        "SoldToParty": order["SoldToParty"], "PayerParty": order["SoldToParty"],
        "BillingDocumentDate": today, "TransactionCurrency": currency,
        "TotalNetAmount": net, "TotalTaxAmount": tax,
        "TotalGrossAmount": round(net + tax, 2), "AccountingPostingStatus": "C",
    }, user=ctx.user)

    for item in items:
        amount = round(float(item["NetAmount"] or 0), 2)
        store.insert(ctx.conn, ENTITY_TYPES["A_BillingDocumentItem"], {
            "BillingDocument": billing, "BillingDocumentItem": item["SalesOrderItem"],
            "Material": item["Material"],
            "BillingDocumentItemText": item["SalesOrderItemText"],
            "BillingQuantity": item["RequestedQuantity"],
            "BillingQuantityUnit": item["RequestedQuantityUnit"],
            "NetAmount": amount, "TaxAmount": round(amount * tax_rate, 2),
            "TransactionCurrency": currency,
            "SalesDocument": order["SalesOrder"],
            "SalesDocumentItem": item["SalesOrderItem"],
        }, user=ctx.user)

    document, company, year = post_journal_entry(ctx, {
        "CompanyCode": "1710", "AccountingDocumentType": "RV", "PostingDate": today,
        "TransactionCurrency": currency, "ReferenceDocument": billing,
        "HeaderText": "Invoice %s" % billing,
    }, [
        {"GLAccount": "0012100000", "Amount": round(net + tax, 2),
         "Text": "Receivable", "Customer": order["SoldToParty"]},
        {"GLAccount": "0041000000", "Amount": -net, "Text": "Revenue",
         "ProfitCenter": "YB110"},
        {"GLAccount": "0022000000", "Amount": -tax, "Text": "Output tax"},
    ])
    store.update(ctx.conn, ENTITY_TYPES["A_BillingDocument"],
                 {"BillingDocument": billing},
                 {"AccountingDocument": document}, user=ctx.user)

    return {"billing_document": billing, "accounting_document": document,
            "company_code": company, "fiscal_year": year,
            "net": net, "tax": tax, "gross": round(net + tax, 2)}
