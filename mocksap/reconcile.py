"""Matching a bank statement to the open items it pays.

`statement.py` says what a FINSTA01 claims.  This decides what to do about
it, which is the half that can be wrong in expensive ways.

Electronic bank statement processing in miniature.  A debit line - money that
left the account - clears the open item it names, if the reference matches
*and* the amount and its currency match *and* only one party's item does: an
amount without a currency is not an amount, two that happen to be equal are
not a payment, and an invoice number is a supplier's own sequence, so two
suppliers can both have an `INV-100` and nothing on the line says which was
paid.  A credit line that *says it is a returned payment* and carries the
reference of an item already cleared reverses the clearing and leaves the item
open again, which is the only thing that makes a returned payment visible in
SAP.  A credit that says it is money arriving reverses nothing: it is a
customer paying, and it clears the open receivable it quotes on the terms a
debit clears a payable (#175).  One that says neither does neither and is
reported: money in has two
readings that are opposites, and a credit used to be read as a return on the
strength of nothing but its sign, so a refund or a supplier returning an
overpayment that happened to quote an invoice already paid reopened it and the
next payment run paid it twice (#89).  `statement.py` says how a line
declares which it is.

**A part payment is refused, not posted.**  A customer chooses what they pay
and rarely pays the invoice exactly, and SAP has two answers - clear the item
and leave a residual one for the difference, or post the payment against an
item that stays open - which behave differently on the next statement.  This
mock does neither.  A credit for anything other than the receivable's exact
amount clears nothing and the reason names both figures, so the difference is
visible and whoever reads it decides what it was: a mock that built a residual
item would be deciding a short payment was a deduction, and one that posted a
part payment would be deciding it was an instalment.

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

from . import (clock, db, documents, money, outcome,
               statement as statement_reader, store)
from .odata import SapError
from .schema import ENTITY_TYPES

CUBE = "A_OperationalAcctgDocItemCube"

# The two sides an open item can be on, in SAP's account-type letters. A
# payable is money we owe a supplier; a receivable is money a customer owes us.
SUPPLIER_LINE = documents.ITEM_TYPE_SUPPLIER
CUSTOMER_LINE = documents.ITEM_TYPE_CUSTOMER


def _money(value) -> str:
    """An amount as a reader compares them: two places, and always positive.

    The two numbers in a mismatch come from different places - a database
    column and a parsed statement - and printing them however each arrived
    makes "1190.0 vs -1000.00" look like a formatting bug rather than the
    disagreement it is.
    """
    return money.text(abs(money.of(value)), money.CURRENCY_SCALE)


def _tight(text: str) -> str:
    """A note to payee with its spaces removed.

    A bank wraps the note at 70 characters wherever it falls, so an invoice
    number can arrive split - `SUP- 9001` rather than `SUP-9001`.  Searching
    the tight form as well is the difference between matching that line and
    reporting it as unreferenced.
    """
    return "".join((text or "").split())


def references_in(line: dict) -> List[str]:
    """What this line states it is paying, exactly.

    The structured reference is a field a bank filled in to say which document
    the money settles, so it is matched exactly and nothing else on the line
    is consulted.  Empty when the line carries none, which is the only
    circumstance in which the note to payee - prose a human typed into a
    payment form - is worth searching.

    That ordering is the whole of the rule `_quotes` applies, and it lives
    here so there is one place to read it.  It used to live only in this
    docstring: the function had no callers and the matching path searched the
    note even when the reference was there and disagreed (#86).
    """
    found = []
    reference = (line.get("reference") or "").strip()
    if reference:
        found.append(reference)
    return found


def _open_items(conn, item_type: str = SUPPLIER_LINE) -> List[dict]:
    """Items of one account type that nothing has cleared yet."""
    rows = conn.execute(
        'SELECT * FROM "%s" WHERE "AccountingDocumentItemType" = ? '
        'AND "ClearingAccountingDocument" = ?' % ENTITY_TYPES[CUBE].table,
        (item_type, "")).fetchall()
    return [dict(row) for row in rows]


def _cleared_items(conn, item_type: str = SUPPLIER_LINE) -> List[dict]:
    """Items of one account type that something has already cleared."""
    rows = conn.execute(
        'SELECT * FROM "%s" WHERE "AccountingDocumentItemType" = ? '
        'AND "ClearingAccountingDocument" <> ?' % ENTITY_TYPES[CUBE].table,
        (item_type, "")).fetchall()
    return [dict(row) for row in rows]


def _invoice_reference(conn, item: dict) -> str:
    """The document number the payer for this item would quote.

    Each side quotes the other party's document, not its own, and the two
    sides keep that number in different places. A supplier bills us and we
    pay quoting *their* invoice number, which is why the payable side reads
    `SupplierInvoiceIDByInvcgParty` rather than the invoice's own key. We
    bill a customer and they pay quoting the billing document we sent, which
    is its own key.

    Dispatch is on the item rather than on an argument, because an item
    already knows which side it is on and a caller passing the other one
    would be asking the wrong question.
    """
    if item.get("AccountingDocumentItemType") == CUSTOMER_LINE:
        # A_BillingDocument is keyed by BillingDocument alone - it carries no
        # company code or fiscal year - so the accounting document is the
        # whole join here, unlike the supplier side below.
        row = conn.execute(
            'SELECT "BillingDocument" FROM "A_BillingDocument" '
            'WHERE "AccountingDocument" = ?',
            (item["AccountingDocument"],)).fetchone()
        return (row["BillingDocument"] if row else "") or ""
    row = conn.execute(
        'SELECT "SupplierInvoiceIDByInvcgParty" FROM "A_SupplierInvoice" '
        'WHERE "AccountingDocument" = ? AND "CompanyCode" = ? AND "FiscalYear" = ?',
        (item["AccountingDocument"], item["CompanyCode"],
         item["FiscalYear"])).fetchone()
    return (row["SupplierInvoiceIDByInvcgParty"] if row else "") or ""


def _quotes(reference: str, line: dict) -> bool:
    """Whether a statement line names this reference.

    A structured reference ends the question.  `INV-1` is a substring of
    `INV-10`, so a note naming the invoice that was paid also named a
    different one, and searching the note after the reference had disagreed
    settled the wrong invoice while leaving the right one open for the next
    payment run to pay again (#86).  A bank that told us which document it
    paid is believed over prose that merely contains a number.

    The note is read only for a line that states no reference, which is the
    ordering `references_in` holds.
    """
    if not reference:
        return False
    stated = references_in(line)
    if stated:
        return reference in stated
    note = line.get("note_to_payee") or ""
    return reference in note or _tight(reference) in _tight(note)


# The field naming the party an item's account belongs to, by the side the
# item is on. A payable names the supplier we owe; a receivable, the customer.
_PARTY_FIELD = {CUSTOMER_LINE: ("customer", "Customer")}


def _party_of(item: dict) -> str:
    """Whose account this item sits on, as a reader names it.

    Dispatch is on the item, the way `_invoice_reference` dispatches: an item
    already knows which side it is on. The label is part of the answer, so a
    supplier and a customer who happen to share a number are not read as one
    party. Empty for an item naming neither, which is not something two
    candidates can be told apart by.
    """
    label, field = _PARTY_FIELD.get(
        item.get("AccountingDocumentItemType"), ("supplier", "Supplier"))
    value = item.get(field) or ""
    return "%s %s" % (label, value) if value else ""


def _named(item: dict) -> str:
    """One candidate item, as a refusal naming several of them has to.

    The document number alone stops being enough once the reason for refusing
    is that two parties are involved: naming them is the difference between a
    line somebody can now settle by hand and one they have to go and look up.
    """
    party = _party_of(item)
    return ("item %s of %s" % (item["AccountingDocument"], party) if party
            else "item %s" % item["AccountingDocument"])


def _in_words(parts: List[str]) -> str:
    """A list as a person reads one: `a`, `a and b`, `a, b and c`."""
    if len(parts) < 3:
        return " and ".join(parts)
    return "%s and %s" % (", ".join(parts[:-1]), parts[-1])


def _currency_of(item: dict) -> str:
    """The currency an open item is owed in, compared as a file writes one."""
    return (item.get("TransactionCurrency") or "").strip().upper()


def _priced(amount, currency: str) -> str:
    """An amount as a reason has to give one: the number and its currency.

    Two amounts that happen to be equal are not a payment (#88), so a
    sentence naming a figure without its currency is not saying what
    disagreed - and a mock whose refusals read `1190.00 vs 1190.00` would be
    unreadable in exactly the case worth reading.
    """
    return "%s %s" % (_money(amount), currency or "(no currency)")


def _item_price(item: dict) -> str:
    return _priced(item["AmountInTransactionCurrency"], _currency_of(item))


def _claimed_by(item: dict) -> str:
    """The payment run that has this item in flight, or "" if none has."""
    return (item.get("PaymentRunID") or "").strip()


def _match(conn, line: dict, candidates: List[dict]) -> Dict[str, Any]:
    """The item this line pays, or why it pays none of them.

    The reference, the amount *and its currency* narrow the candidates; the
    party decides whether what is left is one answer or none.  An amount
    without a currency is not an amount, and two amounts that happen to be
    equal are not a payment (#88): a line for 1190.00 USD does not pay a
    payable of 1190.00 EUR, and the refusal prints both currencies, because
    that is the one disagreement which reads as agreement.  An invoice number is a
    supplier's own sequence and means nothing across suppliers, so two of
    them numbering an invoice `INV-100` is ordinary - and nothing this mock
    reads off a statement line separates them: `E1IDPF1` gives it a
    reference, a note to payee and amounts, and the account `statement.py`
    takes from the file is the one being reconciled, not the payee's.  So
    where two parties' items fit the line equally well, neither is cleared
    and the reason names both (#87).

    Unless one of them says so itself.  A payment run writes its claim on an
    item before it sends the payment (#90), which is the run's own word that
    this is the item it paid - exactly what the line leaves out.  So where
    the parties tie and exactly one of the fitting items carries a claim,
    that item is the one paid (#173).  No claim, or more than one, decides
    nothing, and the line is refused as before.  Without this, one invoice
    number shared by two suppliers left the first supplier's paid item open
    and claimed for good, and a payment run that will not send two items
    with one reference never paid the second supplier at all.

    Taking the first instead cleared one supplier's invoice with another's
    money and left an item open for the next payment run to pay a second
    time.  Since #107 it also decided which supplier's payment document the
    invoice landed in, so the wrong party was credited as well.

    Two items of the *same* party sharing a reference and an amount is a
    different fault - the invoice was posted twice - and is still settled
    once, by whichever is found first.  Either one clears the right party for
    the right money, so refusing there would report a line that is not the
    reconciliation gap this reports.
    """
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
        return {"item": None, "unquoted": True,
                "reason": "no open item quotes this reference"}

    wanted = abs(money.of(amount))
    paid_in = (line.get("currency") or "").strip().upper()
    if not paid_in:
        # `statement.py` falls back to the account's own FIIKWAER when a line
        # carries no CUXWAERZ, so this is a statement that names no currency
        # anywhere. There is nothing to compare and nothing to assume.
        return {"item": None, "reason":
                "neither the line nor the account it is on names a currency, "
                "and an amount without one is not an amount"}
    equal = [(item, reference) for item, reference in quoted
             if abs(money.of(item["AmountInTransactionCurrency"])) == wanted]
    paying = [(item, reference) for item, reference in equal
              if _currency_of(item) == paid_in]
    if len({_party_of(item) for item, _ in paying}) > 1:
        # The line cannot say which party was paid, but an item can: the one
        # a payment run sent to the bank carries that run's claim, written
        # before the file went (#90). Exactly one claimed item among those
        # that fit is therefore the one this line paid (#173).
        claimed = [(item, reference) for item, reference in paying
                   if _claimed_by(item)]
        if len(claimed) == 1:
            item, reference = claimed[0]
            return {"item": item, "reference": reference}
        reason = ("this line's reference is %s, each for %s, and nothing else "
                  "on the line says which was paid"
                  % (_in_words([_named(item) for item, _ in paying]),
                     _priced(amount, paid_in)))
        if claimed:
            # More than one claim is no better than none, and worth saying:
            # it is a different thing to go and look for.
            reason += (", nor does a payment run's claim: %s carry one"
                       % _in_words(["item %s (%s)" % (item["AccountingDocument"],
                                                      _claimed_by(item))
                                    for item, _ in claimed]))
        return {"item": None, "reason": reason}
    if paying:
        item, reference = paying[0]
        return {"item": item, "reference": reference}
    # An equal number in another currency gets its own sentence. It is the
    # one disagreement that reads as agreement, which is how a bank writing
    # the wrong currency code and a mock not looking at it cancelled out.
    if equal:
        return {"item": None, "reason":
                "this line is for %s, and %s: two amounts that happen to be "
                "equal are not a payment"
                % (_priced(amount, paid_in),
                   _in_words(["%s is for %s" % (_named(item), _item_price(item))
                              for item, _ in equal]))}
    # The reference found something and the amount did not agree. Say both
    # numbers: a part payment and a wrong payment look identical otherwise.
    if len(quoted) == 1:
        item, reference = quoted[0]
        return {"item": None, "reason":
                "reference %s is item %s for %s, but the line is for %s"
                % (reference, item["AccountingDocument"], _item_price(item),
                   _priced(amount, paid_in))}
    return {"item": None, "reason":
            "this line's reference is %s, but the line is for %s"
            % (_in_words(["%s for %s" % (_named(item), _item_price(item))
                          for item, _ in quoted]),
               _priced(amount, paid_in))}


def _set_clearing(ctx, item: dict, values: dict) -> None:
    store.update(ctx.conn, ENTITY_TYPES[CUBE], {
        "AccountingDocument": item["AccountingDocument"],
        "CompanyCode": item["CompanyCode"],
        "FiscalYear": item["FiscalYear"],
        "AccountingDocumentItem": item["AccountingDocumentItem"],
    }, values, user=ctx.user)
    # Every route through here changes whether the item is waiting to be paid
    # - cleared by a payment, cleared as a payment's own line, or opened again
    # by a return - and a payment run's claim is a statement about an item
    # that is waiting (#90). So it goes, on both rows that carry it.
    store.release_payment_run(ctx.conn, item)


def _self_clear(ctx, document: str, company: str, year: str, posting: str,
                item_type: str = SUPPLIER_LINE) -> None:
    """A payment document's own subledger line is cleared by that document.

    Otherwise the payment posts a second open line with nothing against it,
    and the next payment run sees a phantom open item and pays it. SAP clears
    both sides with the same clearing document; so does this.

    `item_type` is the side the payment document itself posted to, which is
    the same side as the item being settled: paying a supplier posts another
    supplier line, and collecting from a customer posts another customer one.

    It is cleared on `posting`, the day the document itself posted. A line
    that names its clearing document and carries no clearing date is cleared
    according to one field and open according to the other, and a report
    that asks "cleared on or before the key date" drops it (#160).
    """
    rows = ctx.conn.execute(
        'SELECT * FROM "%s" WHERE "AccountingDocument" = ? AND "CompanyCode" = ? '
        'AND "FiscalYear" = ? AND "AccountingDocumentItemType" = ?'
        % ENTITY_TYPES[CUBE].table,
        (document, company, year, item_type)).fetchall()
    for row in rows:
        _set_clearing(ctx, dict(row), {
            "ClearingAccountingDocument": document,
            "ClearingDate": posting,
            "ClearingCreationDate": posting,
            "ClearingDocFiscalYear": year,
            "ClearingItem": dict(row)["AccountingDocumentItem"],
            "ClearingIsReversed": False,
        })


def _item_key(item: dict) -> tuple:
    """The four fields that identify one accounting document item."""
    return (item["AccountingDocument"], item["CompanyCode"],
            item["FiscalYear"], item["AccountingDocumentItem"])


def _payee_of(item: dict) -> tuple:
    """What decides which payment document settles this item.

    A payment run pays a *supplier*, not an invoice: one document settles
    everything of theirs that fell due, which is the whole reason the supplier
    has to be told which invoices the credit covers (#107). The company code
    and the currency are in the key because a document carries one of each in
    its header, so items that disagree on either cannot share a payment
    however much they share a payee.

    A customer pays us the same way, one credit for what they owe, so the
    party is whichever one the item's side names - labelled, so a supplier and
    a customer who share a number are two payees.

    The last element is empty for a real payable or receivable and the item's
    own document for one naming no party, so rows that are neither are not
    merged into one payment on the strength of both being blank.
    """
    party = _party_of(item)
    return (party, item["CompanyCode"],
            item["TransactionCurrency"] or "EUR",
            "" if party else item["AccountingDocument"])


def _payment_text(settling: List[dict]) -> str:
    """A payment document's header text, which SAP gives 25 characters.

    `BKTXT` is `CHAR(25)`, and this mock enforces the declared length rather
    than truncating, so naming the supplier here does not fit and does not
    need to: the supplier is on every line of the document.
    """
    if len(settling) == 1:
        return "Payment of %s" % settling[0]["item"]["AccountingDocument"]
    return "Payment of %d invoices" % len(settling)


def _pay(ctx, settling: List[dict], posting: str, statement: str) -> List[dict]:
    """Post one payment document for everything this statement settles for one payee.

    One supplier line per invoice and one bank line for the total, which is how
    a payment run pays: the supplier receives a single credit on their account,
    and the remittance advice is what tells them which invoices it covers.

    `post_journal_entry` numbers a document's items in the order it is given
    them, so the invoice settled by the *n*th supplier line points at item *n*.
    That is what `ClearingItem` is for, and a hardcoded `000001` could only
    ever be right while a payment settled exactly one invoice.
    """
    first = settling[0]["item"]
    amounts = [abs(money.of(s["line"].get("amount"))) for s in settling]
    total = sum(amounts, Decimal("0"))
    # the payables go away, the bank account pays for all of them at once
    lines = [{"Supplier": s["item"].get("Supplier") or "",
              "Amount": amount,
              "Text": "Clearing %s" % s["item"]["AccountingDocument"]}
             for s, amount in zip(settling, amounts)]
    lines.append({"GLAccount": "0000113100", "Amount": -total,
                  "Text": "Bank"})
    document, company, year = documents.post_journal_entry(ctx, {
        "CompanyCode": first["CompanyCode"],
        "AccountingDocumentType": "ZP",          # a payment, in FI terms
        "PostingDate": posting,
        "TransactionCurrency": first["TransactionCurrency"] or "EUR",
        "HeaderText": _payment_text(settling),
        # A payment's own reference is the statement that produced it, not one
        # of the invoices it covers - which could only have been right while
        # there was exactly one of those. Each invoice's reference stays on its
        # own item, and in the outcome's REFERENCE.
        "ReferenceDocument": statement,
    }, lines)
    paid = []
    for index, (s, amount) in enumerate(zip(settling, amounts), start=1):
        _set_clearing(ctx, s["item"], {
            "ClearingAccountingDocument": document,
            "ClearingDate": posting,
            "ClearingCreationDate": posting,
            "ClearingItem": str(index).zfill(6),
            "ClearingDocFiscalYear": year,
            "ClearingIsReversed": False,
        })
        paid.append({"settling": s, "document": document, "amount": amount})
    _self_clear(ctx, document, company, year, posting)
    return paid


def _receipt_text(settling: List[dict]) -> str:
    """A receipt document's header text, inside `BKTXT`'s 25 characters."""
    if len(settling) == 1:
        return "Receipt for %s" % settling[0]["item"]["AccountingDocument"]
    return "Receipt for %d invoices" % len(settling)


def _collect(ctx, settling: List[dict], posting: str, statement: str) -> List[dict]:
    """Post one receipt document for everything one customer's money settles.

    `_pay` the other way round. The bank account gains the total and the
    customer's account loses one line per invoice, so the receivable - a
    debit the customer owed - is met by a credit of the same size. The signs
    are the whole difference: a receipt posted with a payment's signs would
    double what the customer owes and take the money out of the bank.

    `DZ` is SAP's customer payment, as `ZP` is the payment run's. The
    document's own customer lines are cleared by the document, for the reason
    `_self_clear` gives: left open, they are credits on the customer's account
    that nothing is against, and the customer appears to be owed money.
    """
    first = settling[0]["item"]
    amounts = [abs(money.of(s["line"].get("amount"))) for s in settling]
    total = sum(amounts, Decimal("0"))
    lines = [{"Customer": s["item"].get("Customer") or "",
              "Amount": -amount,
              "Text": "Clearing %s" % s["item"]["AccountingDocument"]}
             for s, amount in zip(settling, amounts)]
    lines.append({"GLAccount": "0000113100", "Amount": total, "Text": "Bank"})
    document, company, year = documents.post_journal_entry(ctx, {
        "CompanyCode": first["CompanyCode"],
        "AccountingDocumentType": "DZ",          # a customer payment
        "PostingDate": posting,
        "TransactionCurrency": first["TransactionCurrency"] or "EUR",
        "HeaderText": _receipt_text(settling),
        "ReferenceDocument": statement,
    }, lines)
    received = []
    for index, (s, amount) in enumerate(zip(settling, amounts), start=1):
        _set_clearing(ctx, s["item"], {
            "ClearingAccountingDocument": document,
            "ClearingDate": posting,
            "ClearingCreationDate": posting,
            "ClearingItem": str(index).zfill(6),
            "ClearingDocFiscalYear": year,
            "ClearingIsReversed": False,
        })
        received.append({"settling": s, "document": document, "amount": amount})
    _self_clear(ctx, document, company, year, posting, CUSTOMER_LINE)
    return received


def _reopen(ctx, item: dict, line: dict, posting: str) -> dict:
    """A returned payment: reverse the clearing and leave the item open.

    The clearing document is *removed from the item*, the way reversing a
    clearing in SAP puts it back among the open items - otherwise a payment
    run would never look at it again.  ``ClearingIsReversed`` stays set, so an
    item that was paid and returned can still be told apart from one nobody
    ever paid.  Without that, the two look identical and the difference is
    exactly what a treasury team is trying to see.
    """
    amount = abs(money.of(line.get("amount")))
    document, company, year = documents.post_journal_entry(ctx, {
        "CompanyCode": item["CompanyCode"],
        "AccountingDocumentType": "ZP",
        "PostingDate": posting,
        "TransactionCurrency": item["TransactionCurrency"] or "EUR",
        "HeaderText": "Return of %s" % item["AccountingDocument"],
        "ReferenceDocument": line.get("reference") or "",
    }, [
        {"GLAccount": "0000113100", "Amount": amount, "Text": "Bank"},
        {"Supplier": item["Supplier"], "Amount": -amount,
         "Text": "Payment returned"},
    ])
    _set_clearing(ctx, item, {
        "ClearingAccountingDocument": "",
        "ClearingDate": None,
        # The day the clearing was entered goes with the clearing. Left
        # behind, an item that is open again still said when it was cleared.
        "ClearingCreationDate": None,
        "ClearingItem": "",
        "ClearingDocFiscalYear": "",
        "ClearingIsReversed": True,
    })
    # The reversal's own supplier line is not a new debt either.
    _self_clear(ctx, document, company, year, posting)
    return {"document": document, "amount": amount}


def settlement_of(ctx, document: str) -> dict:
    """What one payment document settled, as a remittance advice needs it (#106).

    The payer's side of what a supplier has to be told: who was paid, the day
    the payment settled, and one row per invoice carrying the number that
    supplier quotes and what came off it.

    The rows are found through `ClearingItem` rather than by re-matching
    anything: an invoice points at the line of the payment document that paid
    it, so each row's amount is what that payment actually paid for it, not
    what the invoice happened to be for. The two can differ once there is a
    short payment, and the advice has to say what was paid.

    The total is the sum of those rows and never a figure of its own, so an
    advice built from this cannot claim a total its parts do not add up to -
    which is one of the two things mock-edi's reader refuses. It is still
    checked against the payment's own credit to the bank, because if those
    disagree the payment does not balance and an advice from it would be a
    lie the supplier acts on.
    """
    items = ENTITY_TYPES[CUBE].table
    header = ctx.conn.execute(
        'SELECT * FROM "A_JournalEntry" WHERE "AccountingDocument" = ?',
        (document.zfill(10),)).fetchone()
    if header is None:
        header = ctx.conn.execute(
            'SELECT * FROM "A_JournalEntry" WHERE "AccountingDocument" = ?',
            (document,)).fetchone()
    if header is None:
        raise SapError("Accounting document %s does not exist" % document, 404)
    header = dict(header)
    paying = header["AccountingDocument"]

    settled = [dict(row) for row in ctx.conn.execute(
        'SELECT * FROM "%s" WHERE "ClearingAccountingDocument" = ? '
        'AND "AccountingDocument" <> ? AND "AccountingDocumentItemType" = ? '
        'ORDER BY "ClearingItem"' % items,
        (paying, paying, SUPPLIER_LINE)).fetchall()]
    if not settled:
        raise SapError(
            "Accounting document %s settled nothing, so there is no payment to "
            "advise anyone of" % paying, 400)

    lines = {dict(row)["AccountingDocumentItem"]: dict(row)
             for row in ctx.conn.execute(
                 'SELECT * FROM "%s" WHERE "AccountingDocument" = ?' % items,
                 (paying,)).fetchall()}
    rows, total = [], Decimal("0")
    for item in settled:
        paid = lines.get(item["ClearingItem"])
        amount = money.of((paid or item)["AmountInTransactionCurrency"])
        total += amount
        rows.append({
            "invoice": item["AccountingDocument"],
            "reference": _invoice_reference(ctx.conn, item),
            "amount": amount,
            "item": item["ClearingItem"],
        })

    bank = [row for row in lines.values() if not (row.get("Supplier") or "")]
    credited = sum((money.of(row["AmountInTransactionCurrency"]) for row in bank),
                   Decimal("0"))
    if bank and credited != total:
        raise SapError(
            "Accounting document %s credits the bank %s but settles %s, so it "
            "does not balance" % (paying, _money(credited), _money(total)), 409)

    payees = {item.get("Supplier") or "" for item in settled}
    return {
        "document": paying,
        "company": header["CompanyCode"],
        "year": header["FiscalYear"],
        "payee": sorted(payees)[0],
        "currency": header["TransactionCurrency"] or "EUR",
        "settled": header["PostingDate"],
        "reference": header["ReferenceDocument"] or "",
        "total": total,
        "rows": rows,
    }


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
         clock.stamp()))
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
        if not previous.get("interim") and money.of(previous["closing"]) != opening:
            findings.append(
                "Statement %s opens at %s, but statement %s closed at %s: a "
                "statement is missing" % (parsed.get("statement") or "(unnumbered)",
                                          opening, previous["statement"] or "(unnumbered)",
                                          previous["closing"]))
    return findings




def _in_line_order(rows: List[tuple]) -> List[dict]:
    """Every list the outcome carries reads in the statement's own line order.

    Grouping posts by payee, so the order things happen in is no longer the
    order they were read in. What a client is shown is the statement, not the
    posting sequence.
    """
    return [row for _, row in sorted(rows, key=lambda pair: pair[0])]


def apply_statement(ctx, body: bytes, docnum: str = "") -> dict:
    """Post a bank statement against the open items it pays.

    ``docnum`` is the inbound IDoc this statement arrived on. It is what the
    outcome is filed under, so a later read of that IDoc can say what posting
    it did - and so the clearing can be found from the invoice's side. Empty
    means nothing is filed, which is what a caller with no IDoc gets.
    """
    parsed = statement_reader.parse(body)
    posting = parsed.get("date") or clock.today().isoformat()
    findings = check_balances(ctx.conn, parsed)
    statement = parsed.get("statement") or ""

    cleared, reopened, unprocessed = [], [], []
    settling: List[dict] = []
    receiving: List[dict] = []
    returning: List[tuple] = []
    claimed = set()
    for seq, line in enumerate(parsed["lines"]):
        side = line.get("side")
        if side == "debit":
            # An item a line above has already spoken for is not open to this
            # one. Clearing used to post as each line was read, which took the
            # item out of `_open_items` for free; the payment is now posted
            # once per payee after every line has been read, so what is
            # already claimed is tracked here instead.
            candidates = [item for item in _open_items(ctx.conn)
                          if _item_key(item) not in claimed]
            matched = _match(ctx.conn, line, candidates)
            if matched["item"] is None:
                unprocessed.append((seq, {"LINE": line["line"],
                                          "REASON": matched["reason"]}))
                continue
            claimed.add(_item_key(matched["item"]))
            settling.append({"seq": seq, "line": line,
                             "item": matched["item"],
                             "reference": matched["reference"]})
        elif side == "credit":
            # Money in has two readings and they are opposites, so the line
            # has to say which (#89). Undeclared is read as neither: guessing
            # `return` pays an invoice twice, and guessing `receipt` hides a
            # payment that genuinely came back.
            kind = line.get("kind")
            if kind == "return":
                returning.append((seq, line))
            elif kind == "receipt":
                # A customer paying. The candidates are the receivables and
                # never the payables: money arriving that quotes an invoice of
                # a supplier's is a refund, and clears nothing of ours (#89).
                candidates = [item for item in _open_items(ctx.conn, CUSTOMER_LINE)
                              if _item_key(item) not in claimed]
                matched = _match(ctx.conn, line, candidates)
                if matched["item"] is None:
                    reason = matched["reason"]
                    if matched.get("unquoted"):
                        # Said as what it is. Money that arrived and settled
                        # nothing is the line most worth finding, and "no
                        # open item" reads as though nothing happened.
                        reason = ("this line is money arriving and no open "
                                  "receivable quotes its reference, so it is "
                                  "applied to nothing")
                    unprocessed.append((seq, {"LINE": line["line"],
                                              "REASON": reason}))
                    continue
                claimed.add(_item_key(matched["item"]))
                receiving.append({"seq": seq, "line": line,
                                  "item": matched["item"],
                                  "reference": matched["reference"]})
            else:
                unprocessed.append((seq, {
                    "LINE": line["line"],
                    "REASON": "this line is money in and does not say which "
                              "kind: a payment of ours coming back reopens "
                              "the invoice, money arriving does not, and "
                              "guessing pays the invoice twice"}))
        else:
            unprocessed.append((seq, {
                "LINE": line["line"],
                "REASON": "the line does not say whether it is money in or out"}))

    # What we paid and what we were paid are posted separately however the
    # parties are numbered: one document cannot be both a payment and a
    # receipt, and `_payee_of` labels the party so the two never share a key.
    for post, lines in ((_pay, settling), (_collect, receiving)):
        by_payee: Dict[tuple, List[dict]] = {}
        for item in lines:
            by_payee.setdefault(_payee_of(item["item"]), []).append(item)
        for group in by_payee.values():
            for paid in post(ctx, group, posting, statement):
                settled = paid["settling"]
                cleared.append((settled["seq"], {
                    "LINE": settled["line"]["line"],
                    "REFERENCE": settled["reference"],
                    "ACCOUNTINGDOCUMENT": settled["item"]["AccountingDocument"],
                    "CLEARINGDOCUMENT": paid["document"],
                    "AMOUNT": str(paid["amount"]),
                }))

    # The returns are posted after the payments. A statement that pays an
    # invoice and takes the money back now names the clearing it reverses
    # whichever order those two lines are in; while clearing posted line by
    # line, a credit could only see a clearing made by a line above it.
    for seq, line in returning:
        matched = _match(ctx.conn, line, _cleared_items(ctx.conn))
        if matched["item"] is None:
            unprocessed.append((seq, {"LINE": line["line"],
                                      "REASON": matched["reason"]}))
            continue
        posted = _reopen(ctx, matched["item"], line, posting)
        reopened.append((seq, {
            "LINE": line["line"], "REFERENCE": matched["reference"],
            "ACCOUNTINGDOCUMENT": matched["item"]["AccountingDocument"],
            "REVERSALDOCUMENT": posted["document"],
            "AMOUNT": str(posted["amount"]),
        }))

    cleared = _in_line_order(cleared)
    reopened = _in_line_order(reopened)
    unprocessed = _in_line_order(unprocessed)

    _remember(ctx.conn, parsed, findings)
    applied = {
        "STATEMENT": statement,
        "ACCOUNT": parsed["account"]["number"],
        "CLEARED": cleared,
        "REOPENED": reopened,
        "UNPROCESSED": unprocessed,
        "FINDINGS": findings,
        "MESSAGE": outcome.statement_message(statement, cleared, reopened,
                                            unprocessed),
    }
    if docnum:
        outcome.file_statement(ctx.conn, docnum, applied)
    return applied
