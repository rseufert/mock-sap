"""Creating the documents that follow a sales order.

A delivery, an invoice and the journal entry it posts. Three callers need
these - the BAPIs, the IDoc generators and an inbound delivery - and a
document created one way has to look like one created another, so the
creation lives here rather than in whichever layer needed it first.
"""
from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import clock, db, money, store
from .odata import SapError
from .schema import ENTITY_TYPES


# The places a quantity is carried to, taken from the scale the order item
# declares for one rather than repeated here.
QUANTITY_SCALE = money.scale_of(
    ENTITY_TYPES["A_SalesOrderItem"].prop("RequestedQuantity"))


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


def delivered_so_far(ctx, order) -> Dict[str, Decimal]:
    """How much of each item this order has had delivered, over every delivery.

    This is the fact `apply_delivery_status` and the over-delivery check both
    need, and it is a property of the *order* rather than of the delivery in
    hand. Read from the delivery items that reference the order, because they
    are the record: an order for 10 delivered as 5 and 5 has had 10, and
    asking only the delivery being created can never see that.
    """
    rows = ctx.conn.execute(
        'SELECT "ReferenceSDDocumentItem" position, '
        '"ActualDeliveredQtyInOrderQtyUnit" quantity '
        'FROM "A_OutbDeliveryItem" WHERE "ReferenceSDDocument" = ?',
        (order["SalesOrder"],)).fetchall()
    out: Dict[str, Decimal] = {}
    # Summed here rather than with SQL SUM, for the reason #98 gives: a
    # quantity is a decimal, and SQLite would add these up as doubles.
    for row in rows:
        position = str(row["position"] or "")
        out[position] = out.get(position, Decimal(0)) + money.of(row["quantity"])
    return out


def open_quantity(item, delivered: Optional[Dict[str, Decimal]] = None) -> Decimal:
    """What is still to be delivered on an item: ordered less delivered.

    Never negative. An item already over-delivered has nothing open rather
    than a negative amount open, because the question a caller is asking is
    how much more it may send.
    """
    done = (delivered or {}).get(item["SalesOrderItem"], Decimal(0))
    remaining = money.of(item["RequestedQuantity"]) - done
    return remaining if remaining > 0 else Decimal(0)


def over_delivery(item, delivered: Dict[str, Decimal], quantity) -> Optional[str]:
    """Why `quantity` more of `item` is more than the order allows, or None.

    SAP refuses past the item's over-delivery tolerance, so this mock does,
    and the tolerance is initial on a created item - by default an order
    receives what it asked for and no more. The sentence is returned rather
    than raised because each transport says it differently: an OData write
    gets a `SapError`, a BAPI a BAPIRET2 record, an inbound IDoc status 51.

    Worded as SAP's VL 367 is, in terms of what is open, because that is
    what a caller asked for and what it already read on the refusal before
    there was a tolerance to mention. The tolerance is named only when there
    is one, so saying "0.000 open" cannot look like a contradiction of an
    order quantity the caller can see.
    """
    if item["UnlimitedOverdeliveryIsAllowed"]:
        return None
    ordered = money.of(item["RequestedQuantity"])
    tolerance = money.of(item["OverdelivTolrtdLmtRatioInPct"])
    allowed = money.at(ordered * (1 + tolerance / 100), QUANTITY_SCALE)
    done = delivered.get(item["SalesOrderItem"], Decimal(0))
    if done + money.of(quantity) <= allowed:
        return None
    still_open = allowed - done
    refusal = ("Only %s %s are open for item %s"
               % (money.text(still_open if still_open > 0 else Decimal(0),
                             QUANTITY_SCALE),
                  item["RequestedQuantityUnit"], item["SalesOrderItem"]))
    if tolerance:
        refusal += " (%s ordered, %s%% tolerance)" % (
            money.text(ordered, QUANTITY_SCALE), money.text(tolerance, 1))
    return refusal


