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
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from . import db, store
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


def function(name: str, alias: str):
    def wrap(fn):
        FUNCTIONS[name] = {"name": name, "alias": alias, "handler": fn}
        return fn

    return wrap


@function("RFC_PING", "RfcPing")
def _rfc_ping(ctx, params):
    return {}


@function("STFC_CONNECTION", "StfcConnection")
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


ALIASES = {norm(f["alias"]): name for name, f in FUNCTIONS.items()}
ALIASES.update({norm(name): name for name in FUNCTIONS})


def resolve(name: str) -> Optional[str]:
    return ALIASES.get(norm(name))


def call(ctx, name: str, params: dict, protocol: str = "json") -> dict:
    """Invoke a function module.  Raises SapError if it does not exist."""
    resolved = resolve(name)
    if resolved is None:
        raise SapError(
            "Function module %s does not exist (mock system %s)" % (name.upper(), SYSTEM_ID),
            404, code="RFC_ERROR_FUNCTION_NOT_FOUND")
    result = FUNCTIONS[resolved]["handler"](ctx, params or {})
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
