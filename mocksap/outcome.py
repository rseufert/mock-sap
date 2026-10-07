"""What posting an IDoc decided, filed under the IDoc it arrived on.

Three message types post something, and each answers in a different shape: a
statement's lines, the sales orders a delivery moved, the pair of numbers a
supplier's bill created. The POST receipt carried that answer and the mock kept
none of it, so a client that did not hold on to the response had no way back to
it -- #105 for the statement, #116 for the other two.

Each shape gets its own small table. One table with a ``kind`` column deciding
what the other columns mean, most of them null on any given row, is the
modelling this mock argues against everywhere else.

No table stores the sentence it answers with. Every ``MESSAGE`` is rebuilt here
from the numbers beside it, so the sentence on the POST receipt and the one a
later read returns cannot drift apart; two columns exist (``currency`` and
``gross`` on an invoice) only to make that possible.

:func:`of` is the inverse of the three ``file_`` functions and reproduces what
``idoc._apply`` returned rather than an approximation of it. That round trip is
what the tests assert, because an outcome that reads back nearly right is worse
than one that is plainly missing.
"""
from __future__ import annotations

import json
from typing import List, Optional


# --- a bank statement (#105) ------------------------------------------------

def statement_message(statement: str, cleared: list, reopened: list,
                      unprocessed: list) -> str:
    """The one-line summary of what a statement did."""
    return ("Statement %s: %d item(s) cleared, %d reopened, %d line(s) "
            "unprocessed" % (statement or "(unnumbered)", len(cleared),
                             len(reopened), len(unprocessed)))


_LINE_KINDS = (
    # kind, the key in `applied`, and the column `related_document` holds
    ("CLEARED", "CLEARED", "CLEARINGDOCUMENT"),
    ("REOPENED", "REOPENED", "REVERSALDOCUMENT"),
    ("UNPROCESSED", "UNPROCESSED", ""),
)


def file_statement(conn, docnum: str, applied: dict) -> None:
    """File what posting this statement decided.

    Written even when every list is empty: a statement that cleared nothing is
    a different answer from an IDoc nobody posted a statement for, and the
    header row is what tells them apart.
    """
    conn.execute("DELETE FROM idoc_statement WHERE docnum = ?", (docnum,))
    conn.execute("DELETE FROM idoc_statement_line WHERE docnum = ?", (docnum,))
    conn.execute(
        "INSERT INTO idoc_statement(docnum,statement,account,findings) "
        "VALUES(?,?,?,?)",
        (docnum, applied["STATEMENT"], applied["ACCOUNT"],
         json.dumps(applied["FINDINGS"])))
    seq = 0
    for kind, key, related in _LINE_KINDS:
        for row in applied[key]:
            conn.execute(
                "INSERT INTO idoc_statement_line(docnum,seq,kind,line,reference,"
                "accounting_document,related_document,amount,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (docnum, seq, kind, row["LINE"], row.get("REFERENCE", ""),
                 row.get("ACCOUNTINGDOCUMENT", ""),
                 row.get(related, "") if related else "",
                 row.get("AMOUNT", ""), row.get("REASON", "")))
            seq += 1
    conn.commit()


def _statement_of(conn, docnum: str) -> Optional[List[dict]]:
    head = conn.execute(
        "SELECT statement,account,findings FROM idoc_statement WHERE docnum = ?",
        (docnum,)).fetchone()
    if head is None:
        return None
    by_kind = {"CLEARED": [], "REOPENED": [], "UNPROCESSED": []}
    rows = conn.execute(
        "SELECT * FROM idoc_statement_line WHERE docnum = ? ORDER BY seq",
        (docnum,)).fetchall()
    for row in rows:
        row = dict(row)
        if row["kind"] == "UNPROCESSED":
            by_kind["UNPROCESSED"].append(
                {"LINE": row["line"], "REASON": row["reason"]})
            continue
        related = "CLEARINGDOCUMENT" if row["kind"] == "CLEARED" else "REVERSALDOCUMENT"
        by_kind[row["kind"]].append({
            "LINE": row["line"], "REFERENCE": row["reference"],
            "ACCOUNTINGDOCUMENT": row["accounting_document"],
            related: row["related_document"], "AMOUNT": row["amount"]})
    statement = head["statement"] or ""
    return [{
        "STATEMENT": statement,
        "ACCOUNT": head["account"] or "",
        "CLEARED": by_kind["CLEARED"],
        "REOPENED": by_kind["REOPENED"],
        "UNPROCESSED": by_kind["UNPROCESSED"],
        "FINDINGS": json.loads(head["findings"]),
        "MESSAGE": statement_message(statement, by_kind["CLEARED"],
                                     by_kind["REOPENED"],
                                     by_kind["UNPROCESSED"]),
    }]


# --- a delivery (#116) ------------------------------------------------------

