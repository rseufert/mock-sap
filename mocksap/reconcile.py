"""Matching a bank statement to the open items it pays.

`statement.py` says what a FINSTA01 claims.  This decides what to do about
it, which is the half that can be wrong in expensive ways.

Electronic bank statement processing in miniature.  A debit line - money that
left the account - clears the open item it names, if the reference matches
*and* the amount matches.  A credit line that carries the reference of an
item already cleared is a returned payment: the clearing is reversed and the
item is open again, which is the only thing that makes a returned payment
visible in SAP.

**Everything else is left alone and reported.**  A line that matches nothing,
or matches a reference but not the amount, clears nothing and comes back as
unprocessed.  That is not a shortcoming; it is the reconciliation gap a
treasury team works through every morning, and a mock that guessed at those
lines would be inventing the answer they are paid to find.
"""
from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import db, documents, statement as statement_reader, store
from .schema import ENTITY_TYPES

CUBE = "A_OperationalAcctgDocItemCube"

# A payable line, in SAP's account-type letters.
SUPPLIER_LINE = documents.ITEM_TYPE_SUPPLIER


def _decimal(value) -> Decimal:
    return Decimal(str(value if value not in (None, "") else "0"))


def _money(value) -> str:
    """An amount as a reader compares them: two places, and always positive.

    The two numbers in a mismatch come from different places - a database
    column and a parsed statement - and printing them however each arrived
    makes "1190.0 vs -1000.00" look like a formatting bug rather than the
    disagreement it is.
    """
    return "%.2f" % abs(_decimal(value))


def _tight(text: str) -> str:
    """A note to payee with its spaces removed.

    A bank wraps the note at 70 characters wherever it falls, so an invoice
    number can arrive split - `SUP- 9001` rather than `SUP-9001`.  Searching
    the tight form as well is the difference between matching that line and
    reporting it as unreferenced.
    """
    return "".join((text or "").split())


def references_in(line: dict) -> List[str]:
    """What this line could be quoting, best first.

    The structured reference is exact.  The note to payee is prose that a
    human typed into a payment form, so it is searched only when there is no
    structured one.
    """
    found = []
    if line.get("reference"):
        found.append(line["reference"])
    return found


def _open_payables(conn) -> List[dict]:
    rows = conn.execute(
        'SELECT * FROM "%s" WHERE "AccountingDocumentItemType" = ? '
        'AND "ClearingAccountingDocument" = ?' % ENTITY_TYPES[CUBE].table,
        (SUPPLIER_LINE, "")).fetchall()
    return [dict(row) for row in rows]


def _cleared_payables(conn) -> List[dict]:
    rows = conn.execute(
        'SELECT * FROM "%s" WHERE "AccountingDocumentItemType" = ? '
        'AND "ClearingAccountingDocument" <> ?' % ENTITY_TYPES[CUBE].table,
        (SUPPLIER_LINE, "")).fetchall()
    return [dict(row) for row in rows]


def _invoice_reference(conn, item: dict) -> str:
    """The supplier's own invoice number for the invoice this item came from.

    That is what a payment quotes, so it is what a statement line names.
    """
    row = conn.execute(
        'SELECT "SupplierInvoiceIDByInvcgParty" FROM "A_SupplierInvoice" '
        'WHERE "AccountingDocument" = ? AND "CompanyCode" = ? AND "FiscalYear" = ?',
        (item["AccountingDocument"], item["CompanyCode"],
         item["FiscalYear"])).fetchone()
    return (row["SupplierInvoiceIDByInvcgParty"] if row else "") or ""


def _quotes(reference: str, line: dict) -> bool:
    """Whether a statement line names this reference at all."""
    if not reference:
        return False
    if line.get("reference") and line["reference"].strip() == reference:
        return True
    note = line.get("note_to_payee") or ""
    return reference in note or _tight(reference) in _tight(note)


def _match(conn, line: dict, candidates: List[dict]) -> Dict[str, Any]:
    """The item this line pays, or why it pays none of them."""
    amount = line.get("amount")
    if amount is None:
        return {"item": None, "reason": "the line carries several amounts and "
                                        "none of them is named as the line's"}
    quoted = []
    for item in candidates:
        reference = _invoice_reference(conn, item)
        if _quotes(reference, line):
            quoted.append((item, reference))
    if not quoted:
        return {"item": None, "reason": "no open item quotes this reference"}

    wanted = abs(_decimal(amount))
    for item, reference in quoted:
        if abs(_decimal(item["AmountInTransactionCurrency"])) == wanted:
            return {"item": item, "reference": reference}
    # The reference found something and the amount did not agree. Say both
    # numbers: a part payment and a wrong payment look identical otherwise.
    item, reference = quoted[0]
    return {"item": None, "reason":
            "reference %s is item %s for %s, but the line is for %s"
            % (reference, item["AccountingDocument"],
               _money(item["AmountInTransactionCurrency"]), _money(amount))}


