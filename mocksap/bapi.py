"""Mock RFC / BAPI layer.

Exposes a small set of well-known BAPIs over two transports:

* JSON   -  ``POST /sap/bc/rfc/BAPI_SALESORDER_CREATEFROMDAT2``
* SOAP   -  ``POST /sap/bc/srt/rfc/sap/<service>/<client>/<service>/<binding>``

Parameter and field names use the ABAP spellings (``ORDER_HEADER_IN``,
``ITM_NUMBER``, ...) and every function answers with a ``RETURN`` table of
BAPIRET2 records, exactly like the real thing.  Parameter lookup is
case/underscore insensitive, so the CamelCase spellings used by SAP's
SOAP "mc-style" services work as well.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from . import db, documents, store
from .odata import SapError
from .schema import ENTITY_TYPES

SOAP_NS = "urn:sap-com:document:sap:soap:functions:mc-style"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"

SYSTEM_ID = "MCK"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


def param(params: dict, name: str, default=None):
    """Fetch a parameter regardless of underscore/case spelling."""
    target = norm(name)
    for key, value in params.items():
        if norm(key) == target:
            return value
    return default


def field(struct, name, default=""):
    if not isinstance(struct, dict):
        return default
    value = param(struct, name, None)
    return default if value is None else value


def ret(msg_type: str, message: str, msg_id: str = "SR", number: str = "000",
        v1: str = "", v2: str = "", v3: str = "", v4: str = "",
        parameter: str = "", row: int = 0, fld: str = "") -> dict:
    """One BAPIRET2 record."""
    return {
        "TYPE": msg_type,
        "ID": msg_id,
        "NUMBER": number,
        "MESSAGE": message,
        "LOG_NO": "",
        "LOG_MSG_NO": "000000",
        "MESSAGE_V1": v1,
        "MESSAGE_V2": v2,
        "MESSAGE_V3": v3,
        "MESSAGE_V4": v4,
        "PARAMETER": parameter,
        "ROW": row,
        "FIELD": fld,
        "SYSTEM": SYSTEM_ID + "CLNT100",
    }


def _sap_date(value) -> str:
    if not value:
        return "0000-00-00"
    text = str(value)
    return text.split("T")[0]


def _num(value, default=0.0) -> float:
    try:
        return float(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Function modules
# --------------------------------------------------------------------------

FUNCTIONS: Dict[str, dict] = {}


def function(name: str, alias: str, bapiret: bool = True):
    """Register a function module.

    ``bapiret`` is False for the ones that are not BAPIs and so have no
    ``RETURN`` table: a real `RFC_READ_TABLE` reports trouble by raising an
    ABAP exception, not by filling a message table.
    """

    def wrap(fn):
        FUNCTIONS[name] = {"name": name, "alias": alias, "handler": fn,
                           "bapiret": bapiret}
        return fn

    return wrap


@function("RFC_PING", "RfcPing", bapiret=False)
def _rfc_ping(ctx, params):
    return {}


@function("STFC_CONNECTION", "StfcConnection", bapiret=False)
def _stfc_connection(ctx, params):
    text = param(params, "REQUTEXT", "")
    return {
        "ECHOTEXT": text,
        "RESPTEXT": "Mock SAP system %s, date %s, connection test successful"
                    % (SYSTEM_ID, _dt.date.today().isoformat()),
    }


@function("BAPI_SALESORDER_CREATEFROMDAT2", "SalesOrderCreateFromDat2")
def _so_create(ctx, params):
    header = param(params, "ORDER_HEADER_IN") or {}
    items = param(params, "ORDER_ITEMS_IN") or []
    partners = param(params, "ORDER_PARTNERS") or []
    testrun = str(param(params, "TESTRUN", "")).strip().upper() in ("X", "TRUE", "1")

    if not header:
        return {"SALESDOCUMENT": "", "RETURN": [
            ret("E", "Enter an order header", "V1", "021", parameter="ORDER_HEADER_IN")]}

    sold_to = ""
    for p in partners:
        if str(field(p, "PARTN_ROLE")).upper() in ("AG", "SP"):
            sold_to = str(field(p, "PARTN_NUMB")).lstrip("0") or field(p, "PARTN_NUMB")
    if not sold_to:
        return {"SALESDOCUMENT": "", "RETURN": [
            ret("E", "Please enter sold-to party", "V1", "301", parameter="ORDER_PARTNERS")]}

    bp = ENTITY_TYPES["A_BusinessPartner"]
    if store.get(ctx.conn, bp, {"BusinessPartner": sold_to}) is None:
        return {"SALESDOCUMENT": "", "RETURN": [
            ret("E", "Customer %s not found" % sold_to, "F2", "003", v1=sold_to,
                parameter="ORDER_PARTNERS", fld="PARTN_NUMB")]}

    messages: List[dict] = []
    payload_items = []
    product = ENTITY_TYPES["A_Product"]
    for index, item in enumerate(items, start=1):
        material = str(field(item, "MATERIAL")).strip()
        if material and store.get(ctx.conn, product, {"Product": material}) is None:
            messages.append(
                ret("E", "Material %s does not exist" % material, "V1", "033",
                    v1=material, row=index, parameter="ORDER_ITEMS_IN", fld="MATERIAL"))
            continue
        qty = _num(field(item, "REQ_QTY", field(item, "TARGET_QTY", 1)), 1.0)
        price = _num(field(item, "COND_VALUE", 0)) or round(qty * 100.0, 2)
        payload_items.append({
            "SalesOrderItem": str(field(item, "ITM_NUMBER", "")).strip() or None,
            "Material": material,
            "SalesOrderItemText": str(field(item, "SHORT_TEXT", "")),
            "RequestedQuantity": qty,
            "RequestedQuantityUnit": str(field(item, "SALES_UNIT", "PC")) or "PC",
            "NetAmount": price,
            "TransactionCurrency": str(field(header, "CURRENCY", "EUR")) or "EUR",
            "Plant": str(field(item, "PLANT", "")),
            "SalesOrderItemCategory": str(field(item, "ITEM_CATEG", "TAN")),
        })
    payload_items = [{k: v for k, v in it.items() if v is not None} for it in payload_items]

    if any(m["TYPE"] == "E" for m in messages):
        return {"SALESDOCUMENT": "", "RETURN": messages}

    if testrun:
        return {"SALESDOCUMENT": "", "RETURN": messages + [
            ret("S", "Test run: no document was created", "V1", "312")]}

    order = {
        "SalesOrderType": str(field(header, "DOC_TYPE", "OR")) or "OR",
        "SalesOrganization": str(field(header, "SALES_ORG", "1710")) or "1710",
        "DistributionChannel": str(field(header, "DISTR_CHAN", "10")) or "10",
        "OrganizationDivision": str(field(header, "DIVISION", "00")) or "00",
        "SoldToParty": sold_to,
        "PurchaseOrderByCustomer": str(field(header, "PURCH_NO_C", "")),
        "TransactionCurrency": str(field(header, "CURRENCY", "EUR")) or "EUR",
        "OverallSDProcessStatus": "A",
        "OverallDeliveryStatus": "A",
        "to_Item": payload_items,
    }
    keys = store.insert(ctx.conn, ENTITY_TYPES["A_SalesOrder"], order, user=ctx.user)
    doc = keys["SalesOrder"]
    for p in partners:
        role = str(field(p, "PARTN_ROLE", "AG")).upper()[:2]
        try:
            store.insert(ctx.conn, ENTITY_TYPES["A_SalesOrderHeaderPartner"], {
                "SalesOrder": doc, "PartnerFunction": role,
                "Customer": str(field(p, "PARTN_NUMB", "")).lstrip("0"),
            }, user=ctx.user)
        except SapError:
            pass
    messages.append(
        ret("S", "Standard Order %s has been saved" % doc.lstrip("0"), "V1", "311",
            v1="Standard Order", v2=doc.lstrip("0")))
    return {"SALESDOCUMENT": doc, "RETURN": messages}


@function("BAPI_SALESORDER_GETLIST", "SalesOrderGetList")
def _so_getlist(ctx, params):
    customer = str(param(params, "CUSTOMER_NUMBER", "") or "").lstrip("0")
    sales_org = str(param(params, "SALES_ORGANIZATION", "") or "")
    where, binds = [], []
    if customer:
        where.append('"SoldToParty" = ?')
        binds.append(customer)
    if sales_org:
        where.append('"SalesOrganization" = ?')
        binds.append(sales_org)
    rows = store.query(ctx.conn, ENTITY_TYPES["A_SalesOrder"], " AND ".join(where), binds)
    orders = [{
        "SD_DOC": r["SalesOrder"],
        "DOC_TYPE": r["SalesOrderType"],
        "SALES_ORG": r["SalesOrganization"],
        "DISTR_CHAN": r["DistributionChannel"],
        "DIVISION": r["OrganizationDivision"],
        "SOLD_TO": r["SoldToParty"],
        "PURCH_NO_C": r["PurchaseOrderByCustomer"],
        "DOC_DATE": _sap_date(r["SalesOrderDate"]),
        "NET_VALUE": "%.2f" % (r["TotalNetAmount"] or 0),
        "CURRENCY": r["TransactionCurrency"],
    } for r in rows]
    messages = [] if orders else [ret("W", "No sales orders found", "V1", "175")]
    return {"SALES_ORDERS": orders, "RETURN": messages}


@function("BAPI_SALESORDER_GETSTATUS", "SalesOrderGetStatus")
def _so_getstatus(ctx, params):
    doc = str(param(params, "SALESDOCUMENT", "") or "").strip()
    so = ENTITY_TYPES["A_SalesOrder"]
    row = store.get(ctx.conn, so, {"SalesOrder": doc.zfill(10)}) or store.get(ctx.conn, so, {"SalesOrder": doc})
    if row is None:
        return {"STATUSINFO": [], "RETURN": [
            ret("E", "Document %s does not exist" % doc, "V1", "555", v1=doc)]}
    items = store.children(ctx.conn, row, so, so.nav("to_Item"))
    info = [{
        "DOC_NUMBER": row["SalesOrder"],
        "ITM_NUMBER": it["SalesOrderItem"],
        "MATERIAL": it["Material"],
        "SHORT_TEXT": it["SalesOrderItemText"],
        "REQ_QTY": "%.3f" % (it["RequestedQuantity"] or 0),
        "SALES_UNIT": it["RequestedQuantityUnit"],
        "DLV_STAT": row["OverallDeliveryStatus"],
        "PROCESS_STAT": row["OverallSDProcessStatus"],
    } for it in items]
    return {"STATUSINFO": info, "RETURN": [
        ret("S", "Status for document %s read" % row["SalesOrder"], "V1", "311")]}


@function("BAPI_PO_CREATE1", "PurchaseOrderCreate1")
def _po_create(ctx, params):
    header = param(params, "POHEADER") or {}
    items = param(params, "POITEM") or []
    supplier = str(field(header, "VENDOR", "")).lstrip("0")
    bp = ENTITY_TYPES["A_BusinessPartner"]
    if not supplier:
        return {"EXPPURCHASEORDER": "", "RETURN": [
            ret("E", "Enter a vendor", "06", "056", parameter="POHEADER", fld="VENDOR")]}
    if store.get(ctx.conn, bp, {"BusinessPartner": supplier}) is None:
        return {"EXPPURCHASEORDER": "", "RETURN": [
            ret("E", "Vendor %s does not exist" % supplier, "06", "083", v1=supplier)]}

    po_items = []
    for index, item in enumerate(items, start=1):
        po_items.append({
            "PurchaseOrderItem": str(field(item, "PO_ITEM", "")).strip() or None,
            "Material": str(field(item, "MATERIAL", "")),
            "PurchaseOrderItemText": str(field(item, "SHORT_TEXT", "")),
            "Plant": str(field(item, "PLANT", "")),
            "StorageLocation": str(field(item, "STGE_LOC", "")),
            "OrderQuantity": _num(field(item, "QUANTITY", 1), 1.0),
            "PurchaseOrderQuantityUnit": str(field(item, "PO_UNIT", "PC")) or "PC",
            "NetPriceAmount": _num(field(item, "NET_PRICE", 0)),
            "NetPriceQuantity": _num(field(item, "PRICE_UNIT", 1), 1.0),
            "DocumentCurrency": str(field(header, "CURRENCY", "EUR")) or "EUR",
            "TaxCode": str(field(item, "TAX_CODE", "V1")),
        })
    po_items = [{k: v for k, v in it.items() if v is not None} for it in po_items]
    po = {
        "PurchaseOrderType": str(field(header, "DOC_TYPE", "NB")) or "NB",
        "CompanyCode": str(field(header, "COMP_CODE", "1710")) or "1710",
        "PurchasingOrganization": str(field(header, "PURCH_ORG", "1710")) or "1710",
        "PurchasingGroup": str(field(header, "PUR_GROUP", "001")) or "001",
        "Supplier": supplier,
        "DocumentCurrency": str(field(header, "CURRENCY", "EUR")) or "EUR",
        "Language": "EN",
        "PurchasingProcessingStatus": "02",
        "to_PurchaseOrderItem": po_items,
    }
    keys = store.insert(ctx.conn, ENTITY_TYPES["A_PurchaseOrder"], po, user=ctx.user)
    doc = keys["PurchaseOrder"]
    return {"EXPPURCHASEORDER": doc, "RETURN": [
        ret("S", "Standard PO created under the number %s" % doc, "06", "017", v1=doc)]}


@function("BAPI_PO_GETDETAIL1", "PurchaseOrderGetDetail1")
def _po_getdetail(ctx, params):
    doc = str(param(params, "PURCHASEORDER", "") or "").strip()
    po = ENTITY_TYPES["A_PurchaseOrder"]
    row = store.get(ctx.conn, po, {"PurchaseOrder": doc.zfill(10)}) or store.get(ctx.conn, po, {"PurchaseOrder": doc})
    if row is None:
        return {"POHEADER": {}, "POITEM": [], "RETURN": [
            ret("E", "Purchase order %s does not exist" % doc, "06", "055", v1=doc)]}
    items = store.children(ctx.conn, row, po, po.nav("to_PurchaseOrderItem"))
    return {
        "POHEADER": {
            "PO_NUMBER": row["PurchaseOrder"], "DOC_TYPE": row["PurchaseOrderType"],
            "COMP_CODE": row["CompanyCode"], "PURCH_ORG": row["PurchasingOrganization"],
            "PUR_GROUP": row["PurchasingGroup"], "VENDOR": row["Supplier"],
            "CURRENCY": row["DocumentCurrency"], "DOC_DATE": _sap_date(row["PurchaseOrderDate"]),
        },
        "POITEM": [{
            "PO_ITEM": it["PurchaseOrderItem"], "MATERIAL": it["Material"],
            "SHORT_TEXT": it["PurchaseOrderItemText"], "PLANT": it["Plant"],
            "QUANTITY": "%.3f" % (it["OrderQuantity"] or 0),
            "PO_UNIT": it["PurchaseOrderQuantityUnit"],
            "NET_PRICE": "%.2f" % (it["NetPriceAmount"] or 0),
        } for it in items],
        "RETURN": [ret("S", "Purchase order %s read" % row["PurchaseOrder"], "06", "017")],
    }


@function("BAPI_OUTB_DELIVERY_CREATE_SLS", "OutbDeliveryCreateSls")
def _delivery_create(ctx, params):
    lines = param(params, "SALES_ORDER_ITEMS") or []
    if not lines:
        return {"DELIVERY": "", "RETURN": [ret(
            "E", "Enter at least one sales order item", "VL", "561",
            parameter="SALES_ORDER_ITEMS")]}

    wanted: Dict[str, Dict[str, float]] = {}
    for index, line in enumerate(lines, start=1):
        order = str(field(line, "REF_DOC", "")).strip()
        item = str(field(line, "REF_ITEM", "")).strip()
        if not order or not item:
            return {"DELIVERY": "", "RETURN": [ret(
                "E", "Reference document and item are required", "VL", "561",
                row=index, parameter="SALES_ORDER_ITEMS")]}
        wanted.setdefault(order, {})[item] = _num(field(line, "DLV_QTY", 0))

    if len(wanted) > 1:
        return {"DELIVERY": "", "RETURN": [ret(
            "E", "All items must belong to one sales order in this mock", "VL", "561")]}

    number, quantities = list(wanted.items())[0]
    order = documents.sales_order(ctx, number)
    if order is None:
        return {"DELIVERY": "", "RETURN": [ret(
            "E", "Sales order %s does not exist" % number, "V1", "555", v1=number)]}

    items = {item["SalesOrderItem"]: item for item in documents.order_items(ctx, order)}
    selected, messages = [], []
    for index, (position, quantity) in enumerate(quantities.items(), start=1):
        item = items.get(position) or items.get(position.zfill(6))
        if item is None:
            messages.append(ret("E", "Item %s does not exist in order %s"
                                % (position, number), "V1", "555", row=index))
            continue
        open_quantity = float(item["RequestedQuantity"] or 0)
        quantity = quantity or open_quantity
        if quantity > open_quantity + 1e-9:
            messages.append(ret(
                "E", "Only %.3f %s are open for item %s"
                % (open_quantity, item["RequestedQuantityUnit"], item["SalesOrderItem"]),
                "VL", "367", row=index, fld="DLV_QTY"))
            continue
        selected.append((item, quantity))

    if any(m["TYPE"] == "E" for m in messages):
        return {"DELIVERY": "", "RETURN": messages}

    delivery = documents.create_delivery(ctx, order, selected)
    messages.append(ret("S", "Delivery %s has been saved" % delivery.lstrip("0"),
                        "VL", "311", v1=delivery.lstrip("0")))
    return {"DELIVERY": delivery, "RETURN": messages}


@function("BAPI_ACC_DOCUMENT_POST", "AccDocumentPost")
def _acc_document_post(ctx, params):
    header = param(params, "DOCUMENTHEADER") or {}
    accounts = param(params, "ACCOUNTGL") or []
    amounts = {str(field(a, "ITEMNO_ACC", "")).strip(): a
               for a in (param(params, "CURRENCYAMOUNT") or [])}

    company = str(field(header, "COMP_CODE", "")).strip()
    if not company:
        return {"OBJ_KEY": "", "RETURN": [ret(
            "E", "Enter a company code", "F5", "165", parameter="DOCUMENTHEADER",
            fld="COMP_CODE")]}
    if not accounts:
        return {"OBJ_KEY": "", "RETURN": [ret(
            "E", "Enter at least one line item", "F5", "166", parameter="ACCOUNTGL")]}

    lines, currency = [], "EUR"
    for index, account in enumerate(accounts, start=1):
        number = str(field(account, "ITEMNO_ACC", "")).strip()
        amount_row = amounts.get(number, {})
        currency = str(field(amount_row, "CURRENCY", currency)) or currency
        lines.append({
            "GLAccount": str(field(account, "GL_ACCOUNT", "")),
            "Amount": _num(field(amount_row, "AMT_DOCCUR", 0)),
            "Text": str(field(account, "ITEM_TEXT", "")),
            "CostCenter": str(field(account, "COSTCENTER", "")),
            "ProfitCenter": str(field(account, "PROFIT_CTR", "")),
            "Customer": str(field(account, "CUSTOMER", "")),
            "Supplier": str(field(account, "VENDOR_NO", "")),
            "TransactionCurrency": currency,
        })
        if number not in amounts:
            return {"OBJ_KEY": "", "RETURN": [ret(
                "E", "No amount was supplied for item %s" % number, "F5", "167",
                row=index, parameter="CURRENCYAMOUNT")]}

    balance = documents.balance_of(lines)
    if balance:
        return {"OBJ_KEY": "", "RETURN": [ret(
            "E", "Balance in transaction currency: %.2f %s" % (balance, currency),
            "F5", "702", v1="%.2f" % balance, v2=currency)]}

    posting = str(field(header, "PSTNG_DATE", "")) or None
    document, company_code, year = documents.post_journal_entry(ctx, {
        "CompanyCode": company,
        "AccountingDocumentType": str(field(header, "DOC_TYPE", "SA")) or "SA",
        "DocumentDate": str(field(header, "DOC_DATE", "")) or None,
        "PostingDate": posting,
        "TransactionCurrency": currency,
        "HeaderText": str(field(header, "HEADER_TXT", "")),
        "ReferenceDocument": str(field(header, "REF_DOC_NO", "")),
    }, lines)

    return {
        "OBJ_TYPE": "BKPFF", "OBJ_SYS": SYSTEM_ID + "CLNT100",
        "OBJ_KEY": "%s%s%s" % (document, company_code, year),
        "RETURN": [ret("S", "Document posted successfully: %s %s %s"
                       % (company_code, document, year), "RW", "605",
                       v1=company_code, v2=document, v3=year)],
    }


@function("BAPI_MATERIAL_GET_DETAIL", "MaterialGetDetail")
def _material_detail(ctx, params):
    material = str(param(params, "MATERIAL", "") or "").strip()
    plant = str(param(params, "PLANT", "") or "").strip()
    row = store.get(ctx.conn, ENTITY_TYPES["A_Product"], {"Product": material})
    if row is None:
        return {"MATERIAL_GENERAL_DATA": {}, "RETURN": ret(
            "E", "Material %s does not exist" % material, "M3", "305", v1=material)}
    desc = store.query(
        ctx.conn, ENTITY_TYPES["A_ProductDescription"],
        '"Product" = ? AND "Language" = ?', [material, "EN"])
    return {
        "MATERIAL_GENERAL_DATA": {
            "MATERIAL": row["Product"],
            "MATL_DESC": desc[0]["ProductDescription"] if desc else "",
            "MATL_TYPE": row["ProductType"],
            "MATL_GROUP": row["ProductGroup"],
            "BASE_UOM": row["BaseUnit"],
            "DIVISION": row["Division"],
            "NET_WEIGHT": "%.3f" % (row["NetWeight"] or 0),
            "GROSS_WT": "%.3f" % (row["GrossWeight"] or 0),
            "UNIT_OF_WT": row["WeightUnit"],
            "CREATED_ON": _sap_date(row["CreationDate"]),
            "PLANT": plant,
        },
        "RETURN": ret("S", "Material %s read" % material, "M3", "801"),
    }


@function("BAPI_BUSINESS_PARTNER_GETDETAIL", "BusinessPartnerGetDetail")
def _bp_detail(ctx, params):
    bp_no = str(param(params, "BUSINESSPARTNER", "") or "").strip()
    bp = ENTITY_TYPES["A_BusinessPartner"]
    row = store.get(ctx.conn, bp, {"BusinessPartner": bp_no})
    if row is None:
        return {"CENTRALDATA": {}, "ADDRESSDATA": {}, "RETURN": [
            ret("E", "Business partner %s does not exist" % bp_no, "R1", "201", v1=bp_no)]}
    addr = store.children(ctx.conn, row, bp, bp.nav("to_BusinessPartnerAddress"), limit=1)
    a = addr[0] if addr else None
    return {
        "CENTRALDATA": {
            "PARTNCATEGORY": row["BusinessPartnerCategory"],
            "PARTNGROUP": row["BusinessPartnerGrouping"],
            "NAME_ORG1": row["OrganizationBPName1"],
            "FIRSTNAME": row["FirstName"],
            "LASTNAME": row["LastName"],
            "SEARCHTERM1": row["SearchTerm1"],
        },
        "ADDRESSDATA": {} if a is None else {
            "STREET": a["StreetName"], "HOUSE_NO": a["HouseNumber"],
            "CITY": a["CityName"], "POSTL_COD1": a["PostalCode"],
            "COUNTRY": a["Country"], "REGION": a["Region"],
            "TEL1_NUMBR": a["PhoneNumber"], "E_MAIL": a["EmailAddress"],
        },
        "RETURN": [ret("S", "Business partner %s read" % bp_no, "R1", "000")],
    }


@function("BAPI_TRANSACTION_COMMIT", "TransactionCommit")
def _commit(ctx, params):
    ctx.conn.commit()
    return {"RETURN": ret("S", "Changes were committed", "00", "344")}


@function("BAPI_TRANSACTION_ROLLBACK", "TransactionRollback")
def _rollback(ctx, params):
    return {"RETURN": ret("S", "Changes were rolled back", "00", "344")}


# --------------------------------------------------------------------------
# Master data reads
# --------------------------------------------------------------------------


def _address_of(ctx, bp_row):
    bp = ENTITY_TYPES["A_BusinessPartner"]
    rows = store.children(ctx.conn, bp_row, bp, bp.nav("to_BusinessPartnerAddress"), limit=1)
    return rows[0] if rows else None


def _bp_address_fields(address) -> dict:
    if address is None:
        return {}
    return {
        "STREET": address["StreetName"], "HOUSE_NO": address["HouseNumber"],
        "CITY": address["CityName"], "POSTL_COD1": address["PostalCode"],
        "COUNTRY": address["Country"], "REGION": address["Region"],
        "TELEPHONE": address["PhoneNumber"], "E_MAIL": address["EmailAddress"],
        "LANGU": address["Language"],
    }


@function("BAPI_CUSTOMER_GETLIST", "CustomerGetList")
def _customer_getlist(ctx, params):
    bp = ENTITY_TYPES["A_BusinessPartner"]
    max_rows = int(_num(param(params, "MAXROWS", 0), 0))
    wanted = [str(field(row, "SIGN", "I")) and str(field(row, "LOW", ""))
              for row in (param(params, "IDRANGE") or [])]
    wanted = [value.lstrip("0") or value for value in wanted if value]

    rows = store.query(ctx.conn, bp, '"Customer" <> \'\'', [])
    out = []
    for row in rows:
        if wanted and row["Customer"].lstrip("0") not in wanted:
            continue
        address = _address_of(ctx, row)
        record = {"CUSTOMER": row["Customer"], "NAME": row["BusinessPartnerFullName"],
                  "SEARCHTERM": row["SearchTerm1"]}
        record.update(_bp_address_fields(address))
        out.append(record)
        if max_rows and len(out) >= max_rows:
            break
    messages = [] if out else [ret("W", "No customers found for the selection", "F2", "101")]
    return {"ADDRESSDATA": out, "RETURN": messages}


@function("BAPI_CUSTOMER_GETDETAIL2", "CustomerGetDetail2")
def _customer_getdetail2(ctx, params):
    number = str(param(params, "CUSTOMERNO", "") or "").strip().lstrip("0")
    bp = ENTITY_TYPES["A_BusinessPartner"]
    rows = store.query(ctx.conn, bp, '"Customer" = ?', [number], "", 1) if number else []
    if not rows:
        return {"CUSTOMERGENERALDETAIL": {}, "CUSTOMERCOMPANYDETAIL": {},
                "RETURN": ret("E", "Customer %s does not exist" % number, "F2", "003",
                              v1=number)}
    row = rows[0]
    general = {"NAME": row["BusinessPartnerFullName"], "SEARCHTERM": row["SearchTerm1"],
               "CUSTOMER": row["Customer"], "CREAT_DATE": _sap_date(row["CreationDate"]),
               "CREATED_BY": row["CreatedByUser"]}
    general.update(_bp_address_fields(_address_of(ctx, row)))
    return {
        "CUSTOMERGENERALDETAIL": general,
        "CUSTOMERCOMPANYDETAIL": {"COMP_CODE": "1710", "CUSTOMER": row["Customer"],
                                  "REC_ACCOUNT": "0021100000"},
        "RETURN": ret("S", "Customer %s read" % row["Customer"], "F2", "000"),
    }


@function("BAPI_VENDOR_GETDETAIL", "VendorGetDetail")
def _vendor_getdetail(ctx, params):
    number = str(param(params, "VENDORNO", "") or "").strip().lstrip("0")
    bp = ENTITY_TYPES["A_BusinessPartner"]
    rows = store.query(ctx.conn, bp, '"Supplier" = ?', [number], "", 1) if number else []
    if not rows:
        return {"GENERALDETAIL": {}, "RETURN": ret(
            "E", "Vendor %s does not exist" % number, "F2", "003", v1=number)}
    row = rows[0]
    general = {"VENDOR_NO": row["Supplier"], "NAME": row["BusinessPartnerFullName"],
               "SEARCHTERM": row["SearchTerm1"], "CREAT_DATE": _sap_date(row["CreationDate"])}
    general.update(_bp_address_fields(_address_of(ctx, row)))
    return {"GENERALDETAIL": general,
            "RETURN": ret("S", "Vendor %s read" % row["Supplier"], "F2", "000")}


@function("BAPI_MATERIAL_GETLIST", "MaterialGetList")
def _material_getlist(ctx, params):
    product = ENTITY_TYPES["A_Product"]
    selections = param(params, "MATNRSELECTION") or []
    where, binds = [], []
    for selection in selections:
        low = str(field(selection, "MATNR_LOW", "")).strip()
        high = str(field(selection, "MATNR_HIGH", "")).strip()
        option = str(field(selection, "OPTION", "EQ")).upper()
        if not low:
            continue
        if high and option in ("BT", ""):
            where.append('("Product" >= ? AND "Product" <= ?)')
            binds.extend([low, high])
        elif option == "CP" or low.endswith("*"):
            where.append('"Product" LIKE ?')
            binds.append(low.replace("*", "%"))
        else:
            where.append('"Product" = ?')
            binds.append(low)
    clause = " OR ".join(where)
    rows = store.query(ctx.conn, product, clause, binds)

    descriptions = {}
    for row in store.query(ctx.conn, ENTITY_TYPES["A_ProductDescription"],
                           '"Language" = ?', ["EN"]):
        descriptions[row["Product"]] = row["ProductDescription"]

    listing = [{"MATERIAL": row["Product"], "MATL_DESC": descriptions.get(row["Product"], ""),
                "MATL_TYPE": row["ProductType"], "MATL_GROUP": row["ProductGroup"],
                "BASE_UOM": row["BaseUnit"],
                "DEL_FLAG": "X" if row["IsMarkedForDeletion"] else ""}
               for row in rows]
    messages = [] if listing else [ret("W", "No materials found", "M3", "096")]
    return {"MATNRLIST": listing, "RETURN": messages}


# --------------------------------------------------------------------------
# Changing a sales order
# --------------------------------------------------------------------------


def _flagged(structure, name: str) -> bool:
    """True when the X structure flags this field for change."""
    return str(field(structure, name, "")).strip().upper() == "X"


_HEADER_FIELDS = {
    "PURCH_NO_C": "PurchaseOrderByCustomer",
    "PURCH_DATE": None,
    "DOC_TYPE": "SalesOrderType",
    "SALES_ORG": "SalesOrganization",
    "DISTR_CHAN": "DistributionChannel",
    "DIVISION": "OrganizationDivision",
    "CURRENCY": "TransactionCurrency",
    "INCOTERMS1": "IncotermsClassification",
    "PMNTTRMS": "CustomerPaymentTerms",
    "REQ_DATE_H": "RequestedDeliveryDate",
}

_ITEM_FIELDS = {
    "MATERIAL": "Material",
    "SHORT_TEXT": "SalesOrderItemText",
    "TARGET_QTY": "RequestedQuantity",
    "REQ_QTY": "RequestedQuantity",
    "SALES_UNIT": "RequestedQuantityUnit",
    "PLANT": "Plant",
    "COND_VALUE": "NetAmount",
}


@function("BAPI_SALESORDER_CHANGE", "SalesOrderChange")
def _so_change(ctx, params):
    document = str(param(params, "SALESDOCUMENT", "") or "").strip()
    so = ENTITY_TYPES["A_SalesOrder"]
    row = (store.get(ctx.conn, so, {"SalesOrder": document.zfill(10)})
           or store.get(ctx.conn, so, {"SalesOrder": document}))
    if row is None:
        return {"RETURN": [ret("E", "Document %s does not exist" % document, "V1", "555",
                               v1=document)]}
    keys = {"SalesOrder": row["SalesOrder"]}

    header = param(params, "ORDER_HEADER_IN") or {}
    header_x = param(params, "ORDER_HEADER_INX") or {}
    items = param(params, "ORDER_ITEM_IN") or []
    items_x = {str(field(x, "ITM_NUMBER", "")).strip(): x
               for x in (param(params, "ORDER_ITEM_INX") or [])}

    messages: List[dict] = []
    changed = False

    # Only what the X structure flags is changed - a BAPI ignores the rest,
    # which is the classic way a change call silently does nothing.
    if header and str(field(header_x, "UPDATEFLAG", "U")).upper() in ("U", ""):
        values = {}
        for sap_name, our_name in _HEADER_FIELDS.items():
            if our_name and _flagged(header_x, sap_name):
                values[our_name] = field(header, sap_name, "")
        if values:
            store.update(ctx.conn, so, keys, values, user=ctx.user)
            changed = True
        elif header_x:
            messages.append(ret(
                "W", "No header fields were flagged in ORDER_HEADER_INX; the header "
                     "was not changed", "V1", "010", parameter="ORDER_HEADER_INX"))
        else:
            messages.append(ret(
                "W", "ORDER_HEADER_INX is missing; the header was not changed",
                "V1", "010", parameter="ORDER_HEADER_INX"))

    item_type = ENTITY_TYPES["A_SalesOrderItem"]
    for index, item in enumerate(items, start=1):
        number = str(field(item, "ITM_NUMBER", "")).strip()
        flags = items_x.get(number, {})
        action = str(field(flags, "UPDATEFLAG", "U")).upper() or "U"
        item_keys = {"SalesOrder": row["SalesOrder"], "SalesOrderItem": number}

        if action == "D":
            if store.get(ctx.conn, item_type, item_keys) is None:
                messages.append(ret("E", "Item %s does not exist" % number, "V1", "555",
                                    row=index, parameter="ORDER_ITEM_IN"))
                continue
            store.delete(ctx.conn, item_type, item_keys)
            changed = True
            continue

        if action == "I":
            payload = {our: field(item, sap, "") for sap, our in _ITEM_FIELDS.items()
                       if field(item, sap, "") != ""}
            if number:
                payload["SalesOrderItem"] = number
            payload.setdefault("TransactionCurrency", row["TransactionCurrency"])
            store.insert(ctx.conn, item_type, payload, user=ctx.user,
                         parent_keys={"SalesOrder": row["SalesOrder"]})
            changed = True
            continue

        if store.get(ctx.conn, item_type, item_keys) is None:
            messages.append(ret("E", "Item %s does not exist" % number, "V1", "555",
                                row=index, parameter="ORDER_ITEM_IN"))
            continue
        values = {our: field(item, sap, "") for sap, our in _ITEM_FIELDS.items()
                  if _flagged(flags, sap)}
        if values:
            store.update(ctx.conn, item_type, item_keys, values, user=ctx.user)
            changed = True
        elif flags:
            messages.append(ret(
                "W", "No fields were flagged for item %s; it was not changed" % number,
                "V1", "010", row=index, parameter="ORDER_ITEM_INX"))

    if any(m["TYPE"] == "E" for m in messages):
        return {"RETURN": messages}
    if changed:
        messages.append(ret("S", "Standard Order %s has been saved"
                            % row["SalesOrder"].lstrip("0"), "V1", "311",
                            v1="Standard Order", v2=row["SalesOrder"].lstrip("0")))
    elif not messages:
        messages.append(ret("W", "Nothing was flagged for change", "V1", "010"))
    return {"RETURN": messages}


# --------------------------------------------------------------------------
# RFC_READ_TABLE
# --------------------------------------------------------------------------

# The classic table names people reach for, and what this mock has instead.
_CLASSIC_TABLES = {
    "VBAK": "A_SalesOrder", "VBAP": "A_SalesOrderItem", "KNA1": "A_BusinessPartner",
    "LFA1": "A_BusinessPartner", "MARA": "A_Product", "MAKT": "A_ProductDescription",
    "MARC": "A_ProductPlant", "EKKO": "A_PurchaseOrder", "EKPO": "A_PurchaseOrderItem",
    "ADRC": "A_BusinessPartnerAddress",
}

_ABAP_TYPE = {
    "Edm.String": "C", "Edm.DateTime": "D", "Edm.Decimal": "P",
    "Edm.Double": "P", "Edm.Int32": "I", "Edm.Int16": "I", "Edm.Boolean": "C",
}

_OPTION_OPERATORS = {
    "=": "=", "EQ": "=", "<>": "<>", "NE": "<>", "<": "<", "LT": "<",
    ">": ">", "GT": ">", "<=": "<=", "LE": "<=", ">=": ">=", "GE": ">=",
    "LIKE": "LIKE",
}

_OPTION_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op><>|<=|>=|=|<|>)|(?P<paren>[()])"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*))")


def _options_where(et, lines) -> tuple:
    """Translate the OPTIONS table into a parameterised WHERE clause.

    Deliberately narrow: field names must exist on the table, only the ABAP
    comparison operators are accepted, and every literal is bound. Nothing
    from the caller reaches SQL as text.
    """
    text = " ".join(str(field(line, "TEXT", "")) for line in lines).strip()
    if not text:
        return "", []
    columns = {name.upper(): name for name, _p in et.columns()}
    sql, binds, pos, expect_value_for = [], [], 0, None
    while pos < len(text):
        match = _OPTION_TOKEN.match(text, pos)
        if not match or match.end() == match.start():
            raise SapError("Cannot read OPTIONS at '%s'" % text[pos:pos + 20], 400,
                           code="OPTION_NOT_VALID")
        pos = match.end()
        kind = match.lastgroup
        value = match.group(kind)
        if kind == "paren":
            sql.append(value)
        elif kind == "str":
            binds.append(value[1:-1].replace("''", "'"))
            sql.append("?")
        elif kind == "op":
            sql.append(_OPTION_OPERATORS[value])
        else:
            upper = value.upper()
            if upper in ("AND", "OR", "NOT"):
                sql.append(upper)
            elif upper in _OPTION_OPERATORS:
                sql.append(_OPTION_OPERATORS[upper])
            elif upper in columns:
                sql.append('"%s"' % columns[upper])
            else:
                raise SapError(
                    "Field %s does not exist in table %s" % (value, et.name), 400,
                    code="FIELD_NOT_VALID")
    return " ".join(sql), binds


@function("RFC_READ_TABLE", "RfcReadTable", bapiret=False)
def _rfc_read_table(ctx, params):
    requested = str(param(params, "QUERY_TABLE", "") or "").strip()
    if not requested:
        raise SapError("QUERY_TABLE is missing", 400, code="TABLE_NOT_AVAILABLE")

    tables = {name.upper(): name for name in ENTITY_TYPES}
    resolved = tables.get(requested.upper())
    if resolved is None:
        classic = _CLASSIC_TABLES.get(requested.upper())
        hint = (" This mock exposes %s instead." % classic) if classic else ""
        raise SapError(
            "Table %s is not available in this system.%s" % (requested.upper(), hint),
            400, code="TABLE_NOT_AVAILABLE")
    et = ENTITY_TYPES[resolved]

    columns = dict(et.columns())
    wanted = [str(field(f, "FIELDNAME", "")).strip() for f in (param(params, "FIELDS") or [])]
    wanted = [name for name in wanted if name]
    if wanted:
        lookup = {name.upper(): name for name in columns}
        missing = [name for name in wanted if name.upper() not in lookup]
        if missing:
            raise SapError(
                "Field %s does not exist in table %s" % (missing[0], resolved), 400,
                code="FIELD_NOT_VALID")
        wanted = [lookup[name.upper()] for name in wanted]
    else:
        wanted = list(columns)

    where, binds = _options_where(et, param(params, "OPTIONS") or [])
    skip = int(_num(param(params, "ROWSKIPS", 0), 0))
    count = int(_num(param(params, "ROWCOUNT", 0), 0))
    rows = store.query(ctx.conn, et, where, binds, "", count or None, skip or None)

    # width per field, the way the dictionary would define it
    widths = []
    for name in wanted:
        prop = columns[name]
        width = prop.max_length or {"Edm.DateTime": 8, "Edm.Boolean": 1}.get(prop.type, 18)
        widths.append(max(int(width), len(name)))

    offset, fields_out = 0, []
    for name, width in zip(wanted, widths):
        prop = columns[name]
        fields_out.append({
            "FIELDNAME": name, "OFFSET": str(offset).zfill(6),
            "LENGTH": str(width).zfill(6), "TYPE": _ABAP_TYPE.get(prop.type, "C"),
            "FIELDTEXT": prop.label or name,
        })
        offset += width

    delimiter = str(param(params, "DELIMITER", "") or "")
    data = []
    if str(param(params, "NO_DATA", "") or "").upper() != "X":
        for row in rows:
            cells = []
            for name, width in zip(wanted, widths):
                value = _flat_value(columns[name], row[name])
                cells.append(value if delimiter else value[:width].ljust(width))
            data.append({"WA": delimiter.join(cells) if delimiter else "".join(cells)})

    return {"DATA": data, "FIELDS": fields_out}


def _flat_value(prop, value) -> str:
    """Render one cell the way an ABAP work area would hold it."""
    if value is None:
        return ""
    if prop.type == "Edm.DateTime":
        return str(value)[:10].replace("-", "")
    if prop.type == "Edm.Boolean":
        return "X" if value else ""
    if prop.type in ("Edm.Decimal", "Edm.Double"):
        return ("%%.%df" % (prop.scale if prop.scale is not None else 3)) % float(value)
    return str(value)


ALIASES = {norm(f["alias"]): name for name, f in FUNCTIONS.items()}
ALIASES.update({norm(name): name for name in FUNCTIONS})


def resolve(name: str) -> Optional[str]:
    return ALIASES.get(norm(name))


MESSAGE_TYPES = {
    "E": "Error - the document was not created",
    "A": "Abort - the call gave up",
    "W": "Warning - the call worked and says something anyway",
    "S": "Success - an extra message on a call that worked",
}


class BehaviourRules:
    """What a function module answers, driven through ``/_mock/bapi-behaviour``.

    A BAPI reports a business error by returning normally, with HTTP 200 and a
    ``RETURN`` row of type ``E``: credit limit exceeded, posting period closed,
    material blocked for sales. The obvious client reads the status code, sees
    success, and commits. Nothing else in this mock can produce that
    combination for a *valid* call -- the errors it raises by itself all come
    from malformed input -- so a client that ignores ``RETURN`` could not be
    caught by a test suite written against it.
    """

    def __init__(self):
        self.rules: List[dict] = []
        self._lock = threading.Lock()
        self._next_id = 1

    def add(self, rule: dict) -> dict:
        name = str(rule.get("function") or rule.get("FUNCTION") or "")
        resolved = resolve(name)
        if resolved is None:
            raise SapError(
                "Function module %s does not exist in this mock" % (name.upper() or "?"),
                400, code="RFC_ERROR_FUNCTION_NOT_FOUND")
        if not FUNCTIONS[resolved]["bapiret"]:
            raise SapError(
                "%s has no RETURN table, so it cannot answer with a BAPIRET2 "
                "message; a real one raises an ABAP exception instead. For a "
                "failure on this function use the sap-mock-scenario header."
                % resolved, 400)
        msg_type = str(rule.get("type") or "E").upper()
        if msg_type not in MESSAGE_TYPES:
            raise SapError(
                "A BAPIRET2 message type is one of %s, not %s"
                % (", ".join(sorted(MESSAGE_TYPES)), msg_type), 400)
        message = str(rule.get("message") or "")
        if not message:
            raise SapError(
                "Give the message the %s row should carry; a business error "
                "without its text is not worth returning" % msg_type, 400)
        with self._lock:
            stored = {
                "id": self._next_id,
                "function": resolved,
                "type": msg_type,
                "message": message,
                "msg_id": str(rule.get("id") or rule.get("msg_id") or "SR"),
                "number": str(rule.get("number") or "000"),
                "count": int(rule.get("count") or 0),   # 0 = until cleared
                "hits": 0,
            }
            self._next_id += 1
            self.rules.append(stored)
            return stored

    def clear(self) -> int:
        with self._lock:
            count = len(self.rules)
            self.rules = []
            return count

    def match(self, resolved: str) -> Optional[dict]:
        with self._lock:
            for rule in list(self.rules):
                if rule["function"] != resolved:
                    continue
                rule["hits"] += 1
                if rule["count"] and rule["hits"] >= rule["count"]:
                    self.rules.remove(rule)
                return rule
        return None


def call(ctx, name: str, params: dict, protocol: str = "json",
         behaviour=None) -> dict:
    """Invoke a function module.  Raises SapError if it does not exist.

    ``behaviour`` is an optional :class:`BehaviourRules`. An ``E`` or ``A`` rule
    answers instead of the handler, so nothing is created; a ``W`` or ``S`` rule
    lets the call do its work and adds its message to what comes back.
    """
    resolved = resolve(name)
    if resolved is None:
        raise SapError(
            "Function module %s does not exist (mock system %s)" % (name.upper(), SYSTEM_ID),
            404, code="RFC_ERROR_FUNCTION_NOT_FOUND")
    rule = behaviour.match(resolved) if behaviour else None
    if rule and rule["type"] in ("E", "A"):
        # The handler never runs, so no number is drawn and no row is written.
        # This is a business error, not a transport one: the caller gets 200.
        result = {"RETURN": [ret(rule["type"], rule["message"],
                                rule["msg_id"], rule["number"])]}
    else:
        result = FUNCTIONS[resolved]["handler"](ctx, params or {})
        if rule:
            result.setdefault("RETURN", []).append(
                ret(rule["type"], rule["message"], rule["msg_id"], rule["number"]))
    ctx.conn.execute(
        "INSERT INTO rfc_log(ts,function_name,protocol,request,response) VALUES(?,?,?,?,?)",
        (_dt.datetime.utcnow().isoformat(), resolved, protocol,
         json.dumps(params)[:20000], json.dumps(result)[:20000]),
    )
    ctx.conn.commit()
    return result


# --------------------------------------------------------------------------
# SOAP transport
# --------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _xml_to_value(elem):
    kids = list(elem)
    if not kids:
        return (elem.text or "").strip()
    if all(_local(k.tag) == "item" for k in kids):
        return [_xml_to_value(k) for k in kids]
    out: Dict[str, Any] = {}
    for kid in kids:
        name = _local(kid.tag)
        value = _xml_to_value(kid)
        if name in out:
            if not isinstance(out[name], list):
                out[name] = [out[name]]
            out[name].append(value)
        else:
            out[name] = value
    return out


def parse_soap(body: bytes):
    """Return (function_name, params) from a SOAP envelope."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SapError("The SOAP request is not well-formed XML: %s" % exc, 400)
    body_elem = None
    for elem in root.iter():
        if _local(elem.tag) == "Body":
            body_elem = elem
            break
    if body_elem is None or not list(body_elem):
        raise SapError("The SOAP envelope has no Body payload", 400)
    call_elem = list(body_elem)[0]
    params = _xml_to_value(call_elem)
    if not isinstance(params, dict):
        params = {}
    return _local(call_elem.tag), params


