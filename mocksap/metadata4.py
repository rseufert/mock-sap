"""CSDL 4.0 ($metadata) for the V4 services, in both XML and JSON.

V4 metadata is considerably simpler than V2's: navigation properties carry
their target type directly, so there are no Association or AssociationSet
elements, and the container binds navigations to entity sets instead.
"""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from .odata4 import edm_type
from .schema import COMPLEX_TYPES, ENTITY_TYPES, EntityType, Service, set_for_type

EDMX_NS = "http://docs.oasis-open.org/odata/ns/edmx"
EDM_NS = "http://docs.oasis-open.org/odata/ns/edm"

# An annotation whose term cannot be resolved is worse than no annotation, so
# the vocabularies used are referenced by their published URLs.
VOCABULARIES = [
    ("https://oasis-tcs.github.io/odata-vocabularies/vocabularies/Org.OData.Core.V1.xml",
     "Org.OData.Core.V1", "Core"),
    ("https://oasis-tcs.github.io/odata-vocabularies/vocabularies/"
     "Org.OData.Capabilities.V1.xml", "Org.OData.Capabilities.V1", "Capabilities"),
    ("https://sap.github.io/odata-vocabularies/vocabularies/Common.xml",
     "com.sap.vocabularies.Common.v1", "Common"),
    ("https://sap.github.io/odata-vocabularies/vocabularies/UI.xml",
     "com.sap.vocabularies.UI.v1", "UI"),
]


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
    for uri, namespace, alias in VOCABULARIES:
        out.append("<edmx:Reference%s>" % _a("Uri", uri))
        out.append("<edmx:Include%s%s/>" % (_a("Namespace", namespace), _a("Alias", alias)))
        out.append("</edmx:Reference>")
    out.append("<edmx:DataServices>")
    out.append('<Schema Namespace=%s xmlns="%s">' % (quoteattr(ns), EDM_NS))

    # RAP services declare the message type and hang a collection of it off
    # every entity type, annotated as Common.Messages
    out.append('<ComplexType Name="SAP__Message">')
    for prop, attrs in (("code", 'Type="Edm.String"'),
                        ("message", 'Type="Edm.String"'),
                        ("target", 'Type="Edm.String"'),
                        ("transition", 'Type="Edm.Boolean"'),
                        ("numericSeverity", 'Type="Edm.Byte"')):
        out.append('<Property Name="%s" %s/>' % (prop, attrs))
    out.append("</ComplexType>")

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
            label = p.label or p.name
            out.append("<Property%s><Annotation Term=\"Common.Label\"%s/></Property>"
                       % (_property_attrs(p, ns), _a("String", label)))
        out.append('<Property Name="SAP__Messages" Type="Collection(%s.SAP__Message)" '
                   'Nullable="false"/>' % ns)
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

    for set_name, type_name in svc.sets.items():
        et = ENTITY_TYPES[type_name]
        if et.ui is None:
            continue
        out.append(_ui_annotations(ns, _type_name(svc, et), et))
        out.append(_capability_annotations(ns, svc.name + "_Container", set_name, et.ui))

    out.append("</Schema></edmx:DataServices></edmx:Edmx>")
    return "".join(out)


def _data_field(path: str) -> str:
    return ('<Record Type="UI.DataField"><PropertyValue Property="Value"%s/></Record>'
            % _a("Path", path))


def _ui_annotations(ns: str, type_name: str, et: EntityType) -> str:
    ui = et.ui
    out = ["<Annotations%s>" % _a("Target", "%s.%s" % (ns, type_name))]

    header = ['<Record Type="UI.HeaderInfoType">',
              '<PropertyValue Property="TypeName"%s/>' % _a("String", ui.type_name or et.label),
              '<PropertyValue Property="TypeNamePlural"%s/>'
              % _a("String", ui.type_name_plural or (ui.type_name or et.label) + "s")]
    if ui.title:
        header.append('<PropertyValue Property="Title">%s</PropertyValue>'
                      % _data_field(ui.title))
    if ui.description:
        header.append('<PropertyValue Property="Description">%s</PropertyValue>'
                      % _data_field(ui.description))
    header.append("</Record>")
    out.append('<Annotation Term="UI.HeaderInfo">%s</Annotation>' % "".join(header))

    if ui.line_items:
        out.append('<Annotation Term="UI.LineItem"><Collection>%s</Collection></Annotation>'
                   % "".join(_data_field(path) for path in ui.line_items))
    if ui.selection_fields:
        out.append('<Annotation Term="UI.SelectionFields"><Collection>%s</Collection>'
                   "</Annotation>"
                   % "".join("<PropertyPath>%s</PropertyPath>" % escape(path)
                             for path in ui.selection_fields))
    if ui.identification:
        out.append('<Annotation Term="UI.Identification"><Collection>%s</Collection>'
                   "</Annotation>"
                   % "".join(_data_field(path) for path in ui.identification))
    out.append("</Annotations>")
    return "".join(out)


