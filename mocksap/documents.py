"""Creating the documents that follow a sales order.

A delivery, an invoice and the journal entry it posts. Three callers need
these - the BAPIs, the IDoc generators and an inbound delivery - and a
document created one way has to look like one created another, so the
creation lives here rather than in whichever layer needed it first.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Tuple

from . import clock, db, store
from .odata import SapError
from .schema import ENTITY_TYPES


def _today() -> str:
    return clock.now().replace(hour=0, minute=0, second=0,
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


# T052 in miniature. A supplier or customer line is due a number of days after
# its baseline date, and which number is configuration in a real system - so
# this table is deliberately short, and a document that carries its own
# NetPaymentDays (a purchase order does) should pass that instead of a key.
PAYMENT_TERMS = {
    "": 0,        # no terms: payable at once, as a blank ZTERM means in SAP
    "0001": 0,    # payable immediately due net
    "NT30": 30,
    "NT45": 45,
    "NT60": 60,
}

# SAP's KOART, defined in `store` next to the queries that select open items
# by it, and re-exported here because this is where lines are given one.
ITEM_TYPE_GL = store.ITEM_TYPE_GL
ITEM_TYPE_CUSTOMER = store.ITEM_TYPE_CUSTOMER
ITEM_TYPE_SUPPLIER = store.ITEM_TYPE_SUPPLIER


def net_due_date(baseline: str, terms: str = "", days=None) -> str:
    """When a line falls due: its baseline date plus the days its terms allow.

    ``days`` wins when the document carries its own net payment days, because
    that is the document's own answer rather than a lookup.
    """
    allowed = int(days) if days not in (None, "") else PAYMENT_TERMS.get(
        (terms or "").strip().upper(), 0)
    start = _dt.date.fromisoformat(str(baseline)[:10])
    return (start + _dt.timedelta(days=allowed)).isoformat()


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
        # The journal entry service writes the fields it publishes; the open
        # item view writes the ones it publishes. Same row, same keys - each
        # write checked against a type that actually declares its fields,
        # rather than letting either service store what it does not serve.
        store.update(ctx.conn, ENTITY_TYPES["A_OperationalAcctgDocItemCube"], {
            "AccountingDocument": document, "CompanyCode": company,
            "FiscalYear": year, "AccountingDocumentItem": str(index).zfill(6),
        }, _open_item(line, posting), user=ctx.user)
    return document, company, year


def _open_item(line: Dict[str, Any], posting: str) -> Dict[str, Any]:
    """What an open item carries the moment it is posted, and nothing more.

    A G/L line is not owed to anyone, so it has no due date and no clearing.
    A supplier or customer line is open from the start: it has a date it falls
    due, and the clearing fields stay empty until something pays it. Empty is
    the whole test - clients select open items by asking for a blank clearing
    document, so this must be blank rather than absent.
    """
    supplier = line.get("Supplier") or ""
    customer = line.get("Customer") or ""
    if supplier:
        item_type = ITEM_TYPE_SUPPLIER
    elif customer:
        item_type = ITEM_TYPE_CUSTOMER
    else:
        item_type = ITEM_TYPE_GL
    state = {
        "AccountingDocumentItemType": item_type,
        "PostingDate": posting,
        "PaymentTerms": "",
        "PaymentBlockingReason": "",
        "NetDueDate": None,
        "ClearingAccountingDocument": "",
        "ClearingDate": None,
        "ClearingCreationDate": None,
        "ClearingItem": "",
        "ClearingDocFiscalYear": "",
        "ClearingIsReversed": False,
    }
    if item_type == ITEM_TYPE_GL:
        return state
    terms = str(line.get("PaymentTerms") or "")
    state["PaymentTerms"] = terms
    state["PaymentBlockingReason"] = str(line.get("PaymentBlockingReason") or "")
    state["NetDueDate"] = net_due_date(
        str(line.get("DueCalculationBaseDate") or posting), terms,
        line.get("NetPaymentDays"))
    return state


def post_supplier_invoice(ctx, invoice: Dict[str, Any]) -> Dict[str, Any]:
    """File a supplier invoice and post the payable it creates.

    This is the half of the document chain that runs the other way. A sales
    order becomes a delivery, an invoice and money owed *to* us; a supplier's
    invoice becomes an accounting document and money owed *by* us, and that
    payable is what a payment run later selects, pays and clears.

    The invoice is recorded whatever it says; the open item is what makes it
    payable. Nothing here checks the invoice against a purchase order - that
    is the payer's job, and `examples/invoice_check.py` does it. A mock that
    silently refused a mismatched invoice would hide the bug its user is
    looking for.
    """
    supplier = str(invoice.get("supplier") or "")
    if not supplier:
        raise SapError("An invoice with no invoicing party cannot be posted", 400)

    posting = str(invoice.get("posting_date") or _today())[:10]
    year = posting[:4]
    currency = str(invoice.get("currency") or "EUR")
    gross = round(float(invoice.get("gross") or 0), 2)
    net = round(float(invoice.get("net") or gross), 2)
    tax = round(float(invoice.get("tax") if invoice.get("tax") is not None
                      else gross - net), 2)
    terms = str(invoice.get("terms") or "")
    baseline = str(invoice.get("baseline_date") or posting)[:10]

    # The payable is a credit to the supplier; the expense and the input tax
    # are the debits that balance it. Same shape as the entry a real invoice
    # posts, which is what makes the open item look like a real one.
    lines = [{
        "Supplier": supplier,
        "Amount": -gross,
        "Text": "Invoice %s" % (invoice.get("reference") or ""),
        "PaymentTerms": terms,
        "DueCalculationBaseDate": baseline,
        "PaymentBlockingReason": str(invoice.get("payment_block") or ""),
        "NetPaymentDays": invoice.get("net_payment_days"),
        "TransactionCurrency": currency,
    }, {
        "GLAccount": "0000400000", "Amount": net, "Text": "Expense",
        "TransactionCurrency": currency,
    }]
    if tax:
        lines.append({"GLAccount": "0000154000", "Amount": tax,
                      "Text": "Input tax", "TransactionCurrency": currency})

    document, company, fiscal = post_journal_entry(ctx, {
        "CompanyCode": invoice.get("company_code") or "1710",
        "AccountingDocumentType": "RE",      # a supplier invoice, in FI terms
        "DocumentDate": invoice.get("document_date") or posting,
        "PostingDate": posting,
        "TransactionCurrency": currency,
        "HeaderText": "Invoice %s" % (invoice.get("reference") or ""),
        "ReferenceDocument": str(invoice.get("reference") or ""),
    }, lines)

    number = db.next_number(ctx.conn, "SUPPLIERINVOICE", 10)
    store.insert(ctx.conn, ENTITY_TYPES["A_SupplierInvoice"], {
        "SupplierInvoice": number, "FiscalYear": fiscal, "CompanyCode": company,
        "InvoicingParty": supplier,
        "SupplierInvoiceIDByInvcgParty": str(invoice.get("reference") or ""),
        "DocumentDate": str(invoice.get("document_date") or posting)[:10],
        "PostingDate": posting,
        "InvoiceGrossAmount": gross,
        "DocumentCurrency": currency,
        "PaymentTerms": terms,
        "DueCalculationBaseDate": baseline,
        "NetPaymentDays": int(invoice.get("net_payment_days")
                              if invoice.get("net_payment_days") not in (None, "")
                              else PAYMENT_TERMS.get(terms.upper(), 0)),
        "PaymentBlockingReason": str(invoice.get("payment_block") or ""),
        "PaymentMethod": str(invoice.get("payment_method") or ""),
        # which account to pay into. A supplier's first account unless the
        # invoice named another; see A_BusinessPartnerBank.
        "BPBankAccountInternalID": str(invoice.get("bank_details") or "0001"),
        "SupplierInvoiceStatus": "5",        # RBSTAT 5: posted
        "AccountingDocumentType": "RE",
        "SupplierInvoiceIsCreditMemo": "",
        "ReverseDocument": "", "ReverseDocumentFiscalYear": "",
        "AccountingDocument": document,
    }, user=ctx.user)

    for index, item in enumerate(invoice.get("items") or [], start=1):
        store.insert(ctx.conn, ENTITY_TYPES["A_SuplrInvcItemPurOrdRef"], {
            "SupplierInvoice": number, "FiscalYear": fiscal,
            "SupplierInvoiceItem": str(index).zfill(6),
            "PurchaseOrder": str(item.get("purchase_order") or ""),
            "PurchaseOrderItem": str(item.get("purchase_order_item") or ""),
            "DocumentCurrency": currency,
            "SupplierInvoiceItemAmount": round(float(item.get("amount") or 0), 2),
            "QuantityInPurchaseOrderUnit": float(item.get("quantity") or 0),
            "PurchaseOrderQuantityUnit": str(item.get("unit") or ""),
            "SupplierInvoiceItemText": str(item.get("text") or "")[:50],
        }, user=ctx.user)

    return {"supplier_invoice": number, "fiscal_year": fiscal,
            "accounting_document": document, "company_code": company,
            "gross": gross, "currency": currency}


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
