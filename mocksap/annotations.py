"""The V2 annotation document.

A V2 service publishes its UI intent in a document of its own rather than in
`$metadata`, which carries `sap:` attributes instead. The terms are the same
vocabulary the V4 services use; only the envelope differs, so this renders
from the very same `EntityType.ui` declarations.

Where the document *lives* is the part SAP does not settle for you: a SAPUI5
application normally names the annotation URL in its manifest rather than
discovering it. This mock therefore serves the document at a predictable path
and links to it from the service document as a convenience - the link is the
mock's own, not a protocol SAP defines, and is documented as such.
"""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from .metadata4 import VOCABULARIES
from .schema import ENTITY_TYPES, EntityType, Service, set_for_type

EDMX_NS = "http://docs.oasis-open.org/odata/ns/edmx"
EDM_NS = "http://docs.oasis-open.org/odata/ns/edm"

# the link relation the service document advertises the annotations with
LINK_RELATION = "http://www.sap.com/Protocols/SAPData/annotations"


def _a(name, value):
    return " %s=%s" % (name, quoteattr(str(value)))


def _data_field(path: str) -> str:
    return ('<Record Type="UI.DataField"><PropertyValue Property="Value"%s/></Record>'
            % _a("Path", path))


def has_annotations(svc: Service) -> bool:
    """Only a V2 service that opts in publishes one.

    The entity types carry UI intent for the V4 services' sake, but SAP's own
    A2X integration APIs ship no UI annotations, and neither do these.
    """
    return (svc.version < 4 and svc.annotations
            and any(ENTITY_TYPES[name].ui is not None for name in svc.sets.values()))


def path_for(svc: Service) -> str:
    return svc.path + "/annotations"


def annotations_document(svc: Service) -> str:
    """The annotation document for one V2 service."""
    ns = svc.namespace
    out = ['<?xml version="1.0" encoding="utf-8"?>']
    out.append('<edmx:Edmx Version="4.0" xmlns:edmx="%s">' % EDMX_NS)

    # the vocabularies the terms come from, and the service they describe
    for uri, namespace, alias in VOCABULARIES:
        out.append("<edmx:Reference%s>" % _a("Uri", uri))
        out.append("<edmx:Include%s%s/>" % (_a("Namespace", namespace), _a("Alias", alias)))
        out.append("</edmx:Reference>")
    out.append("<edmx:Reference%s>" % _a("Uri", svc.path + "/$metadata"))
    out.append("<edmx:Include%s/>" % _a("Namespace", ns))
    out.append("</edmx:Reference>")

    out.append("<edmx:DataServices>")
    out.append('<Schema Namespace=%s xmlns="%s">'
               % (quoteattr(ns + ".Annotations"), EDM_NS))

    for set_name, type_name in svc.sets.items():
        et = ENTITY_TYPES[type_name]
        if et.ui is None:
            continue
        out.append(_annotations_for(ns, et, set_name))
    out.append("</Schema></edmx:DataServices></edmx:Edmx>")
    return "".join(out)


def _annotations_for(ns: str, et: EntityType, set_name: str) -> str:
    ui = et.ui
    out = ["<Annotations%s>" % _a("Target", "%s.%s" % (ns, et.edm_name))]

    header = ['<Record Type="UI.HeaderInfoType">',
              '<PropertyValue Property="TypeName"%s/>'
              % _a("String", ui.type_name or et.label),
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
    for group in ui.field_groups:
        out.append('<Annotation Term="UI.FieldGroup"%s>'
                   '<Record Type="UI.FieldGroupType">'
                   '<PropertyValue Property="Label"%s/>'
                   '<PropertyValue Property="Data"><Collection>%s</Collection>'
                   "</PropertyValue></Record></Annotation>"
                   % (_a("Qualifier", group.qualifier), _a("String", group.label),
                      "".join(_data_field(path) for path in group.fields)))
    if ui.facets:
        out.append('<Annotation Term="UI.Facets"><Collection>%s</Collection></Annotation>'
                   % "".join(
                       '<Record Type="UI.ReferenceFacet">'
                       '<PropertyValue Property="Label"%s/>'
                       '<PropertyValue Property="Target"%s/></Record>'
                       % (_a("String", facet.label), _a("AnnotationPath", facet.target))
                       for facet in ui.facets))

    out.append("</Annotations>")

    # a property label targets the property, in a block of its own: Annotations
    # elements sit at schema level and do not nest
    for prop in et.props:
        out.append('<Annotations%s><Annotation Term="Common.Label"%s/></Annotations>'
                   % (_a("Target", "%s.%s/%s" % (ns, et.edm_name, prop.name)),
                      _a("String", prop.label or prop.name)))
    return "".join(out)