def create_delivery(ctx, order, lines: List[Tuple[Any, Any]]) -> str:
    """Create an outbound delivery for `lines`, each an order item and a quantity."""
    today = _today()
    lines = [(item, money.of(quantity)) for item, quantity in lines]
    already = delivered_so_far(ctx, order)
    for item, quantity in lines:
        refusal = over_delivery(item, already, quantity)
        if refusal:
            raise SapError(refusal, 400, target="ActualDeliveredQtyInOrderQtyUnit")
    # Only now: a number range that has handed out a number cannot take it
    # back, and a refused delivery should not consume one.
    delivery = db.next_number(ctx.conn, "DELIVERY", 10)
    weight = sum((quantity for _item, quantity in lines), Decimal(0))
    store.insert(ctx.conn, ENTITY_TYPES["A_OutbDeliveryHeader"], {
        "DeliveryDocument": delivery, "DeliveryDocumentType": "LF",
        "ShippingPoint": "1710", "SalesOrganization": order["SalesOrganization"],
        "SoldToParty": order["SoldToParty"], "ShipToParty": order["SoldToParty"],
        "DeliveryDate": today, "ActualGoodsMovementDate": today,
        "OverallSDProcessStatus": "C", "OverallGoodsMovementStatus": "C",
        "TotalWeight": weight, "WeightUnit": "KG",
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
            "ItemGrossWeight": quantity, "ItemWeightUnit": "KG",
        }, user=ctx.user)

    apply_delivery_status(ctx, order)
    return delivery


def delivery_status(items, delivered: Dict[str, Decimal]) -> str:
    """An order's `OverallDeliveryStatus`, given what it has had delivered.

    SAP's three values: `A` nothing yet, `B` partly, `C` fully.  The rule
    lives here rather than in whoever writes the column, because two writers
    deciding it separately is how an order comes to claim a status its own
    deliveries contradict - which the seeded data used to do.

    Quantities are compared exactly, because both sides are decimals: an
    order for 0.3 delivered in full is complete without the tolerance this
    used to carry, which was there because neither 0.3 was quite 0.3.
    """
    complete, anything = bool(items), False
    for item in items:
        done = delivered.get(item["SalesOrderItem"], Decimal(0))
        if done > 0:
            anything = True
        if done < money.of(item["RequestedQuantity"]):
            complete = False
    return "C" if complete else ("B" if anything else "A")


def apply_delivery_status(ctx, order) -> str:
    """Move the order's delivery status to what its deliveries add up to.

    Read from every delivery against the order rather than from the one
    being created. An order for 10 delivered as 5 and 5 is complete; asking
    only the delivery in hand, neither 5 covered it and the order never left
    `B`.
    """
    status = delivery_status(order_items(ctx, order),
                             delivered_so_far(ctx, order))
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


class Unbalanced(SapError):
    """The debits and credits a document would post do not come to zero."""


_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T.*)?$|(\d{4})(\d{2})(\d{2})$")


def as_date(value) -> _dt.date:
    """A date as a caller writes one: ``YYYY-MM-DD`` or SAP's ``YYYYMMDD``.

    Both, on every Python. `date.fromisoformat` reads the second only from
    3.11, so the same call posted on one interpreter and failed on another
    (#94); the shapes are spelled out here so the answer is this mock's and
    not the host's. Anything else is a `ValueError` that names what arrived.
    """
    match = _DATE.match(str(value).strip())
    if not match:
        raise ValueError("%r is not a date; write it as YYYYMMDD or YYYY-MM-DD"
                         % (value,))
    year, month, day = (int(part) for part in match.groups() if part is not None)
    try:
        return _dt.date(year, month, day)
    except ValueError:
        raise ValueError("%r is not a date in any calendar" % (value,))


def net_due_date(baseline: str, terms: str = "", days=None) -> str:
    """When a line falls due: its baseline date plus the days its terms allow.

    ``days`` wins when the document carries its own net payment days, because
    that is the document's own answer rather than a lookup.
    """
    allowed = int(days) if days not in (None, "") else PAYMENT_TERMS.get(
        (terms or "").strip().upper(), 0)
    return (as_date(baseline) + _dt.timedelta(days=allowed)).isoformat()


