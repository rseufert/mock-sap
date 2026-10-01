"""System time, and a clock a test can pin and move.

Every date this mock computes - a posting date, a document date, the baseline
a payment term counts from, the timestamp on a log row - used to come from
``datetime.utcnow()`` at the point of use. That makes a run unrepeatable: the
same script produces different documents on different days, and an example
whose assertions depend on a date goes stale by itself overnight.

So there is one clock, every date is computed against it, and it can be pinned
with ``--clock`` and moved with ``POST /_mock/advance``.

Three things worth knowing:

**It holds an offset, not an instant.** ``now()`` is real time plus an offset,
so the clock keeps ticking between advances and two rows written a moment
apart still sort in the order they were written. A stored instant would
freeze, and a frozen clock makes every timestamp in a run identical - which is
indistinguishable, in a log, from a bug.

**It is naive UTC, and only UTC.** That is exactly what ``utcnow()`` returned
at every call site this replaced, so the substitution changes no timestamp the
mock has ever written. mock-bank takes ``--timezone`` because a bank has a
cutoff hour and a weekend, and neither means anything without a zone. SAP here
has a posting date and a due date; a zone would change the meaning of every
existing timestamp and buy nothing.

**It knows nothing about business days.** No cutoff, no weekend, no holidays,
no settlement rule. Those are a bank's questions and they live in mock-bank.
Nothing here is released when a date arrives either, so there are no advance
hooks: advancing this clock moves time and that is all it does.

One mock per process holds one clock. ``Mock`` installs its own on
construction, and `current()` hands it to the call sites that have no other
reason to know a clock exists - which is most of them. A test that needs a
pinned clock uses `install`, which hands back the previous one to restore.
"""
from __future__ import annotations

import datetime
import threading
from typing import Dict, Optional

# The furthest one advance may move, about ten years, and also how far ahead
# `to` may land. A mock asked to skip more than that has been asked by
# mistake, and the failure is worth naming: left unbounded, `to=9999-12-31`
# is accepted and then the next advance raises OverflowError, which reaches a
# client as a 500 with a traceback and tells it nothing.
MAX_ADVANCE_DAYS = 3650


class Invalid(ValueError):
    """A clock the mock could not keep, and why. Answered 400, or refused at startup."""


