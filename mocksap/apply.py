"""$apply: the aggregation transformations, on the V4 services.

An analytical client opens with an `$apply` and expects rows that are not
entities - a grouping key and some aggregates. That maps almost exactly onto
`GROUP BY`, which is what makes this tractable: the pipeline is parsed into a
plan and handed to SQLite, rather than being computed here.

A useful subset is implemented - `filter`, `groupby`, `aggregate`, `orderby`,
`top`, `skip` and their composition with `/` - and anything else is refused
by name rather than half-honoured.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .odata import SapError, build_where, to_json_value
from .schema import EntityType

SUPPORTED = ("filter", "groupby", "aggregate", "orderby", "top", "skip", "identity")

_AGGREGATES = {
    "sum": "SUM(%s)",
    "min": "MIN(%s)",
    "max": "MAX(%s)",
    "average": "AVG(%s)",
    "countdistinct": "COUNT(DISTINCT %s)",
}

_COUNTING = ("countdistinct",)

_TRANSFORMATION = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?$", re.S)
_AGGREGATE_ITEM = re.compile(
    r"^(?:(?P<count>\$count)|(?P<path>[A-Za-z_][A-Za-z0-9_/]*)\s+with\s+(?P<op>\w+))"
    r"\s+as\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)$", re.I | re.S)


class Aggregation:
    """The plan an $apply pipeline compiles to."""

    def __init__(self):
        self.where: str = ""
        self.params: List[Any] = []
        self.group: List[Tuple[str, str, Any]] = []   # alias, column, property
        self.aggregates: List[Tuple[str, str, Optional[Any], bool]] = []
        self.order: str = ""
        self.top: Optional[int] = None
        self.skip: Optional[int] = None

    @property
    def names(self) -> List[str]:
        return [alias for alias, _c, _p in self.group] + \
               [alias for alias, _s, _p, _i in self.aggregates]

    def select_sql(self) -> str:
        parts = ['"%s" AS "%s"' % (column, alias) for alias, column, _p in self.group]
        parts += ['%s AS "%s"' % (sql, alias) for alias, sql, _p, _i in self.aggregates]
        return ", ".join(parts) or "1"

    def statement(self, table: str) -> Tuple[str, List[Any]]:
        sql = 'SELECT %s FROM "%s"' % (self.select_sql(), table)
        if self.where:
            sql += " WHERE " + self.where
        if self.group:
            sql += " GROUP BY " + ", ".join('"%s"' % column for _a, column, _p in self.group)
        if self.order:
            sql += " ORDER BY " + self.order
        if self.top is not None:
            sql += " LIMIT %d" % self.top
            if self.skip:
                sql += " OFFSET %d" % self.skip
        elif self.skip:
            sql += " LIMIT -1 OFFSET %d" % self.skip
        return sql, list(self.params)

    def render(self, row) -> Dict[str, Any]:
        """One result row: grouping keys and aggregates, nothing else."""
        out: Dict[str, Any] = {}
        for alias, _column, prop in self.group:
            out[alias] = to_json_value(prop, row[alias])
        for alias, _sql, prop, integral in self.aggregates:
            value = row[alias]
            if value is None:
                out[alias] = None
            elif integral:
                out[alias] = int(value)
            elif prop is not None and prop.type == "Edm.Decimal":
                # an average of amounts is an amount: keep the scale SAP declares
                out[alias] = round(float(value), prop.scale if prop.scale is not None else 3)
            else:
                out[alias] = float(value)
        return out


def split_top_level(text: str, separator: str) -> List[str]:
    parts, buf, depth, in_str = [], "", 0, False
    for ch in text:
        if ch == "'":
            in_str = not in_str
        elif not in_str and ch == "(":
            depth += 1
        elif not in_str and ch == ")":
            depth -= 1
        if ch == separator and depth == 0 and not in_str:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def parse(expr: str, et: EntityType) -> Aggregation:
    """Compile an $apply pipeline into a plan, or explain why it cannot be."""
    plan = Aggregation()
    if not expr.strip():
        raise SapError("$apply is empty", 400)

    for stage in split_top_level(expr, "/"):
        match = _TRANSFORMATION.match(stage)
        if not match:
            raise SapError("Cannot read the transformation '%s'" % stage, 400)
        name, args = match.group(1).lower(), (match.group(2) or "").strip()
        if name not in SUPPORTED:
            raise SapError(
                "Transformation '%s' is not supported; this mock applies %s"
                % (match.group(1), ", ".join(SUPPORTED)), 400)

        if name == "identity":
            continue
        if name == "filter":
            if plan.group or plan.aggregates:
                raise SapError(
                    "filter must come before groupby or aggregate in this mock", 400)
            where, params = build_where(args, et)
            plan.where = "(%s) AND (%s)" % (plan.where, where) if plan.where else where
            plan.params.extend(params)
        elif name == "groupby":
            _groupby(plan, args, et)
        elif name == "aggregate":
            _aggregate(plan, args, et)
        elif name == "orderby":
            _orderby(plan, args)
        elif name in ("top", "skip"):
            try:
                value = int(args)
            except ValueError:
                raise SapError("%s(...) needs a number" % name, 400)
            if value < 0:
                raise SapError("%s(...) must not be negative" % name, 400)
            setattr(plan, name, value)

    if not plan.group and not plan.aggregates:
        raise SapError(
            "$apply must end in a groupby or an aggregate for this mock to answer", 400)
    return plan


def _groupby(plan: Aggregation, args: str, et: EntityType) -> None:
    parts = split_top_level(args, ",")
    if not parts or not parts[0].startswith("("):
        raise SapError(
            "groupby needs its properties in parentheses, as in groupby((Field))", 400)
    properties = split_top_level(parts[0][1:-1], ",")
    if not properties:
        raise SapError("groupby needs at least one property", 400)
    for path in properties:
        resolved = et.resolve(path)
        if resolved is None:
            raise SapError(
                "Property '%s' cannot be grouped on in type '%s'" % (path, et.name),
                400, target=path)
        column, prop = resolved
        plan.group.append((path.replace("/", "_"), column, prop))

    for nested in parts[1:]:
        match = _TRANSFORMATION.match(nested)
        if not match or match.group(1).lower() != "aggregate":
            raise SapError(
                "groupby takes an aggregate(...) beside its properties, not '%s'"
                % nested, 400)
        _aggregate(plan, (match.group(2) or "").strip(), et)


def _aggregate(plan: Aggregation, args: str, et: EntityType) -> None:
    for item in split_top_level(args, ","):
        match = _AGGREGATE_ITEM.match(item.strip())
        if not match:
            raise SapError(
                "Cannot read the aggregate '%s'; write it as "
                "'Property with sum as Alias' or '$count as Alias'" % item, 400)
        alias = match.group("alias")
        if match.group("count"):
            plan.aggregates.append((alias, "COUNT(*)", None, True))
            continue
        operator = match.group("op").lower()
        if operator not in _AGGREGATES:
            raise SapError(
                "Aggregation method '%s' is not supported; this mock has %s"
                % (match.group("op"), ", ".join(sorted(_AGGREGATES))), 400)
        path = match.group("path")
        resolved = et.resolve(path)
        if resolved is None:
            raise SapError(
                "Property '%s' cannot be aggregated in type '%s'" % (path, et.name),
                400, target=path)
        column, prop = resolved
        if operator not in _COUNTING and prop.type not in (
                "Edm.Decimal", "Edm.Double", "Edm.Int32", "Edm.Int16", "Edm.Int64"):
            raise SapError(
                "Property '%s' is not numeric, so it cannot be aggregated with %s"
                % (path, operator), 400, target=path)
        plan.aggregates.append((alias, _AGGREGATES[operator] % ('"%s"' % column),
                                prop, operator in _COUNTING))


def _orderby(plan: Aggregation, args: str) -> None:
    known = {alias for alias in plan.names}
    parts = []
    for chunk in split_top_level(args, ","):
        bits = chunk.split()
        alias = bits[0]
        if alias not in known:
            raise SapError(
                "orderby in $apply can only sort on what the pipeline produced: %s"
                % ", ".join(sorted(known)), 400, target=alias)
        direction = "ASC"
        if len(bits) > 1:
            if bits[1].lower() not in ("asc", "desc"):
                raise SapError("Invalid sort direction '%s'" % bits[1], 400)
            direction = bits[1].upper()
        parts.append('"%s" %s' % (alias, direction))
    plan.order = ", ".join(parts)


def run(conn, et: EntityType, plan: Aggregation) -> List[Dict[str, Any]]:
    sql, params = plan.statement(et.name)
    return [plan.render(row) for row in conn.execute(sql, params).fetchall()]