def post_journal_entry(ctx, header: Dict[str, Any],
                       lines: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    """Post a journal entry.  `lines` carry a signed amount: + debit, - credit.

    Returns (document, company code, fiscal year).  The caller is expected to
    have checked that the lines balance; `balance_of` is here for that.
    """
    company = str(header.get("CompanyCode") or "1710")
    posting = str(header.get("PostingDate") or _today())
    year = posting[:4]
    # Everything a line can get wrong is worked out before the first write: a
    # baseline date nobody can read used to be found on the line it belonged
    # to, with the header and the lines before it already in the database.
    states = [_open_item(line, posting) for line in lines]
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
        amount = money.at(line.get("Amount"), money.CURRENCY_SCALE)
        store.insert(ctx.conn, ENTITY_TYPES["A_JournalEntryItem"], {
            "AccountingDocument": document, "CompanyCode": company, "FiscalYear": year,
            "AccountingDocumentItem": str(index).zfill(6),
            "GLAccount": line.get("GLAccount") or "",
            "DebitCreditCode": "S" if amount >= 0 else "H",
            "AmountInTransactionCurrency": abs(amount),
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
        }, states[index - 1], user=ctx.user)
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
        "PaymentRunID": "",
        "PaymentRunDate": None,
        "DueCalculationBaseDate": None,
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
    # A line can arrive already claimed, because /_mock and a deep insert both
    # post items a test needs in that state; nothing else sets it on a post.
    state["PaymentRunID"] = str(line.get("PaymentRunID") or "")
    state["PaymentRunDate"] = line.get("PaymentRunDate")
    # Kept, not only used: the due date alone cannot say what it was counted
    # from once a document carries its own net payment days, and the terms say
    # more than the net date (#182).
    baseline = str(line.get("DueCalculationBaseDate") or posting)
    state["DueCalculationBaseDate"] = as_date(baseline).isoformat()
    state["NetDueDate"] = net_due_date(baseline, terms,
                                       line.get("NetPaymentDays"))
    return state


def post_supplier_invoice(ctx, invoice: Dict[str, Any]) -> Dict[str, Any]:
    """File a supplier invoice and post the payable it creates.

    This is the half of the document chain that runs the other way. A sales
    order becomes a delivery, an invoice and money owed *to* us; a supplier's
    invoice becomes an accounting document and money owed *by* us, and that
    payable is what a payment run later selects, pays and clears.

    The invoice is recorded whatever it says about the purchase order; the
    open item is what makes it payable. Nothing here checks the invoice
    against a purchase order - that is the payer's job, and mock-acme's
    `invoice_check.py` does it. A mock that silently refused a mismatched
    invoice would hide the bug its user is looking for.

    What it says about itself is another matter. An invoice whose gross is not
    its net plus its tax, or whose items do not add up to its net, would post
    an accounting document that does not balance, and that is the one thing FI
    will not write: `Unbalanced`, before anything is written.
    """
    supplier = str(invoice.get("supplier") or "")
    if not supplier:
        raise SapError("An invoice with no invoicing party cannot be posted", 400)

    posting = str(invoice.get("posting_date") or _today())[:10]
    year = posting[:4]
    currency = str(invoice.get("currency") or "EUR")
    cents = money.CURRENCY_SCALE
    gross = money.at(invoice.get("gross"), cents)
    # Whichever of net and tax the invoice left out is what the other leaves
    # of the gross. Both stated is a claim, and it is checked below.
    stated_net, stated_tax = invoice.get("net"), invoice.get("tax")
    if stated_net in (None, ""):
        net = gross - money.at(stated_tax, cents)
    else:
        net = money.at(stated_net, cents)
    tax = (gross - net if stated_tax in (None, "")
           else money.at(stated_tax, cents))
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

    balance = balance_of(lines)
    if balance:
        raise Unbalanced(
            "Balance not zero: the total of %s %s is not the net of %s plus "
            "the tax of %s, which leaves %s" % (gross, currency, net, tax,
                                                -balance))
    # The items are what the net is made of. One that states no amount claims
    # nothing, so only an invoice whose items all say what they bill is held
    # to their sum.
    amounts = [item.get("amount") for item in invoice.get("items") or []]
    if amounts and all(amount not in (None, "") for amount in amounts):
        billed = sum((money.at(amount, cents) for amount in amounts), Decimal(0))
        if billed != net:
            raise Unbalanced(
                "Balance not zero: the items come to %s %s and the invoice "
                "states a net of %s, which leaves %s" % (billed, currency, net,
                                                         net - billed))

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
            "SupplierInvoiceItemAmount": money.at(item.get("amount"), cents),
            "QuantityInPurchaseOrderUnit": money.of(item.get("quantity")),
            "PurchaseOrderQuantityUnit": str(item.get("unit") or ""),
            "SupplierInvoiceItemText": str(item.get("text") or "")[:50],
        }, user=ctx.user)

    return {"supplier_invoice": number, "fiscal_year": fiscal,
            "accounting_document": document, "company_code": company,
            "gross": gross, "currency": currency}


