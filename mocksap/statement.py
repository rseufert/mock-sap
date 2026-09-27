"""Bank statements: what an inbound FINSTA01 says, before anything acts on it.

A bank statement reaches SAP as a ``FINSTA01`` IDoc with message type
``FINSTA``.  This module reads one and says what it claims - the account, the
balances, and each statement line with its reference, note to payee and
amounts - and nothing more.  Clearing and reopening open items is somebody
else's job; this is the part that has to be right first.

The segment layout is SAP's FINSTA01 definition, not a guess:

    E1IDKU1                  header (mandatory); BGMREF = the statement number
      E1EDK02                header reference data (QUALF + BELNR)
      E1EDK03                header dates (IDDAT 026 = statement date)
      E1IDB02                the account: FIIBKENN, FIIKONTO, FIIBLAND, FIIKWAER
      E1IDPF1                a statement line
        E1EDP02              structured reference (QUALF + BELNR)
        E1IDT01              note to payee, TXT01..TXT14
        E1IDPU5              qualified amounts (MOAQUAL + MOABETR + CUXWAERZ)

``MOAQUAL`` takes SAP's own fixed values from domain ``EDIF5025``, *not* the
UN/EDIFACT 5025 code list: 019/021 are the opening and closing balance,
020/022 the same for an interim statement, 023/024 total debits and credits.

Two things are deliberately left unanswered rather than invented, and each is
returned as ``None`` so that a consumer cannot mistake a gap for a value:

- **Which side a line is on.** Nothing verified says how FINSTA01 marks a debit
  or a credit, so ``side`` is ``None``.
- **Which amount is a line's amount** when it carries several qualified ones.
  With exactly one, that is the amount; with more, ``amount`` is ``None`` and
  every qualified amount is in ``amounts``.

Nothing in SAP's dictionary settles the first: ``E1IDPF1-LINACTION`` (domain
``EDIF1229``) has no fixed values, and ``E1IDPU5-MOABETR`` (``EDIF5004``) is
18 characters of text.  A sign on the amount is kept as written - SAP puts a
minus after the number - but is not read as a side.

``E1IDLB1``/``E1IDLB2`` and everything under them are lockbox - message type
``LOCKBX`` on the same basic type - and are skipped.  A ``LOCKBX`` IDoc is
refused rather than read as a statement.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from .odata import SapError

OPENING, OPENING_INTERIM = "019", "020"
CLOSING, CLOSING_INTERIM = "021", "022"
TOTAL_DEBITS, TOTAL_CREDITS = "023", "024"
BALANCE_CODES = (OPENING, OPENING_INTERIM, CLOSING, CLOSING_INTERIM,
                 TOTAL_DEBITS, TOTAL_CREDITS)

LOCKBOX = ("E1IDLB1", "E1IDLB2")


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _fields(element) -> Dict[str, str]:
    """A segment's own fields - child elements with no children of their own."""
    return {_local(child.tag): (child.text or "").strip()
            for child in element if len(child) == 0}


def _children(element, name: str) -> list:
    return [child for child in element if _local(child.tag) == name]


def _amount(raw: str) -> Decimal:
    """MOABETR as SAP writes it: a decimal point, and a trailing minus if negative."""
    text = (raw or "").strip().replace(" ", "")
    negative = text.endswith("-")
    if negative:
        text = text[:-1]
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise SapError("The FINSTA01 amount %r is not a number" % raw, 400)
    return -value if negative else value


def _date(raw: str) -> Optional[str]:
    digits = "".join(ch for ch in raw or "" if ch.isdigit())[:8]
    if len(digits) != 8:
        return None
    return "%s-%s-%s" % (digits[:4], digits[4:6], digits[6:])


def _qualified_amounts(line) -> List[dict]:
    return [{"qualifier": fields.get("MOAQUAL", ""),
             "amount": _amount(fields.get("MOABETR", "")),
             "currency": fields.get("CUXWAERZ", "")}
            for fields in (_fields(seg) for seg in _children(line, "E1IDPU5"))]


def _control(root) -> Dict[str, str]:
    control = {}
    for element in root.iter():
        if _local(element.tag) == "EDI_DC40":
            control = _fields(element)
            break
    return control