def _capability_annotations(ns: str, container: str, set_name: str, ui) -> str:
    def restriction(term: str, prop: str, allowed: bool) -> str:
        return ('<Annotation Term="Capabilities.%s"><Record>'
                '<PropertyValue Property="%s"%s/></Record></Annotation>'
                % (term, prop, _a("Bool", "true" if allowed else "false")))

    return ("<Annotations%s>%s%s%s</Annotations>" % (
        _a("Target", "%s.%s/%s" % (ns, container, set_name)),
        restriction("InsertRestrictions", "Insertable", ui.insertable),
        restriction("UpdateRestrictions", "Updatable", ui.updatable),
        restriction("DeleteRestrictions", "Deletable", ui.deletable)))


def metadata_json(svc: Service) -> dict:
    """The JSON representation of the same CSDL document."""
    ns = svc.namespace
    types = _types_of(svc)
    schema = {}

    # RAP services declare the message type and hang a collection of it off
    # every entity type
    schema["SAP__Message"] = {
        "$Kind": "ComplexType",
        "code": {"$Kind": "Property", "$Type": "Edm.String"},
        "message": {"$Kind": "Property", "$Type": "Edm.String"},
        "target": {"$Kind": "Property", "$Type": "Edm.String"},
        "transition": {"$Kind": "Property", "$Type": "Edm.Boolean"},
        "numericSeverity": {"$Kind": "Property", "$Type": "Edm.Byte"},
    }
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
            entry[p.name]["@Common.Label"] = p.label or p.name
        entry["SAP__Messages"] = {"$Kind": "Property", "$Collection": True,
                                  "$Type": "%s.SAP__Message" % ns, "$Nullable": False}
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

    for set_name, type_name in svc.sets.items():
        et = ENTITY_TYPES[type_name]
        if et.ui is None:
            continue
        schema[_type_name(svc, et)].update(_ui_annotations_json(et))
        container_entry = schema[svc.name + "_Container"][set_name]
        container_entry.update({
            "@Capabilities.InsertRestrictions": {"Insertable": et.ui.insertable},
            "@Capabilities.UpdateRestrictions": {"Updatable": et.ui.updatable},
            "@Capabilities.DeleteRestrictions": {"Deletable": et.ui.deletable},
        })

    return {
        "$Version": "4.0",
        "$EntityContainer": "%s.%s_Container" % (ns, svc.name),
        "$Reference": {
            uri: {"$Include": [{"$Namespace": namespace, "$Alias": alias}]}
            for uri, namespace, alias in VOCABULARIES
        },
        ns: schema,
    }


def _data_field_json(path: str) -> dict:
    return {"$Type": "UI.DataField", "Value": {"$Path": path}}


def _ui_annotations_json(et: EntityType) -> dict:
    ui = et.ui
    header = {
        "$Type": "UI.HeaderInfoType",
        "TypeName": ui.type_name or et.label,
        "TypeNamePlural": ui.type_name_plural or (ui.type_name or et.label) + "s",
    }
    if ui.title:
        header["Title"] = _data_field_json(ui.title)
    if ui.description:
        header["Description"] = _data_field_json(ui.description)

    out = {"@UI.HeaderInfo": header}
    if ui.line_items:
        out["@UI.LineItem"] = [_data_field_json(path) for path in ui.line_items]
    if ui.selection_fields:
        out["@UI.SelectionFields"] = [{"$PropertyPath": path}
                                      for path in ui.selection_fields]
    if ui.identification:
        out["@UI.Identification"] = [_data_field_json(path) for path in ui.identification]
    return out


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
