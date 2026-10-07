"""Amounts, as decimals rather than as binary floats.

An amount in SAP is a packed decimal with a declared scale.  `CURR(16,3)` is
not a number that happens to have three places; it is a number that *has*
three places and no others.  A binary float cannot hold 0.475 at all, and
`round()` would not help if it could - it rounds a half to the nearest even
digit, where every tax authority, and SAP, round it up.  Tax at 19% on 2.50
is 0.4750 exactly, which is 0.48; in floats it is 0.47, and the error is
systematic rather than random, so the mock is a cent light every time.

So every figure this mock *makes* is made here, in `decimal.Decimal`, and
rounded with `ROUND_HALF_UP` at the scale the property declares.  Reading a
value in through `of` is lossless whatever it arrived as, a float included,
because `str(0.475)` is `'0.475'` - the shortest decimal that reads back as
that same float, which is the number the caller meant.  It is only arithmetic
in floats that loses anything, and there is none of that left on an amount.

`Edm.Decimal` columns are `TEXT` for the same reason - see `Prop.sql_type` -
because a `REAL` column has no scale to keep, so a declared one would not
survive being written down.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# The scale this mock assumes for a decimal that declares none, and the one
# `$metadata` advertises for it, so the two cannot disagree. CSDL's own
# default is 0; three is SAP's habit for an amount, and every property here
# declares its scale anyway - this is the answer for one that forgot.
DEFAULT_SCALE = 3

# The places a currency amount is rounded to before it is posted.  A real
# system reads this per currency from TCURX - JPY has none, TND has three -
# and this mock has no such table, so it assumes the two that almost every
# currency has, and says so here rather than implying it looked.
CURRENCY_SCALE = 2


def of(value, default: str = "0") -> Decimal:
    """Whatever arrived, as an exact decimal.

    A float goes through `str` deliberately: `str(0.475)` is `'0.475'`, the
    shortest decimal that reads back as the same float, and therefore the
    number the caller wrote rather than the binary approximation it became.
    """
    if value is None or value == "":
        value = default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip() or default)
    except InvalidOperation:
        raise ValueError("%r is not a number" % (value,))


def at(value, scale: int = CURRENCY_SCALE) -> Decimal:
    """`value` at `scale` places, halves rounded up as SAP rounds them.

    A value no decimal of that scale could hold is a `ValueError` rather than
    something stored approximately: `Decimal` says no to a quantity of 1e40
    where `float` said yes and lost it.
    """
    number = of(value)
    if not number.is_finite():
        raise ValueError("%r is not a finite number" % (value,))
    try:
        return number.quantize(Decimal(1).scaleb(-int(scale)),
                               rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError("%r is out of range for a decimal of %d places"
                         % (value, scale))


def text(value, scale: int = CURRENCY_SCALE) -> str:
    """`value` at `scale` places as a string: how it is stored, and served."""
    return str(at(value, scale))


def scale_of(prop) -> int:
    """The scale a property declares, or `DEFAULT_SCALE` when it declares none."""
    return DEFAULT_SCALE if prop.scale is None else prop.scale