def _set_clearing(ctx, item: dict, values: dict) -> None:
    store.update(ctx.conn, ENTITY_TYPES[CUBE], {
        "AccountingDocument": item["AccountingDocument"],
        "CompanyCode": item["CompanyCode"],
        "FiscalYear": item["FiscalYear"],
        "AccountingDocumentItem": item["AccountingDocumentItem"],
    }, values, user=ctx.user)


def _self_clear(ctx, document: str, company: str, year: str) -> None:
    """A payment document's own supplier line is cleared by that document.

    Otherwise the payment posts a second payable line with nothing against
    it, and the next payment run sees a phantom open item and pays it. SAP
    clears both sides with the same clearing document; so does this.
    """
    rows = ctx.conn.execute(
        'SELECT * FROM "%s" WHERE "AccountingDocument" = ? AND "CompanyCode" = ? '
        'AND "FiscalYear" = ? AND "AccountingDocumentItemType" = ?'
        % ENTITY_TYPES[CUBE].table,
        (document, company, year, SUPPLIER_LINE)).fetchall()
    for row in rows:
        _set_clearing(ctx, dict(row), {
            "ClearingAccountingDocument": document,
            "ClearingDocFiscalYear": year,
            "ClearingItem": dict(row)["AccountingDocumentItem"],
            "ClearingIsReversed": False,
        })


def _clear(ctx, item: dict, line: dict, posting: str) -> dict:
    """Pay an open item: post the clearing document, then point the item at it."""
    amount = abs(_decimal(line.get("amount")))
    document, company, year = documents.post_journal_entry(ctx, {
        "CompanyCode": item["CompanyCode"],
        "AccountingDocumentType": "ZP",          # a payment, in FI terms
        "PostingDate": posting,
        "TransactionCurrency": item["TransactionCurrency"] or "EUR",
        "HeaderText": "Payment of %s" % item["AccountingDocument"],
        "ReferenceDocument": line.get("reference") or "",
    }, [
        # the payable goes away, the bank account pays for it
        {"Supplier": item["Supplier"], "Amount": float(amount),
         "Text": "Clearing %s" % item["AccountingDocument"]},
        {"GLAccount": "0000113100", "Amount": -float(amount), "Text": "Bank"},
    ])
    _set_clearing(ctx, item, {
        "ClearingAccountingDocument": document,
        "ClearingDate": posting,
        "ClearingCreationDate": posting,
        "ClearingItem": "000001",
        "ClearingDocFiscalYear": year,
        "ClearingIsReversed": False,
    })
    _self_clear(ctx, document, company, year)
    return {"document": document, "company": company, "year": year,
            "amount": amount}


def _reopen(ctx, item: dict, line: dict, posting: str) -> dict:
    """A returned payment: reverse the clearing and leave the item open.

    The clearing document is *removed from the item*, the way reversing a
    clearing in SAP puts it back among the open items - otherwise a payment
    run would never look at it again.  ``ClearingIsReversed`` stays set, so an
    item that was paid and returned can still be told apart from one nobody
    ever paid.  Without that, the two look identical and the difference is
    exactly what a treasury team is trying to see.
    """
    amount = abs(_decimal(line.get("amount")))
    document, company, year = documents.post_journal_entry(ctx, {
        "CompanyCode": item["CompanyCode"],
        "AccountingDocumentType": "ZP",
        "PostingDate": posting,
        "TransactionCurrency": item["TransactionCurrency"] or "EUR",
        "HeaderText": "Return of %s" % item["AccountingDocument"],
        "ReferenceDocument": line.get("reference") or "",
    }, [
        {"GLAccount": "0000113100", "Amount": float(amount), "Text": "Bank"},
        {"Supplier": item["Supplier"], "Amount": -float(amount),
         "Text": "Payment returned"},
    ])
    _set_clearing(ctx, item, {
        "ClearingAccountingDocument": "",
        "ClearingDate": None,
        "ClearingItem": "",
        "ClearingDocFiscalYear": "",
        "ClearingIsReversed": True,
    })
    # The reversal's own supplier line is not a new debt either.
    _self_clear(ctx, document, company, year)
    return {"document": document, "amount": amount}


