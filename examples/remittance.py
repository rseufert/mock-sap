"""remittance: tell a supplier what SAP paid them, as an X12 820.

`invoice_check.py` runs one way: the supplier's 810 becomes an inbound INVOIC
IDoc, because the supplier's document is the supplier's to send and the example
plays the EDI subsystem that converts it. This runs the other way. mock-sap
generates the payment advice itself - the facts are its own, a payment document
it posted - and this converts that `PEXR2002` into the 820 a supplier reads.

The supplier is mock-edi (https://github.com/rseufert/mock-edi), which reads an
820 as the payee and says what is wrong with its *business* beside the 997
rather than in it: a total that is not the sum of its parts, and an advice that
claims a settlement date the payment has not reached.

**X12 rather than EDIFACT**, deliberately. mock-edi reads both, but its timing
and reversal rules read the 820 only - which `REMADV` date is the value date
varies too much between guides to guess - and the timing rule is half the point
of sending this at all.

**The perspective flips here, not in the IDoc.** mock-sap writes money leaving
its own account the way a bank statement does, with the minus after the number
(`3570.00-`). An 820 says the same thing as an unsigned `BPR02` with `BPR03`
`C`: a credit on the supplier's account. The sign is read rather than assumed,
so an advice that is not money out is refused instead of being relabelled.

The tests are in test_remittance.py.
"""
import datetime
import json
import urllib.request
from decimal import Decimal
from xml.etree import ElementTree as ET

from invoice_check import Sap

# BPR01. `I` is remittance information only - no money moves on this document,
# the bank has already moved it. `C` would say the payment travels with the
# advice. Anything else makes the 820 a payment order, which instructs a bank
# to pay, and mock-edi refuses it by name: it is the payee, not the bank.
REMITTANCE_ONLY = "I"

# BPR04, mandatory. `NON` is non-payment data, which is what an advice is once
# the bank has already paid. Saying `ACH` here would claim this document is
# what moved the money.
NO_PAYMENT_HERE = "NON"


class NotMoneyOut(Exception):
    """An advice whose amounts are not money leaving us."""


def _text(element, path, default=""):
    found = element.find(path)
    return default if found is None or found.text is None else found.text.strip()


def _amount(raw):
    """`3570.00-` as a positive Decimal, or raise: the minus is the direction.

    SAP writes a negative with the minus after the number, and `statement.py`
    sets out why the sign is the whole of the direction - no `EDIF5025`
    qualifier says debit or credit. An advice is money out, so a figure without
    the minus is not something to relabel as a credit to the supplier.
    """
    figure = (raw or "").strip()
    if not figure.endswith("-"):
        raise NotMoneyOut(
            "%r is not money out: an advice says what left this account, and "
            "the minus after the number is what says so" % figure)
    return Decimal(figure[:-1])


def read_pexr2002(xml):
    """The advice as a dict: trace, payee, settles, currency, total, invoices.

    `total` is not added up here. It is what the IDoc said, so that a total
    disagreeing with its rows survives the conversion and the supplier's reader
    is the one that catches it - which is the only way to know the check is
    live.
    """
    root = ET.fromstring(xml)
    header = root.find("./IDOC/E1IDKU1")
    if header is None:
        raise ValueError("no E1IDKU1: this is not a PEXR2002 payment advice")
    amount = header.find("./E1IDPU5")
    invoices = []
    for item in header.findall("./E1IDPU1"):
        invoices.append({
            "reference": _text(item, "./E1EDP02/BELNR"),
            "amount": _amount(_text(item, "./E1IDPU5/MOABETR")),
        })
    return {
        "trace": _text(header, "./BGMREF"),
        "payee": _text(header, "./E1EDKA1/LIFNR"),
        "settles": _text(header, "./E1EDK03/DATUM"),
        "currency": _text(amount, "./CUXWAERZ", "EUR") if amount is not None else "EUR",
        "total": _amount(_text(amount, "./MOABETR")) if amount is not None else Decimal("0"),
        "invoices": invoices,
    }


def advice_820(advice, sender, receiver="MOCKEDI", control=1, total=None):
    """The advice as an X12 820 interchange.

    `total` overrides what goes in BPR02, which is only useful to a test that
    wants to see the supplier refuse a total disagreeing with its rows. The
    default is the figure the IDoc carried.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    amount = advice["total"] if total is None else Decimal(total)
    body = [
        "ST*820*0001",
        # BPR02 unsigned with BPR03 C: the credit is on the supplier's side of
        # the relationship, which is the same movement the IDoc wrote as money
        # leaving ours.
        # BPR05-BPR15 are the bank routing and account fields of a payment
        # order, which an advice does not carry, so they are empty - eleven of
        # them, which is what puts the settlement date in BPR16 where the
        # reader looks for it. Counted rather than eyeballed: at nine fillers
        # it lands in BPR13 and the date is silently never read.
        "BPR*%s*%s*C*%s%s%s" % (REMITTANCE_ONLY, amount, NO_PAYMENT_HERE,
                                "*" * 12, advice["settles"]),
        "TRN*1*%s" % advice["trace"],
        "CUR*PR*%s" % advice["currency"],
        # RMR lives inside an ENT loop. The reader scans for RMR flat, but the
        # document has to be structurally valid to be read at all.
        "ENT*1*PE*92*%s" % advice["payee"],
    ]
    # RMR01 `IV` is the seller's invoice number - the number the supplier
    # quotes, not ours. RMR03 is a payment action code and is left empty
    # rather than filled with one this example has no basis to choose.
    body += ["RMR*IV*%s**%s" % (row["reference"], row["amount"])
             for row in advice["invoices"]]
    body += ["SE*%d*0001" % (len(body) + 1)]
    envelope = [
        "ISA*00*%-10s*00*%-10s*ZZ*%-15s*ZZ*%-15s*%s*%s*U*00401*%09d*0*T*>" % (
            "", "", sender, receiver, now.strftime("%y%m%d"),
            now.strftime("%H%M"), control),
        "GS*RA*%s*%s*%s*%s*%d*X*004010" % (
            sender, receiver, now.strftime("%Y%m%d"), now.strftime("%H%M"),
            control)]
    trailer = ["GE*1*%d" % control, "IEA*1*%09d" % control]
    return "~\n".join(envelope + body + trailer) + "~\n"


def generate_advice(sap, clearing_document):
    """Ask SAP for the payment advice for one payment document."""
    return sap.request("POST", "/sap/bc/idoc/generate",
                       {"mestyp": "REMADV",
                        "ClearingAccountingDocument": clearing_document})


def send(edi_base, interchange):
    req = urllib.request.Request(edi_base + "/edi", method="POST",
                                 data=interchange.encode(),
                                 headers={"Content-Type": "application/edi-x12"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


def tell_the_supplier(sap, edi_base, clearing_document, sender,
                      control=1, total=None):
    """Generate the advice for a payment and send it on as an 820.

    Returns what SAP generated and what the supplier made of it, so a caller
    can see both halves rather than only the end.
    """
    generated = generate_advice(sap, clearing_document)
    advice = read_pexr2002(generated["xml"])
    interchange = advice_820(advice, sender, control=control, total=total)
    return {"generated": generated, "advice": advice,
            "interchange": interchange,
            "receipt": send(edi_base, interchange)}