def _value_to_xml(name: str, value) -> str:
    if isinstance(value, dict):
        inner = "".join(_value_to_xml(k, v) for k, v in value.items())
        return "<%s>%s</%s>" % (name, inner, name)
    if isinstance(value, list):
        inner = "".join(_value_to_xml("item", v) for v in value)
        return "<%s>%s</%s>" % (name, inner, name)
    if isinstance(value, bool):
        value = "X" if value else ""
    return "<%s>%s</%s>" % (name, escape("" if value is None else str(value)), name)


def soap_response(function_name: str, result: dict) -> str:
    alias = FUNCTIONS[function_name]["alias"]
    inner = "".join(_value_to_xml(k, v) for k, v in result.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap-env:Envelope xmlns:soap-env="%s"><soap-env:Header/><soap-env:Body>'
        '<n0:%sResponse xmlns:n0="%s">%s</n0:%sResponse>'
        "</soap-env:Body></soap-env:Envelope>"
        % (SOAP_ENV, alias, SOAP_NS, inner, alias)
    )


def soap_fault(err: SapError) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap-env:Envelope xmlns:soap-env="%s"><soap-env:Body><soap-env:Fault>'
        "<faultcode>soap-env:Client</faultcode><faultstring xml:lang=\"en\">%s</faultstring>"
        "<detail><n0:SystemFault xmlns:n0=\"%s\"><Name>%s</Name><Text>%s</Text>"
        "</n0:SystemFault></detail>"
        "</soap-env:Fault></soap-env:Body></soap-env:Envelope>"
        % (SOAP_ENV, escape(err.message), SOAP_NS, escape(err.code), escape(err.message))
    )