def parse(body: bytes) -> dict:
    """A FINSTA01 as a statement, or raise SapError if it is not one."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        # A flat-file FINSTA01 needs every segment's fixed-width layout, which
        # this mock does not have; refuse it by name rather than misread it.
        raise SapError("A FINSTA01 statement must be sent as IDoc XML; "
                       "it is not well-formed XML: %s" % exc, 400)

    control = _control(root)
    idoctyp = (control.get("IDOCTYP") or _local(root.tag)).upper()
    mestyp = (control.get("MESTYP") or "FINSTA").upper()
    if idoctyp != "FINSTA01":
        raise SapError("This is a %s, not a FINSTA01 bank statement" % idoctyp, 400)
    if mestyp == "LOCKBX":
        raise SapError("FINSTA01 with message type LOCKBX is lockbox processing, "
                       "which this mock does not do; only FINSTA is read", 400)
    if mestyp != "FINSTA":
        raise SapError("FINSTA01 carries message type FINSTA or LOCKBX, not %s"
                       % mestyp, 400)

    headers = [e for e in root.iter() if _local(e.tag) == "E1IDKU1"]
    if len(headers) != 1:
        raise SapError("A FINSTA01 has exactly one E1IDKU1 header segment; "
                       "this one has %d" % len(headers), 400)
    header = headers[0]

    references = {}
    for seg in _children(header, "E1EDK02"):
        fields = _fields(seg)
        references.setdefault(fields.get("QUALF", ""), fields.get("BELNR", ""))
    dates = {}
    for seg in _children(header, "E1EDK03"):
        fields = _fields(seg)
        dates.setdefault(fields.get("IDDAT", ""), fields.get("DATUM", ""))
    banks = [_fields(seg) for seg in _children(header, "E1IDB02")]
    bank = banks[0] if banks else {}

    balances: Dict[str, Decimal] = {}
    lines = []
    for position, line in enumerate(_children(header, "E1IDPF1"), 1):
        amounts = _qualified_amounts(line)
        movements = [a for a in amounts if a["qualifier"] not in BALANCE_CODES]
        for amount in amounts:
            if amount["qualifier"] in BALANCE_CODES:
                balances.setdefault(amount["qualifier"], amount["amount"])
        if amounts and not movements:
            continue          # a line that only carries balances is not a movement

        fields = _fields(line)
        refs = [_fields(seg) for seg in _children(line, "E1EDP02")]
        texts = [_fields(seg) for seg in _children(line, "E1IDT01")]
        # A bank wraps the note at 70 characters wherever it falls, so an
        # invoice number can be split across two lines.  The joined note is
        # for reading; ``note_lines`` is what a search should also run over.
        note_lines = [text.get("TXT%02d" % n, "") for text in texts
                      for n in range(1, 15) if text.get("TXT%02d" % n)]
        single = movements[0] if len(movements) == 1 else None
        lines.append({
            "line": fields.get("LINLINEIT") or "%06d" % position,
            "reference": refs[0].get("BELNR", "") if refs else "",
            "qualifier": refs[0].get("QUALF", "") if refs else "",
            "note_to_payee": " ".join(" ".join(note_lines).split()),
            "note_lines": note_lines,
            "amount": single["amount"] if single else None,
            "currency": (single["currency"] if single else "")
                        or bank.get("FIIKWAER", ""),
            "amounts": movements,
            "side": None,     # not verified; see the module docstring
        })

    interim = OPENING not in balances and CLOSING not in balances and (
        OPENING_INTERIM in balances or CLOSING_INTERIM in balances)
    return {
        "account": {"bank": bank.get("FIIBKENN", ""),
                    "number": bank.get("FIIKONTO", ""),
                    "country": bank.get("FIIBLAND", ""),
                    "currency": bank.get("FIIKWAER", "")},
        "statement": _fields(header).get("BGMREF") or None,
        "references": references,
        "date": _date(dates.get("026", "")),
        "opening": balances.get(OPENING_INTERIM if interim else OPENING),
        "closing": balances.get(CLOSING_INTERIM if interim else CLOSING),
        "total_debits": balances.get(TOTAL_DEBITS),
        "total_credits": balances.get(TOTAL_CREDITS),
        "interim": interim,
        "lines": lines,
    }


def balances_add_up(statement: dict) -> Optional[str]:
    """Whether the statement agrees with itself, as a sentence if it does not.

    ``opening + total credits - total debits`` must equal ``closing``.  This is
    a check of one statement on its own - a corrupt or partial file - and is a
    different failure from a statement that does not follow the previous one.
    A statement that does not carry all four figures cannot be checked, and
    is not reported as failing.
    """
    figures = [statement.get(k) for k in
               ("opening", "total_credits", "total_debits", "closing")]
    if any(figure is None for figure in figures):
        return None
    opening, credits, debits, closing = figures
    expected = opening + credits - debits
    if expected == closing:
        return None
    return ("Statement %s does not add up: opening %s plus credits %s less "
            "debits %s is %s, but its closing balance is %s"
            % (statement.get("statement") or "(unnumbered)", opening, credits,
               debits, expected, closing))