def _real_now() -> datetime.datetime:
    """Real time as naive UTC.

    Spelled this way rather than as `utcnow()`, which is deprecated from
    Python 3.12 and warns on every call. The value is identical - an aware UTC
    moment with its zone dropped - so nothing downstream can tell the
    difference, and no aware datetime escapes to a caller that would then
    serialise an unexpected `+00:00`.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def parse_start(text: str) -> datetime.datetime:
    """``YYYY-MM-DDTHH:MM`` (seconds optional, or a bare date) as a moment."""
    for shape in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, shape)
        except ValueError:
            continue
    raise Invalid("--clock %r is not a moment; write it as YYYY-MM-DDTHH:MM, "
                  "as in 2026-10-02T16:00" % text)


def parse_date(text, what: str = "to") -> datetime.date:
    """``YYYY-MM-DD`` as a date, or an `Invalid` saying what was wanted."""
    try:
        return datetime.datetime.strptime(str(text), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise Invalid("%s %r is not a date; write it as YYYY-MM-DD"
                      % (what, text)) from None


def whole_days(value) -> int:
    """`days` for an advance: a whole number from 0 to `MAX_ADVANCE_DAYS`.

    Everything else is refused by name rather than reaching `timedelta`, where
    `nan`, `inf` and `1e9` each come back as an OverflowError or a traceback
    instead of an answer. A silently rounded `0.5` is no better: it invites
    reasoning about a half day that the caller never thought through.
    """
    if isinstance(value, bool):
        raise Invalid("days is a whole number of days, not %r" % value)
    if isinstance(value, str):
        # A query string delivers everything as text, so `?days=0.5` arrives
        # here as "0.5". Read it as a number first and let the rules below
        # judge it, or the caller who wrote a fraction in the URL gets a
        # worse message than the one who put it in a JSON body.
        text = value.strip()
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                raise Invalid("days is a whole number of days, not %r"
                              % value) from None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise Invalid("days is a whole number of days, and %s is not a "
                          "number at all" % value)
        if value != int(value):
            raise Invalid(
                "days is a whole number of days, so %s is refused rather than "
                "rounded; use to=YYYY-MM-DD to land on a particular date"
                % value)
        value = int(value)
    if not isinstance(value, int):
        raise Invalid("days is a whole number of days, not %r" % (value,))
    if not 0 <= value <= MAX_ADVANCE_DAYS:
        raise Invalid(
            "days is a whole number from 0 to %d; %d is outside that. The "
            "clock does not go backwards, and %d days is further than a mock "
            "is ever asked to skip on purpose."
            % (MAX_ADVANCE_DAYS, value, MAX_ADVANCE_DAYS))
    return value


class Clock:
    """System time for one mock: pinned, readable, and movable forwards."""

    def __init__(self, start: str = ""):
        self.start = start or ""
        self._lock = threading.Lock()
        self.offset = datetime.timedelta(0)
        if self.start:
            self.offset = parse_start(self.start) - _real_now()

    # -- reading it -------------------------------------------------------

    def now(self) -> datetime.datetime:
        """The current moment, naive UTC - a drop-in for `utcnow()`."""
        return _real_now() + self.offset

    def today(self) -> datetime.date:
        return self.now().date()

    def stamp(self) -> str:
        """The moment as the control plane and the logs write it.

        One shape everywhere: ISO-8601 to the second with a `Z`. The logs
        previously disagreed - microseconds and no zone in `request_log` and
        `rfc_log`, truncated to the second and no zone on an IDoc, a `Z` only
        on `/_mock/health` - so a client reading two of them had to parse two
        shapes and guess at the zone of both.

        Seconds, not microseconds: the extra digits were never a tie-breaker
        for anything, because both logs are ordered by `id`.
        """
        return self.now().replace(microsecond=0).isoformat() + "Z"

    # -- moving it --------------------------------------------------------

    def advance(self, days=None, to=None) -> Dict[str, object]:
        """Move time forward, and say where it went.

        Takes either `days` - a whole number of calendar days - or `to`, a
        date to land on at 00:00. Not both, and not neither.

        The clock does not go backwards. A `to` the clock has reached but not
        passed is a move of no distance rather than a refusal, so a test that
        advances to a date that happens to be today is not punished for it; a
        date strictly behind today is refused, because every timestamp written
        since would then be in the future.
        """
        if (days is None) == (to is None):
            raise Invalid("advance takes either days=N or to=YYYY-MM-DD, "
                          "and needs one of them")
        with self._lock:
            before = self.now()
            if days is not None:
                shift = datetime.timedelta(days=whole_days(days))
            else:
                target_day = to if isinstance(to, datetime.date) else parse_date(to)
                if target_day < before.date():
                    raise Invalid(
                        "the clock does not go backwards: it is %s and %s is "
                        "behind that, so every timestamp written since would "
                        "be in the future"
                        % (before.isoformat(timespec="minutes"),
                           target_day.isoformat()))
                # Bounded like `days`, and for the same reason: an unbounded
                # `to` is accepted and then the next advance overflows, which
                # a client sees as a 500 rather than as a refusal.
                ahead = (target_day - before.date()).days
                if ahead > MAX_ADVANCE_DAYS:
                    raise Invalid(
                        "to=%s is %d days ahead, and an advance moves at most "
                        "%d; past that the clock reaches a date arithmetic on "
                        "it cannot represent."
                        % (target_day.isoformat(), ahead, MAX_ADVANCE_DAYS))
                target = datetime.datetime.combine(target_day, datetime.time(0, 0))
                shift = max(target - before, datetime.timedelta(0))
            self.offset += shift
            after = self.now()
        return {
            "from": before.replace(microsecond=0).isoformat() + "Z",
            "to": after.replace(microsecond=0).isoformat() + "Z",
            "calendarDays": (after.date() - before.date()).days,
            "counts": "calendar days",
        }

    def reset(self) -> None:
        """Back to the moment the mock started at, not back to real time.

        With ``--clock`` that is the pinned moment, because ``--clock`` is
        configuration and a reset is not meant to undo configuration: a suite
        that pins time and resets between tests would otherwise get the pinned
        moment for its first test and the wall clock for every test after it,
        which surfaces as one flaky test somewhere else entirely.
        """
        with self._lock:
            self.offset = (parse_start(self.start) - _real_now()
                           if self.start else datetime.timedelta(0))

    def snapshot(self) -> Dict[str, object]:
        """What `/_mock/state` reports about system time."""
        now = self.now()
        return {
            "now": now.replace(microsecond=0).isoformat() + "Z",
            "date": now.date().isoformat(),
            "pinned": self.start,
            "offsetSeconds": int(self.offset.total_seconds()),
        }


# The clock the call sites read. One mock per process holds one; `Mock`
# installs its own, and this is the real-time one until it does.
_current = Clock()


def current() -> Clock:
    return _current


def install(new: Clock) -> Clock:
    """Make `new` the clock the call sites read, and hand back the old one."""
    global _current
    previous, _current = _current, new
    return previous


def now() -> datetime.datetime:
    return _current.now()


def today() -> datetime.date:
    return _current.today()


def stamp() -> str:
    return _current.stamp()