def _previous(conn, account: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM bank_statement WHERE account = ? ORDER BY id DESC LIMIT 1",
        (account,)).fetchone()
    return dict(row) if row else None


def _remember(conn, parsed: dict, findings: List[str]) -> None:
    conn.execute(
        "INSERT INTO bank_statement(account,statement,statement_date,opening,"
        "closing,interim,findings,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (parsed["account"]["number"], parsed.get("statement") or "",
         parsed.get("date") or "",
         str(parsed["opening"]) if parsed["opening"] is not None else None,
         str(parsed["closing"]) if parsed["closing"] is not None else None,
         1 if parsed.get("interim") else 0, json.dumps(findings),
         _dt.datetime.utcnow().isoformat()))
    conn.commit()


def check_balances(conn, parsed: dict) -> List[str]:
    """What the statement says about itself, and about the one before it.

    Two different failures, so two different sentences. A statement that does
    not add up is a corrupt or partial file. A statement whose opening balance
    does not follow the last closing balance means one went missing, and the
    payments on it were never seen at all.
    """
    findings = []
    own = statement_reader.balances_add_up(parsed)
    if own:
        findings.append(own)

    if parsed.get("interim"):
        # An interim statement is a snapshot inside a period, not the next one
        # in the sequence; chaining it would invent a gap that is not there.
        return findings

    previous = _previous(conn, parsed["account"]["number"])
    opening = parsed.get("opening")
    if previous and previous.get("closing") is not None and opening is not None:
        if not previous.get("interim") and _decimal(previous["closing"]) != opening:
            findings.append(
                "Statement %s opens at %s, but statement %s closed at %s: a "
                "statement is missing" % (parsed.get("statement") or "(unnumbered)",
                                          opening, previous["statement"] or "(unnumbered)",
                                          previous["closing"]))
    return findings


def apply_statement(ctx, body: bytes) -> dict:
    """Post a bank statement against the open items it pays."""
    parsed = statement_reader.parse(body)
    posting = parsed.get("date") or _dt.date.today().isoformat()
    findings = check_balances(ctx.conn, parsed)

    cleared, reopened, unprocessed = [], [], []
    for line in parsed["lines"]:
        side = line.get("side")
        if side == "debit":
            outcome = _match(ctx.conn, line, _open_payables(ctx.conn))
            if outcome["item"] is None:
                unprocessed.append({"LINE": line["line"], "REASON": outcome["reason"]})
                continue
            posted = _clear(ctx, outcome["item"], line, posting)
            cleared.append({
                "LINE": line["line"], "REFERENCE": outcome["reference"],
                "ACCOUNTINGDOCUMENT": outcome["item"]["AccountingDocument"],
                "CLEARINGDOCUMENT": posted["document"],
                "AMOUNT": str(posted["amount"]),
            })
        elif side == "credit":
            outcome = _match(ctx.conn, line, _cleared_payables(ctx.conn))
            if outcome["item"] is None:
                unprocessed.append({"LINE": line["line"], "REASON": outcome["reason"]})
                continue
            posted = _reopen(ctx, outcome["item"], line, posting)
            reopened.append({
                "LINE": line["line"], "REFERENCE": outcome["reference"],
                "ACCOUNTINGDOCUMENT": outcome["item"]["AccountingDocument"],
                "REVERSALDOCUMENT": posted["document"],
                "AMOUNT": str(posted["amount"]),
            })
        else:
            unprocessed.append({
                "LINE": line["line"],
                "REASON": "the line does not say whether it is money in or out"})

    _remember(ctx.conn, parsed, findings)
    applied = {
        "STATEMENT": parsed.get("statement") or "",
        "ACCOUNT": parsed["account"]["number"],
        "CLEARED": cleared,
        "REOPENED": reopened,
        "UNPROCESSED": unprocessed,
        "FINDINGS": findings,
        "MESSAGE": "Statement %s: %d item(s) cleared, %d reopened, %d line(s) "
                   "unprocessed" % (parsed.get("statement") or "(unnumbered)",
                                    len(cleared), len(reopened), len(unprocessed)),
    }
    return applied
