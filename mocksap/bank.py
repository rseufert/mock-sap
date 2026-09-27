"""Bank details: the account a supplier is paid into.

An IBAN carries its own check digits, and SAP refuses a wrong one when it is
entered rather than passing it to a bank that will reject the payment days
later.  That check is ISO 13616's, and it is arithmetic rather than a lookup:
move the first four characters to the end, turn letters into numbers, and the
whole thing modulo 97 must be 1.

Nothing here talks to a bank.  It validates what was typed and builds the
seeded accounts, so the mock's own data passes the same check a client's does.
"""
from __future__ import annotations

from .odata import SapError

# ISO 13616 allows up to 34; the shortest in use is 15 (Norway).
IBAN_MIN, IBAN_MAX = 15, 34


def _numeric(iban: str) -> str:
    """The IBAN rearranged and lettered-to-numbers, ready for mod 97."""
    rotated = iban[4:] + iban[:4]
    return "".join(
        str(ord(ch) - ord("A") + 10) if ch.isalpha() else ch for ch in rotated)


def is_valid_iban(value: str) -> bool:
    iban = (value or "").replace(" ", "").upper()
    if not (IBAN_MIN <= len(iban) <= IBAN_MAX):
        return False
    if not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    if not iban[4:].isalnum():
        return False
    return int(_numeric(iban)) % 97 == 1


def check_digits(country: str, bban: str) -> str:
    """The two digits that make this account number a valid IBAN."""
    trial = "%s00%s" % (country.upper(), bban.upper())
    return str(98 - int(_numeric(trial)) % 97).zfill(2)


def iban(country: str, bban: str) -> str:
    """Build a valid IBAN, so seeded accounts pass the check clients face."""
    return "%s%s%s" % (country.upper(), check_digits(country, bban), bban.upper())


# Countries whose banks are on IBAN. The United States is not one of them: a
# US supplier is paid on a routing number and an account number, and inventing
# a US IBAN would be a shape no bank would accept.
IBAN_COUNTRIES = frozenset((
    "AT", "BE", "CH", "CZ", "DE", "DK", "ES", "FI", "FR", "GB", "GR", "HU",
    "IE", "IT", "LU", "NL", "NO", "PL", "PT", "RO", "SE", "SK",
))


def uses_iban(country: str) -> bool:
    return (country or "").upper()[:2] in IBAN_COUNTRIES


def check_bank_details(row: dict) -> None:
    """Refuse an account SAP would refuse, the way SAP refuses it.

    A wrong IBAN is a typing mistake, and catching it here is the difference
    between a payment run that reports a bad account and a bank that returns
    the payment a week later.
    """
    value = (row.get("IBAN") or "").strip()
    if value and not is_valid_iban(value):
        raise SapError(
            "IBAN %s is not valid: the check digits do not match the account "
            "number" % value, 400, code="/IWBEP/CX_MGW_BUSI_EXCEPTION")
    swift = (row.get("SWIFTCode") or "").strip()
    if swift and len(swift) not in (8, 11):
        raise SapError(
            "SWIFT/BIC %s is not valid: a BIC is 8 or 11 characters" % swift,
            400, code="/IWBEP/CX_MGW_BUSI_EXCEPTION")
