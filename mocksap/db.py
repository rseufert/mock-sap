"""SQLite storage: schema creation, number ranges and deterministic seed data.

Tables are generated from :mod:`mocksap.schema`, so the database always
matches the OData metadata the mock advertises.
"""
from __future__ import annotations

import datetime as _dt
import random
import sqlite3
import threading
import uuid
from typing import Optional

from .schema import ENTITY_TYPES, EntityType

_MEMORY_URI = "file:mocksap?mode=memory&cache=shared"
_keepalive = []  # holds the connection that keeps an in-memory DB alive
_lock = threading.Lock()


def connect(path: str) -> sqlite3.Connection:
    """Open a connection.  ``:memory:`` maps to a shared-cache memory DB so
    that every request thread sees the same data."""
    if path in (":memory:", "memory"):
        conn = sqlite3.connect(_MEMORY_URI, uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL" if path not in (":memory:", "memory") else "PRAGMA synchronous=OFF")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def open_database(path: str) -> sqlite3.Connection:
    """Open the connection a mock system uses.

    An in-memory database gets a unique shared-cache name so that several
    mock systems in one process stay isolated from each other, and the
    connection is kept referenced for as long as the process lives.
    """
    if path in (":memory:", "memory"):
        uri = "file:mocksap_%s?mode=memory&cache=shared" % uuid.uuid4().hex
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        _keepalive.append(conn)
        return conn
    return connect(path)


def ddl_for(et: EntityType) -> str:
    cols = []
    for p in et.props:
        col = '"%s" %s' % (p.name, p.sql_type)
        if p.key:
            col += " NOT NULL"
        cols.append(col)
    keys = ", ".join('"%s"' % p.name for p in et.keys)
    cols.append("PRIMARY KEY (%s)" % keys)
    return 'CREATE TABLE IF NOT EXISTS "%s" (\n  %s\n)' % (et.name, ",\n  ".join(cols))


def init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for et in ENTITY_TYPES.values():
        cur.execute(ddl_for(et))
    cur.execute(
        """CREATE TABLE IF NOT EXISTS number_range (
            object TEXT PRIMARY KEY,
            current INTEGER NOT NULL
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            method TEXT,
            path TEXT,
            query TEXT,
            headers TEXT,
            body TEXT,
            status INTEGER,
            duration_ms REAL
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS idoc (
            docnum TEXT PRIMARY KEY,
            direction TEXT,
            idoctyp TEXT,
            mestyp TEXT,
            status TEXT,
            status_text TEXT,
            created_at TEXT,
            content_type TEXT,
            payload TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS rfc_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            function_name TEXT,
            protocol TEXT,
            request TEXT,
            response TEXT
        )"""
    )
    conn.commit()


def reset(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for et in ENTITY_TYPES.values():
        cur.execute('DELETE FROM "%s"' % et.name)
    for t in ("number_range", "idoc", "rfc_log"):
        cur.execute("DELETE FROM %s" % t)
    conn.commit()


# --------------------------------------------------------------------------
# Number ranges (SAP hands out document numbers from a number range object)
# --------------------------------------------------------------------------

_RANGE_START = {
    "SALESORDER": 4711,
    "PURCHASEORDER": 4500000100,
    "BUSINESSPARTNER": 1000000,
    "PRODUCT": 100000,
    "IDOC": 1000000000000001,
}


def next_number(conn: sqlite3.Connection, obj: str, width: int = 10) -> str:
    """Draw the next number from a number range object, zero padded like SAP."""
    with _lock:
        cur = conn.cursor()
        row = cur.execute("SELECT current FROM number_range WHERE object=?", (obj,)).fetchone()
        if row is None:
            nxt = _RANGE_START.get(obj, 1)
        else:
            nxt = row["current"] + 1
        cur.execute(
            "INSERT INTO number_range(object,current) VALUES(?,?) "
            "ON CONFLICT(object) DO UPDATE SET current=excluded.current",
            (obj, nxt),
        )
        conn.commit()
    return str(nxt).zfill(width)


# --------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------

_CITIES = [
    ("Walldorf", "69190", "DE", "BW"), ("Palo Alto", "94304", "US", "CA"),
    ("Newtown Square", "19073", "US", "PA"), ("Bangalore", "560103", "IN", "KA"),
    ("Tokyo", "141-0032", "JP", "13"), ("Dublin", "D02 XY45", "IE", ""),
    ("Sao Paulo", "04578-000", "BR", "SP"), ("Sydney", "2000", "AU", "NSW"),
]
_ORGS = [
    "Becker Berlin", "Talpa GmbH", "Panorama Studios", "Asia High tech",
    "Alpine Systems", "Meridian Trading", "Nordwind Logistics", "Delta Manufacturing",
    "Sunrise Retail Group", "Quantum Components", "Helios Energy", "Vector Analytics",
]
_PERSONS = [
    ("Karl", "Mueller"), ("Sophie", "Dubois"), ("Hiro", "Tanaka"), ("Ana", "Silva"),
    ("Priya", "Nair"), ("Liam", "O'Brien"), ("Mia", "Schneider"), ("Noah", "Andersson"),
]
_MATERIALS = [
    ("TG11", "Notebook Professional 15", "FERT", "L001", "PC", 4.2, 3.9),
    ("TG12", "Notebook Professional 17", "FERT", "L001", "PC", 4.9, 4.5),
    ("TG13", "ITelO Vault", "FERT", "L002", "PC", 0.6, 0.5),
    ("TG14", "Comfort Easy Keyboard", "FERT", "L003", "PC", 1.1, 0.9),
    ("TG21", "Flat Basic Monitor 24", "FERT", "L004", "PC", 6.4, 5.8),
    ("TG22", "Flat Pro Monitor 32", "FERT", "L004", "PC", 9.1, 8.4),
    ("MZ-FG-C100", "Cable Assembly C100", "HALB", "L005", "M", 0.3, 0.25),
    ("MZ-RM-S200", "Steel Sheet S200", "ROH", "L006", "KG", 12.0, 12.0),
    ("SRV-INST-01", "On-site Installation Service", "DIEN", "L009", "H", 0.0, 0.0),
    ("TG31", "Smart Multimedia Player", "FERT", "L007", "PC", 1.8, 1.5),
]
_PLANTS = ["1710", "1010", "2010"]


def _iso(d: _dt.date) -> str:
    return _dt.datetime(d.year, d.month, d.day).isoformat()


def seed(conn: sqlite3.Connection, seed_value: int = 42, orders: int = 25, pos: int = 12) -> dict:
    """Populate the database with deterministic, SAP-looking demo data."""
    rnd = random.Random(seed_value)
    cur = conn.cursor()
    today = _dt.date(2026, 1, 15)

    def ins(table, row):
        cols = ", ".join('"%s"' % c for c in row)
        marks = ", ".join("?" for _ in row)
        cur.execute(
            'INSERT OR REPLACE INTO "%s" (%s) VALUES (%s)' % (table, cols, marks),
            list(row.values()),
        )

    # ---- business partners -------------------------------------------------
    customers, suppliers = [], []
    bp_no = 1000000
    for i, org in enumerate(_ORGS):
        bp_no += 1
        bp = str(bp_no)
        is_supplier = i >= len(_ORGS) - 4
        created = today - _dt.timedelta(days=rnd.randint(200, 1800))
        ins(
            "A_BusinessPartner",
            dict(
                BusinessPartner=bp,
                Customer="" if is_supplier else bp,
                Supplier=bp if is_supplier else "",
                BusinessPartnerCategory="2",
                BusinessPartnerFullName=org,
                BusinessPartnerName=org,
                BusinessPartnerGrouping="BP02" if is_supplier else "BP01",
                OrganizationBPName1=org,
                FirstName="",
                LastName="",
                SearchTerm1=org.split()[0].upper()[:20],
                Industry=rnd.choice(["0100", "0200", "0400", "0900"]),
                CreatedByUser="CB9980000001",
                CreationDate=_iso(created),
                LastChangedByUser="CB9980000001",
                LastChangeDate=_iso(created + _dt.timedelta(days=rnd.randint(1, 100))),
                BusinessPartnerIsBlocked=0,
            ),
        )
        (suppliers if is_supplier else customers).append(bp)
        city, postal, country, region = _CITIES[i % len(_CITIES)]
        ins(
            "A_BusinessPartnerAddress",
            dict(
                BusinessPartner=bp,
                AddressID=str(20000 + i).zfill(10),
                CityName=city,
                PostalCode=postal,
                StreetName=rnd.choice(["Dietmar-Hopp-Allee", "Hasso-Plattner-Ring", "Market Street", "Industrial Park Road"]),
                HouseNumber=str(rnd.randint(1, 120)),
                Country=country,
                Region=region,
                Language="EN",
                PhoneNumber="+%d-%d-%d" % (rnd.randint(1, 99), rnd.randint(100, 999), rnd.randint(100000, 999999)),
                EmailAddress="info@%s.example" % org.split()[0].lower(),
            ),
        )
        ins(
            "A_BusinessPartnerRole",
            dict(
                BusinessPartner=bp,
                BusinessPartnerRole="FLVN01" if is_supplier else "FLCU01",
                ValidFrom=_iso(created),
                ValidTo=_iso(_dt.date(9999, 12, 31)),
            ),
        )

    for i, (first, last) in enumerate(_PERSONS):
        bp_no += 1
        bp = str(bp_no)
        created = today - _dt.timedelta(days=rnd.randint(30, 900))
        ins(
            "A_BusinessPartner",
            dict(
                BusinessPartner=bp,
                Customer=bp,
                Supplier="",
                BusinessPartnerCategory="1",
                BusinessPartnerFullName="%s %s" % (first, last),
                BusinessPartnerName=last,
                BusinessPartnerGrouping="BP01",
                OrganizationBPName1="",
                FirstName=first,
                LastName=last,
                SearchTerm1=last.upper()[:20],
                Industry="",
                CreatedByUser="CB9980000002",
                CreationDate=_iso(created),
                LastChangedByUser="CB9980000002",
                LastChangeDate=_iso(created),
                BusinessPartnerIsBlocked=0,
            ),
        )
        customers.append(bp)
        city, postal, country, region = _CITIES[(i + 3) % len(_CITIES)]
        ins(
            "A_BusinessPartnerAddress",
            dict(
                BusinessPartner=bp, AddressID=str(30000 + i).zfill(10), CityName=city,
                PostalCode=postal, StreetName="Main Street", HouseNumber=str(rnd.randint(1, 90)),
                Country=country, Region=region, Language="EN",
                PhoneNumber="+1-555-%d" % rnd.randint(1000, 9999),
                EmailAddress="%s.%s@example.com" % (first.lower(), last.lower().replace("'", "")),
            ),
        )

    # ---- products ----------------------------------------------------------
    for mat, desc, mtype, mgroup, unit, gross, net in _MATERIALS:
        created = today - _dt.timedelta(days=rnd.randint(400, 2000))
        ins(
            "A_Product",
            dict(
                Product=mat, ProductType=mtype, ProductGroup=mgroup, BaseUnit=unit,
                Division="00", ProductOldID="", GrossWeight=gross, NetWeight=net,
                WeightUnit="KG" if unit != "H" else "", ItemCategoryGroup="NORM",
                IsMarkedForDeletion=0, CreatedByUser="CB9980000001",
                CreationDate=_iso(created), LastChangeDate=_iso(created + _dt.timedelta(days=30)),
            ),
        )
        for lang, text in (("EN", desc), ("DE", desc)):
            ins("A_ProductDescription", dict(Product=mat, Language=lang, ProductDescription=text))
        for plant in _PLANTS[: rnd.randint(1, 3)]:
            ins(
                "A_ProductPlant",
                dict(
                    Product=mat, Plant=plant, PurchasingGroup="00%d" % rnd.randint(1, 9),
                    ProfitCenter="YB110", AvailabilityCheckType="02", MRPType="PD",
                    IsMarkedForDeletion=0,
                ),
            )

    # ---- sales orders ------------------------------------------------------
    so_no = _RANGE_START["SALESORDER"]
    for _ in range(orders):
        so_no += 1
        so = str(so_no).zfill(10)
        sold_to = rnd.choice(customers)
        doc_date = today - _dt.timedelta(days=rnd.randint(0, 180))
        currency = "EUR"
        total = 0.0
        n_items = rnd.randint(1, 4)
        items = []
        for i in range(n_items):
            mat, desc, _t, mgroup, unit, _g, _n = rnd.choice(_MATERIALS)
            qty = float(rnd.randint(1, 50))
            price = round(rnd.uniform(15, 2400), 2)
            amount = round(qty * price, 2)
            total += amount
            items.append(
                dict(
                    SalesOrder=so, SalesOrderItem=str((i + 1) * 10).zfill(6), HigherLevelItem="",
                    Material=mat, MaterialByCustomer="", SalesOrderItemText=desc,
                    SalesOrderItemCategory="TAN", RequestedQuantity=qty,
                    RequestedQuantityUnit=unit, NetAmount=amount, TransactionCurrency=currency,
                    MaterialGroup=mgroup, Plant=rnd.choice(_PLANTS), ShippingPoint="1710",
                    RequestedDeliveryDate=_iso(doc_date + _dt.timedelta(days=rnd.randint(3, 30))),
                )
            )
        ins(
            "A_SalesOrder",
            dict(
                SalesOrder=so, SalesOrderType="OR", SalesOrganization="1710",
                DistributionChannel="10", OrganizationDivision="00", SoldToParty=sold_to,
                PurchaseOrderByCustomer="PO-%d" % rnd.randint(10000, 99999),
                TransactionCurrency=currency, TotalNetAmount=round(total, 2),
                SalesOrderDate=_iso(doc_date),
                RequestedDeliveryDate=_iso(doc_date + _dt.timedelta(days=14)),
                ShippingCondition="01", IncotermsClassification=rnd.choice(["EXW", "FOB", "CIF", "DAP"]),
                CustomerPaymentTerms="0001",
                OverallSDProcessStatus=rnd.choice(["A", "B", "C"]),
                OverallDeliveryStatus=rnd.choice(["A", "B", "C"]),
                CreatedByUser="CB9980000001", CreationDate=_iso(doc_date),
                LastChangeDate=_iso(doc_date + _dt.timedelta(days=1)),
            ),
        )
        for it in items:
            ins("A_SalesOrderItem", it)
        ins("A_SalesOrderHeaderPartner", dict(
            SalesOrder=so, PartnerFunction="AG", Customer=sold_to, Supplier="",
            Personnel="", ContactPerson="", AddressID=""))
        ins("A_SalesOrderHeaderPartner", dict(
            SalesOrder=so, PartnerFunction="WE", Customer=sold_to, Supplier="",
            Personnel="", ContactPerson="", AddressID=""))
    cur.execute(
        "INSERT INTO number_range(object,current) VALUES('SALESORDER',?) "
        "ON CONFLICT(object) DO UPDATE SET current=excluded.current", (so_no,))

    # ---- purchase orders ---------------------------------------------------
    po_no = _RANGE_START["PURCHASEORDER"]
    for _ in range(pos):
        po_no += 1
        po = str(po_no).zfill(10)
        supplier = rnd.choice(suppliers)
        doc_date = today - _dt.timedelta(days=rnd.randint(0, 200))
        ins(
            "A_PurchaseOrder",
            dict(
                PurchaseOrder=po, PurchaseOrderType="NB", CompanyCode="1710",
                PurchasingOrganization="1710", PurchasingGroup="001", Supplier=supplier,
                DocumentCurrency="EUR", Language="EN", PaymentTerms="0001", NetPaymentDays=30,
                PurchaseOrderDate=_iso(doc_date), CreatedByUser="CB9980000001",
                CreationDate=_iso(doc_date), PurchasingProcessingStatus=rnd.choice(["02", "05", "08"]),
            ),
        )
        for i in range(rnd.randint(1, 3)):
            mat, desc, _t, mgroup, unit, _g, _n = rnd.choice(_MATERIALS)
            ins(
                "A_PurchaseOrderItem",
                dict(
                    PurchaseOrder=po, PurchaseOrderItem=str((i + 1) * 10).zfill(5),
                    PurchaseOrderItemText=desc, Material=mat, MaterialGroup=mgroup,
                    Plant=rnd.choice(_PLANTS), StorageLocation="171%d" % rnd.randint(0, 9),
                    OrderQuantity=float(rnd.randint(5, 500)), PurchaseOrderQuantityUnit=unit,
                    NetPriceAmount=round(rnd.uniform(5, 900), 2), NetPriceQuantity=1.0,
                    DocumentCurrency="EUR", TaxCode="V1",
                    IsCompletelyDelivered=rnd.choice([0, 0, 1]),
                ),
            )
    cur.execute(
        "INSERT INTO number_range(object,current) VALUES('PURCHASEORDER',?) "
        "ON CONFLICT(object) DO UPDATE SET current=excluded.current", (po_no,))
    cur.execute(
        "INSERT INTO number_range(object,current) VALUES('BUSINESSPARTNER',?) "
        "ON CONFLICT(object) DO UPDATE SET current=excluded.current", (bp_no,))
    conn.commit()

    return counts(conn)


def counts(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    out = {}
    for name in ENTITY_TYPES:
        out[name] = cur.execute('SELECT COUNT(*) c FROM "%s"' % name).fetchone()["c"]
    for name in ("idoc", "rfc_log", "request_log"):
        out[name] = cur.execute("SELECT COUNT(*) c FROM %s" % name).fetchone()["c"]
    return out


def log_request(conn: sqlite3.Connection, **kw) -> None:
    conn.execute(
        "INSERT INTO request_log(ts,method,path,query,headers,body,status,duration_ms) "
        "VALUES(:ts,:method,:path,:query,:headers,:body,:status,:duration_ms)",
        kw,
    )
    conn.commit()
