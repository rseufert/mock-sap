"""CSDL 4.0 ($metadata) for the V4 services, in both XML and JSON.

V4 metadata is considerably simpler than V2's: navigation properties carry
their target type directly, so there are no Association or AssociationSet
elements, and the container binds navigations to entity sets instead.
"""
from __future__ import annotations

from xml.sax.saxutils import quoteattr

from .odata4 import edm_type
from .schema import COMPLEX_TYPES, ENTITY_TYPES, EntityType, Service, set_for_type

EDMX_NS = "http://docs.oasis-open.org/odata/ns/edmx"
EDM_NS = "http://docs.oasis-open.org/odata/ns/edm"


def _a(name, value):
    return " %s=%s" % (name, quoteattr(str(value)))


def _types_of(svc: Service):
    return [ENTITY_TYPES[t] for t in dict.fromkeys(svc.sets.values())]


def _complex_names(types):
    used = []
    for et in types:
        for p in et.props:
            if p.complex_type and p.complex_type not in used:
                used.append(p.complex_type)
    return used


def _type_name(svc: Service, et: EntityType) -> str:
    """V4 services name the type after the entity set: SalesOrder -> SalesOrderType."""
    return set_for_type(svc, et.name) + "Type"


def _property_attrs(p, namespace: str) -> str:
    if p.complex_type:
        attrs = _a("Name", p.name) + _a("Type", "%s.%s" % (namespace, p.complex_type))
    else:
        attrs = _a("Name", p.name) + _a("Type", edm_type(p))
        if p.max_length and p.type == "Edm.String":
            attrs += _a("MaxLength", p.max_length)
        if p.type == "Edm.Decimal":
            attrs += _a("Precision", p.precision or 13) + _a("Scale", p.scale or 3)
    if not p.nullable:
        attrs += ' Nullable="false"'
    return attrs


def metadata_document(svc: Service) -> str:
    ns = svc.namespace
    types = _types_of(svc)
    out = ['<?xml version="1.0" encoding="utf-8"?>']
    out.append('<edmx:Edmx Version="4.0" xmlns:edmx="%s">' % EDMX_NS)
    out.append("<edmx:DataServices>")
    out.append('<Schema Namespace=%s xmlns="%s">' % (quoteattr(ns), EDM_NS))

    for name in _complex_names(types):
        ct = COMPLEX_TYPES[name]
        out.append("<ComplexType Name=%s>" % quoteattr(ct.name))
        for p in ct.props:
            out.append("<Property%s/>" % _property_attrs(p, ns))
        out.append("</ComplexType>")

    for et in types:
        out.append("<EntityType Name=%s>" % quoteattr(_type_name(svc, et)))
        out.append("<Key>")
        for k in et.keys:
            out.append("<PropertyRef Name=%s/>" % quoteattr(k.name))
        out.append("</Key>")
        for p in et.props:
            out.append("<Property%s/>" % _property_attrs(p, ns))
        for nav in et.navs:
            if nav.target not in svc.sets.values():
                continue
            target = ENTITY_TYPES[nav.target]
            type_name = "%s.%s" % (ns, _type_name(svc, target))
            if nav.multiplicity == "*":
                type_name = "Collection(%s)" % type_name
            out.append("<NavigationProperty%s%s/>"
                       % (_a("Name", nav.name), _a("Type", type_name)))
        out.append("</EntityType>")

    out.append("<EntityContainer Name=%s>" % quoteattr(svc.name + "_Container"))
    for set_name, type_name in svc.sets.items():
        et = ENTITY_TYPES[type_name]
        bindings = [nav for nav in et.navs if nav.target in svc.sets.values()]
        attrs = _a("Name", set_name) + _a("EntityType", "%s.%s" % (ns, _type_name(svc, et)))
        if not bindings:
            out.append("<EntitySet%s/>" % attrs)
            continue
        out.append("<EntitySet%s>" % attrs)
        for nav in bindings:
            out.append("<NavigationPropertyBinding%s%s/>"
                       % (_a("Path", nav.name), _a("Target", set_for_type(svc, nav.target))))
        out.append("</EntitySet>")
    out.append("</EntityContainer>")
    out.append("</Schema></edmx:DataServices></edmx:Edmx>")
    return "".join(out)


def metadata_json(svc: Service) -> dict:
    """The JSON representation of the same CSDL document."""
    ns = svc.namespace
    types = _types_of(svc)
    schema = {}

    for name in _complex_names(types):
        ct = COMPLEX_TYPES[name]
        entry = {"$Kind": "ComplexType"}
        for p in ct.props:
            entry[p.name] = _json_property(p, ns)
        schema[ct.name] = entry

    for et in types:
        entry = {"$Kind": "EntityType", "$Key": [k.name for k in et.keys]}
        for p in et.props:
            entry[p.name] = _json_property(p, ns)
        for nav in et.navs:
            if nav.target not in svc.sets.values():
                continue
            target = ENTITY_TYPES[nav.target]
            nav_entry = {"$Kind": "NavigationProperty",
                         "$Type": "%s.%s" % (ns, _type_name(svc, target))}
            if nav.multiplicity == "*":
                nav_entry["$Collection"] = True
            entry[nav.name] = nav_entry
        schema[_type_name(svc, et)] = entry

    container = {"$Kind": "EntityContainer"}
    for set_name, type_name in svc.sets.items():
        et = ENTITY_TYPES[type_name]
        entry = {"$Collection": True, "$Type": "%s.%s" % (ns, _type_name(svc, et))}
        bindings = {nav.name: set_for_type(svc, nav.target)
                    for nav in et.navs if nav.target in svc.sets.values()}
        if bindings:
            entry["$NavigationPropertyBinding"] = bindings
        container[set_name] = entry
    schema[svc.name + "_Container"] = container

    return {
        "$Version": "4.0",
        "$EntityContainer": "%s.%s_Container" % (ns, svc.name),
        ns: schema,
    }


def _json_property(p, ns: str) -> dict:
    entry = {"$Kind": "Property"}
    entry["$Type"] = "%s.%s" % (ns, p.complex_type) if p.complex_type else edm_type(p)
    if entry["$Type"] == "Edm.String" and not p.complex_type:
        if p.max_length:
            entry["$MaxLength"] = p.max_length
    if p.type == "Edm.Decimal":
        entry["$Precision"] = p.precision or 13
        entry["$Scale"] = p.scale or 3
    if not p.nullable:
        entry["$Nullable"] = False
    return entry