# What each of SAP's delivery statuses means, spelled out once because the
# receipt and a later read of it are built from the same words.
_DELIVERY_STATUS = {"A": "not yet delivered", "B": "partly delivered",
                    "C": "fully delivered"}


def delivery_message(sales_order: str, status: str, delivery: str) -> str:
    """What posting a DELVRY did to one sales order.

    A blank status is an order the mock could not find, which is the one
    outcome that moved nothing: ``documents.apply_delivery_status`` answers
    one of SAP's three statuses and never blank.
    """
    if not status:
        return "Sales order %s does not exist" % sales_order
    moved = ("Delivery status set to %s (%s)"
             % (status, _DELIVERY_STATUS.get(status, status)))
    if delivery:
        return "Delivery %s created; %s" % (delivery, moved[0].lower() + moved[1:])
    return moved


def file_delivery(conn, docnum: str, applied: List[dict]) -> None:
    """File which sales orders posting this delivery moved, and how."""
    conn.execute("DELETE FROM idoc_delivery WHERE docnum = ?", (docnum,))
    for seq, row in enumerate(applied):
        conn.execute(
            "INSERT INTO idoc_delivery(docnum,seq,sales_order,status,delivery) "
            "VALUES(?,?,?,?,?)",
            (docnum, seq, row["SALESORDER"], row.get("STATUS", ""),
             row.get("DELIVERY", "")))
    conn.commit()


def _delivery_of(conn, docnum: str) -> Optional[List[dict]]:
    rows = conn.execute(
        "SELECT sales_order,status,delivery FROM idoc_delivery "
        "WHERE docnum = ? ORDER BY seq", (docnum,)).fetchall()
    if not rows:
        return None
    applied = []
    for row in rows:
        row = dict(row)
        # Built in the order `_apply_delivery` builds it, so the JSON a read
        # renders reads the same as the receipt's and not merely equal to it.
        entry = {
            "SALESORDER": row["sales_order"],
            "STATUS": row["status"] or "",
            "MESSAGE": delivery_message(row["sales_order"], row["status"] or "",
                                        row["delivery"] or ""),
        }
        if row["delivery"]:
            entry["DELIVERY"] = row["delivery"]
        applied.append(entry)
    return applied


# --- a supplier's invoice (#116) --------------------------------------------

def invoice_message(supplier_invoice: str, currency: str, gross: str,
                    supplier: str) -> str:
    """What posting an INVOIC owed, and to whom."""
    return ("Supplier invoice %s posted; %s %s payable to %s"
            % (supplier_invoice, currency, gross, supplier))


def file_invoice(conn, docnum: str, applied: List[dict], currency: str,
                 gross: str) -> None:
    """File the invoice and the payable posting this INVOIC created.

    ``currency`` and ``gross`` are passed rather than read off ``applied``:
    they are not fields of the receipt, only words in its sentence, and they
    are stored so that sentence can be rebuilt instead of kept.
    """
    row = applied[0]
    conn.execute("DELETE FROM idoc_invoice WHERE docnum = ?", (docnum,))
    conn.execute(
        "INSERT INTO idoc_invoice(docnum,supplier_invoice,fiscal_year,"
        "accounting_document,invoicing_party,currency,gross) "
        "VALUES(?,?,?,?,?,?,?)",
        (docnum, row["SUPPLIERINVOICE"], row.get("FISCALYEAR", ""),
         row.get("ACCOUNTINGDOCUMENT", ""), row.get("INVOICINGPARTY", ""),
         currency, gross))
    conn.commit()


def _invoice_of(conn, docnum: str) -> Optional[List[dict]]:
    row = conn.execute(
        "SELECT supplier_invoice,fiscal_year,accounting_document,"
        "invoicing_party,currency,gross FROM idoc_invoice WHERE docnum = ?",
        (docnum,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    return [{
        "SUPPLIERINVOICE": row["supplier_invoice"],
        "FISCALYEAR": row["fiscal_year"] or "",
        "ACCOUNTINGDOCUMENT": row["accounting_document"] or "",
        "INVOICINGPARTY": row["invoicing_party"] or "",
        "MESSAGE": invoice_message(row["supplier_invoice"],
                                   row["currency"] or "", row["gross"] or "",
                                   row["invoicing_party"] or ""),
    }]


# --- reading one back -------------------------------------------------------

def of(conn, docnum: str) -> Optional[List[dict]]:
    """What posting the IDoc on this docnum decided, or None if it posted nothing.

    Which of the three it was is not asked: an IDoc has at most one outcome, so
    whichever table holds a row for it answers. Reading the message type first
    and choosing a table from it would make this depend on the spelling of
    ``MESTYP``, which the wire decides and this does not need to know.
    """
    for reader in (_statement_of, _delivery_of, _invoice_of):
        applied = reader(conn, docnum)
        if applied is not None:
            return applied
    return None
