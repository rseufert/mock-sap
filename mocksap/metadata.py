"""EDMX ($metadata) and service document generation, SAP Gateway flavoured."""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from .schema import COMPLEX_TYPES, ENTITY_TYPES, EntityType, Service

EDMX_NS = "http://schemas.microsoft.com/ado/2007/06/edmx"
M_NS = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
EDM_NS = "http://schemas.microsoft.com/ado/2008/09/edm"
SAP_NS = "http://www.sap.com/Protocols/SAPData"
ATOM_NS = "http://www.w3.org/2005/Atom"


def _a(name, value):
    return " %s=%s" % (name, quoteattr(str(value)))


def _assoc_name(type_name: str, nav_name: str) -> str:
    return "assoc_%s_%s" % (type_name, nav_name)


def _property_attrs(p, namespace: str = "") -> str:
    """The attributes of one <Property>, entity or complex alike."""
    if p.complex_type:
        attrs = _a("Name", p.name) + _a(
            "Type", "%s.%s" % (namespace, p.complex_type) if namespace else p.complex_type)
    else:
        attrs = _a("Name", p.name) + _a("Type", p.type)
        if p.max_length and p.type == "Edm.String":
            attrs += _a("MaxLength", p.max_length)
        if p.type == "Edm.Decimal":
            attrs += _a("Precision", p.precision or 13) + _a("Scale", p.scale or 3)
    if not p.nullable:
        attrs += ' Nullable="false"'
    if p.concurrency:
        attrs += ' ConcurrencyMode="Fixed"'  # feeds the entity's ETag
    if not p.creatable:
        attrs += ' sap:creatable="false"'
    if not p.updatable:
        attrs += ' sap:updatable="false"'
    return attrs + _a("sap:label", p.label or p.name)


