"""Delta handling: what changed since the last read.

A replication client does not want the whole entity set every time. It reads
once, keeps the token it was handed, and comes back with it to be told what
has changed - including what has been deleted, which is the half that needs
help: a deleted row leaves nothing behind, so `store.record_deletion()` keeps
a note and this module reads it.

The token is a timestamp, written so that nobody is tempted to parse it.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .odata import SapError
from .schema import EntityType

_TOKEN = re.compile(r"^D(\d{8})T(\d{6})(\d{3})$")


def change_property(et: EntityType):
    """The timestamp a delta read compares against, if the type has one."""
    for prop in et.props:
        if prop.type == "Edm.DateTime" and prop.concurrency:
            return prop
    for name in ("LastChangeDate", "ChangedAt", "LastChangeDateTime"):
        prop = et.prop(name)
        if prop is not None and prop.type == "Edm.DateTime":
            return prop
    return None


def require_change_property(et: EntityType):
    prop = change_property(et)
    if prop is None:
        raise SapError(
            "Delta is not supported for '%s': it has no change timestamp to "
            "compare against" % et.name, 400)
    return prop


def mint(moment: Optional[_dt.datetime] = None) -> str:
    moment = moment or _dt.datetime.utcnow()
    return "D%sT%s%03d" % (moment.strftime("%Y%m%d"), moment.strftime("%H%M%S"),
                           moment.microsecond // 1000)


def read(token: str) -> _dt.datetime:
    value = (token or "").strip().strip("'")
    match = _TOKEN.match(value)
    if not match:
        raise SapError(
            "The delta token '%s' is not one this system issued" % token, 400,
            code="/IWBEP/CX_MGW_BUSI_EXCEPTION")
    try:
        moment = _dt.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        raise SapError("The delta token '%s' is not a valid point in time" % token, 400)
    return moment + _dt.timedelta(milliseconds=int(match.group(3)))


def token_option(opts: Dict[str, str]) -> Optional[str]:
    """V4 spells it $deltatoken, SAP's V2 services !deltatoken."""
    for key in ("$deltatoken", "!deltatoken", "deltatoken"):
        if opts.get(key):
            return opts[key]
    return None


def wants_tracking(headers: Dict[str, str]) -> bool:
    return "odata.track-changes" in (headers.get("prefer") or "").lower()


def changed_since(et: EntityType, since: _dt.datetime) -> Tuple[str, List[Any]]:
    prop = require_change_property(et)
    return '"%s" > ?' % prop.name, [since.isoformat()]


def deletions_since(conn, et: EntityType, since: _dt.datetime) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT keys FROM deleted_entity WHERE entity_type = ? AND deleted_at >= ? "
        "ORDER BY id", (et.name, since.isoformat())).fetchall()
    out = []
    for row in rows:
        try:
            out.append(json.loads(row["keys"]))
        except ValueError:  # pragma: no cover - written by us, so always valid
            continue
    return out