def balance_of(lines: List[Dict[str, Any]]) -> Decimal:
    """What the debits and credits come to.  Zero, or the document is not postable.

    Added as decimals, so the answer is the lines' own arithmetic.  Added as
    floats it was not: three lines of 0.1 against one of 0.3 came to
    2.8e-17, and what kept that from refusing the document was the rounding
    to two places rather than anything about the sum - which held for the
    sizes a document has, and was a bet on them.
    """
    return money.at(sum((money.of(line.get("Amount")) for line in lines),
                        Decimal(0)), money.CURRENCY_SCALE)


# The one output tax rate this mock bills at: Germany's standard rate, which
# is what company code 1710 implies.  A real system reads it from the
# condition records a pricing procedure found, and nothing here does pricing.
TAX_RATE = Decimal("0.19")


def create_billing_document(ctx, order, items=None, tax_rate=TAX_RATE
                            ) -> Dict[str, Any]:
    """Invoice a sales order, and post the journal entry that follows from it."""
    items = order_items(ctx, order) if items is None else items
    billing = db.next_number(ctx.conn, "BILLINGDOCUMENT", 10)
    today = _today()
    cents = money.CURRENCY_SCALE
    rate = money.of(tax_rate)
    currency = order["TransactionCurrency"] or "EUR"
    net = money.at(sum((money.of(item["NetAmount"]) for item in items),
                       Decimal(0)), cents)
    # Half up, because that is what a tax authority specifies and what SAP
    # does: 19% of 2.50 is 0.4750 exactly, and so 0.48.  `round()` would make
    # it 0.47, both by rounding the half to even and by never seeing a half
    # in the first place - 2.50 * 0.19 is a shade under 0.475 in binary.
    tax = money.at(net * rate, cents)

    store.insert(ctx.conn, ENTITY_TYPES["A_BillingDocument"], {
        "BillingDocument": billing, "BillingDocumentType": "F2",
        "SDDocumentCategory": "M", "SalesOrganization": order["SalesOrganization"],
        "SoldToParty": order["SoldToParty"], "PayerParty": order["SoldToParty"],
        "BillingDocumentDate": today, "TransactionCurrency": currency,
        "TotalNetAmount": net, "TotalTaxAmount": tax,
        "TotalGrossAmount": net + tax, "AccountingPostingStatus": "C",
    }, user=ctx.user)

    for item in items:
        amount = money.at(item["NetAmount"], cents)
        store.insert(ctx.conn, ENTITY_TYPES["A_BillingDocumentItem"], {
            "BillingDocument": billing, "BillingDocumentItem": item["SalesOrderItem"],
            "Material": item["Material"],
            "BillingDocumentItemText": item["SalesOrderItemText"],
            "BillingQuantity": item["RequestedQuantity"],
            "BillingQuantityUnit": item["RequestedQuantityUnit"],
            "NetAmount": amount, "TaxAmount": money.at(amount * rate, cents),
            "TransactionCurrency": currency,
            "SalesDocument": order["SalesOrder"],
            "SalesDocumentItem": item["SalesOrderItem"],
        }, user=ctx.user)

    document, company, year = post_journal_entry(ctx, {
        "CompanyCode": "1710", "AccountingDocumentType": "RV", "PostingDate": today,
        "TransactionCurrency": currency, "ReferenceDocument": billing,
        "HeaderText": "Invoice %s" % billing,
    }, [
        # The terms come off the order because that is what the INVOIC we send
        # quotes as ZTERM: an invoice promising 2% 10 net 30 beside an open
        # item due the day it posted is a disagreement the client is right
        # about and this mock is wrong about (#182). No baseline date goes
        # with them - a billing document is due from the day it posts, which
        # is what `_open_item` counts from when a line names none.
        {"GLAccount": "0012100000", "Amount": net + tax,
         "Text": "Receivable", "Customer": order["SoldToParty"],
         "PaymentTerms": order["CustomerPaymentTerms"]},
        {"GLAccount": "0041000000", "Amount": -net, "Text": "Revenue",
         "ProfitCenter": "YB110"},
        {"GLAccount": "0022000000", "Amount": -tax, "Text": "Output tax"},
    ])
    store.update(ctx.conn, ENTITY_TYPES["A_BillingDocument"],
                 {"BillingDocument": billing},
                 {"AccountingDocument": document}, user=ctx.user)

    return {"billing_document": billing, "accounting_document": document,
            "company_code": company, "fiscal_year": year,
            "net": net, "tax": tax, "gross": net + tax}