def metadata_document(svc: Service) -> str:
    """Render the $metadata EDMX document for one service."""
    ns = svc.namespace
    out = ['<?xml version="1.0" encoding="utf-8"?>']
    out.append(
        '<edmx:Edmx Version="1.0" xmlns:edmx="%s" xmlns:m="%s" xmlns:sap="%s">'
        % (EDMX_NS, M_NS, SAP_NS)
    )
    out.append('<edmx:DataServices m:DataServiceVersion="2.0">')
    out.append(
        '<Schema Namespace=%s xml:lang="en" sap:schema-version="1" xmlns="%s">'
        % (quoteattr(ns), EDM_NS)
    )

    types = [ENTITY_TYPES[t] for t in dict.fromkeys(svc.sets.values())]

    # --- complex types, declared before the entity types that use them
    used = []
    for et in types:
        for p in et.props:
            if p.complex_type and p.complex_type not in used:
                used.append(p.complex_type)
    for name in used:
        ct = COMPLEX_TYPES[name]
        out.append("<ComplexType Name=%s%s>"
                   % (quoteattr(ct.name), _a("sap:label", ct.label or ct.name)))
        for p in ct.props:
            out.append("<Property%s/>" % _property_attrs(p))
        out.append("</ComplexType>")

    # --- entity types
    for et in types:
        out.append("<EntityType Name=%s%s sap:content-version=\"1\">"
                   % (quoteattr(et.edm_name), _a("sap:label", et.label or et.name)))
        out.append("<Key>")
        for k in et.keys:
            out.append("<PropertyRef Name=%s/>" % quoteattr(k.name))
        out.append("</Key>")
        for p in et.props:
            out.append("<Property%s/>" % _property_attrs(p, ns))
        for nav in et.navs:
            assoc = _assoc_name(et.name, nav.name)
            out.append(
                "<NavigationProperty%s%s%s%s/>"
                % (
                    _a("Name", nav.name),
                    _a("Relationship", "%s.%s" % (ns, assoc)),
                    _a("FromRole", "FromRole_" + assoc),
                    _a("ToRole", "ToRole_" + assoc),
                )
            )
        out.append("</EntityType>")

    # --- associations
    assocs = []
    for et in types:
        for nav in et.navs:
            if nav.target not in svc.sets.values():
                continue
            assoc = _assoc_name(et.name, nav.name)
            assocs.append((et, nav, assoc))
            out.append('<Association Name=%s sap:content-version="1">' % quoteattr(assoc))
            out.append(
                '<End Type=%s Multiplicity="1" Role=%s/>'
                % (quoteattr("%s.%s" % (ns, et.edm_name)), quoteattr("FromRole_" + assoc))
            )
            out.append(
                "<End Type=%s Multiplicity=%s Role=%s/>"
                % (
                    quoteattr("%s.%s" % (ns, ENTITY_TYPES[nav.target].edm_name)),
                    quoteattr(nav.multiplicity if nav.multiplicity == "1" else "*"),
                    quoteattr("ToRole_" + assoc),
                )
            )
            out.append("<ReferentialConstraint>")
            out.append('<Principal Role=%s>' % quoteattr("FromRole_" + assoc))
            for local, _remote in nav.join:
                out.append("<PropertyRef Name=%s/>" % quoteattr(local))
            out.append("</Principal>")
            out.append('<Dependent Role=%s>' % quoteattr("ToRole_" + assoc))
            for _local, remote in nav.join:
                out.append("<PropertyRef Name=%s/>" % quoteattr(remote))
            out.append("</Dependent>")
            out.append("</ReferentialConstraint>")
            out.append("</Association>")

    # --- entity container
    out.append(
        '<EntityContainer Name=%s m:IsDefaultEntityContainer="true" '
        'sap:supported-formats="atom json xlsx">' % quoteattr(svc.name + "_Entities")
    )
    for set_name, type_name in svc.sets.items():
        et = ENTITY_TYPES[type_name]
        out.append(
            '<EntitySet%s%s sap:creatable="true" sap:updatable="true" '
            'sap:deletable="true" sap:pageable="true" sap:addressable="true" '
            'sap:content-version="1"/>'
            % (_a("Name", set_name), _a("EntityType", "%s.%s" % (ns, et.edm_name)))
        )
    for et, nav, assoc in assocs:
        from .schema import set_for_type

        out.append(
            "<AssociationSet%s%s sap:creatable=\"false\" sap:updatable=\"false\" "
            "sap:deletable=\"false\" sap:content-version=\"1\">"
            % (_a("Name", assoc + "Set"), _a("Association", "%s.%s" % (ns, assoc)))
        )
        out.append(
            "<End%s%s/>" % (_a("EntitySet", set_for_type(svc, et.name)),
                            _a("Role", "FromRole_" + assoc))
        )
        out.append(
            "<End%s%s/>" % (_a("EntitySet", set_for_type(svc, nav.target)),
                            _a("Role", "ToRole_" + assoc))
        )
        out.append("</AssociationSet>")
    out.append("</EntityContainer>")
    out.append(
        '<atom:link rel="self" href=%s xmlns:atom="%s"/>'
        % (quoteattr(svc.path + "/$metadata"), ATOM_NS)
    )
    out.append("</Schema></edmx:DataServices></edmx:Edmx>")
    return "".join(out)


def service_document_xml(svc: Service, base_url: str) -> str:
    out = ['<?xml version="1.0" encoding="utf-8"?>']
    out.append(
        '<app:service xml:base=%s xmlns:app="http://www.w3.org/2007/app" '
        'xmlns:atom="%s" xmlns:sap="%s">'
        % (quoteattr(base_url + svc.path + "/"), ATOM_NS, SAP_NS)
    )
    out.append('<app:workspace><atom:title type="text">Data</atom:title>')
    for set_name in svc.sets:
        out.append(
            '<app:collection href=%s sap:content-version="1">'
            '<atom:title type="text">%s</atom:title></app:collection>'
            % (quoteattr(set_name), escape(set_name))
        )
    out.append("</app:workspace></app:service>")
    return "".join(out)


def service_document_json(svc: Service, base_url: str) -> dict:
    return {
        "d": {
            "EntitySets": list(svc.sets.keys()),
        }
    }
