#!/usr/bin/env python3
"""Hold the served `$metadata` to a schema nobody in this project wrote.

Every other check on this mock's output is a check against this mock's own
declaration. `mocksap/schema.py` drives the writers *and* the readers, and the
assertions in `tests/` compare what the mock wrote to what that same
declaration says it should have written. A reader and a writer built from one
declaration agree with each other even when both are wrong about the format,
and nothing derived from `schema.py` can notice (#78).

SAP's OData payloads and its IDocs have no published corpus, so the trick
mock-bank uses - validate files other people wrote - is not available here.
The one genuine third party within reach is the **CSDL specification itself**,
and `$metadata` is the one document this mock serves that CSDL describes. This
validates every one of them against schemas published by OASIS and by
Microsoft, vendored unmodified under `tests/samples/external/csdl/`:

* the **V4 services** and the **V2 annotation documents** are whole-document
  validated against OASIS's `edmx.xsd` and `edm.xsd` from CSDL XML 4.01, the
  OASIS Standard of 11 May 2020 (the annotation document is CSDL 4.01 too: a
  V2 service publishes its UI intent in a V4 envelope of its own);
* the **V2 services** have their `<Schema>` subtree validated against
  Microsoft's CSDL 2.0 schema, which Entity Framework 6 ships under the MIT
  licence. Only the subtree, because no published XSD covers OData V2's
  envelope: Microsoft's own EDMX 1.0 schema binds `edmx:DataServices` to CSDL
  **1.0**, which is Entity Framework's pairing, not OData's. The three
  elements above `<Schema>` are therefore asserted here, in `wrapper_problems`,
  and that part is this project's word rather than a third party's. It is
  named here instead of dressed up as a schema.

Three decisions worth knowing before you read the code:

**The `sap:` annotations are not stripped, and they are not checked.** They
need neither. Both schemas end their attribute lists with
`xs:anyAttribute namespace="##other" processContents="lax"`, and CSDL 2.0's
`TSchema` ends its content model with `xs:any namespace="##other"`, which is
where the `atom:link` SAP puts after the entity container goes. So a foreign
namespace is admitted by the schemas' own rules and read past: `sap:label`,
`sap:creatable` and the rest pass, unexamined, because nothing published
describes `http://www.sap.com/Protocols/SAPData`. What this check covers is
CSDL; the vendor extensions are outside it.

**A bogus `Edm.` type name passes an XSD.** CSDL spells a type reference as a
loose string pattern, so `Type="Edm.Str"` validates happily, and so would
`Edm.DateTime` in a V4 document where that type does not exist. `type_problems`
closes that: it reads the primitive type names out of the *same* vendored
schemas - OASIS's `TPrimitiveType`, Microsoft's `EDMSimpleType` - and holds
every `Type=` in the document to the list for its version. That pass is pure
standard library, so it runs even where `xmllint` does not.

**A check that cannot fail is worse than no check.** `xmllint` reports a
schema whose own imports failed to resolve as a compile error, but the margin
is thin, and a validator that has quietly become a no-op would report every
document green for ever. So this mutates one document of each kind in ways the
spec forbids - a typo'd attribute, an element out of order, a version that is
not a version - and fails if the validator accepts any of them. The vendored
schemas are checked against their published SHA-256 before use, too: nothing
here may be edited into agreeing with us.

Limits, stated rather than left to be discovered:

* it needs `xmllint` (libxml2), which has no standard-library equivalent.
  Without it the validation is skipped and says so, and the type pass still
  runs. `--require` turns a skip into a failure, which is how CI runs it.
* the CSDL **JSON** a V4 service serves for `$metadata?$format=json` is not
  covered. OASIS publishes a JSON Schema for it, but validating JSON Schema
  needs a dependency this project will not take.
* `$metadata` is the only document CSDL describes. The entity payloads, the
  IDocs and the BAPI responses remain held to `schema.py` alone.

    python3 tools/check_csdl.py
    python3 tools/check_csdl.py --require
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mocksap import annotations as sap_annotations  # noqa: E402
from mocksap import metadata, metadata4  # noqa: E402
from mocksap.schema import SERVICES  # noqa: E402

SCHEMAS = os.path.join(ROOT, "tests", "samples", "external", "csdl")

# Every vendored schema with the SHA-256 it has where it was published. The
# same hashes are in SOURCES.md beside the files, with the URL each came from;
# these are here because this is where they are checked.
VENDORED = {
    "edmx.xsd":
        "ceb670c2b45569a0387d6dadc8388c0fe61ca5583774e93fdd90f34fdd08ee25",
    "edm.xsd":
        "c812c4409477d820bb35374dd9ca3cf38c86c441d3d38587e00c0aca6fc8ac58",
    "System.Data.Resources.CSDLSchema_2.xsd":
        "cfce3bc142edd4a512594583587721f232492d50e8ec094ac93b1359c368f014",
    "System.Data.Resources.CodeGenerationSchema.xsd":
        "1defd67cae1b1c0147faf583c5c7be90eb755a98b104ba6f47f1971f46606af0",
    "System.Data.Resources.AnnotationSchema.xsd":
        "6080358091073d44949e49e8691b1de72adfe1ab4e63b850c17726e4dc5f9209",
}

XS = "{http://www.w3.org/2001/XMLSchema}"
EDMX2 = "{http://schemas.microsoft.com/ado/2007/06/edmx}"
EDM2 = "{http://schemas.microsoft.com/ado/2008/09/edm}"
M2 = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"

# V2 and V4 are different schemas, different documents and different rules, so
# everything below takes one of these two names.
V2, V4 = "v2", "v4"

# the two kinds of document, by the end of the label `served_documents` gives it
METADATA, ANNOTATIONS = "$metadata", "annotations"


# --------------------------------------------------------------------------
# the vendored schemas
# --------------------------------------------------------------------------

def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def vendored_problems():
    """The schemas must be the published bytes, or this proves nothing."""
    problems = []
    for name, digest in sorted(VENDORED.items()):
        path = os.path.join(SCHEMAS, name)
        if not os.path.exists(path):
            problems.append("%s is missing from %s" % (name, SCHEMAS))
            continue
        found = _sha256(path)
        if found != digest:
            problems.append(
                "%s is not the file that was published: SHA-256 %s, expected %s. "
                "Restore it from the URL in SOURCES.md - editing a schema to "
                "make a document pass is the one thing this check exists to "
                "stop" % (name, found, digest))
    return problems


def _driver(tmp):
    """A schema that only says where the other two are.

    OASIS's `edmx.xsd` imports the `edm` namespace without a `schemaLocation`,
    which is correct of it and leaves `xmllint` with nothing to resolve. This
    is this project's file, deliberately: two imports, no declarations, so it
    cannot loosen anything the OASIS schemas say.
    """
    path = os.path.join(tmp, "v4-imports.xsd")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:import namespace="http://docs.oasis-open.org/odata/ns/edmx"'
            ' schemaLocation="%s"/>\n'
            '  <xs:import namespace="http://docs.oasis-open.org/odata/ns/edm"'
            ' schemaLocation="%s"/>\n'
            "</xs:schema>\n"
            % (os.path.join(SCHEMAS, "edmx.xsd").replace("\\", "/"),
               os.path.join(SCHEMAS, "edm.xsd").replace("\\", "/")))
    return path


def schemas_in(tmp):
    """The schema `xmllint` is pointed at, per kind of document.

    CSDL 2.0 declares `<Schema>` as a global element, so a V2 subtree validates
    against the published file directly, with no wrapper of ours in the way.
    """
    return {V2: os.path.join(SCHEMAS, "System.Data.Resources.CSDLSchema_2.xsd"),
            V4: _driver(tmp)}


# --------------------------------------------------------------------------
# what the mock serves
# --------------------------------------------------------------------------

def served_documents():
    """Every CSDL document this mock serves, in one list.

    `(label, kind, xml)`. The labels are what a failure is reported under, so
    they name the service and the document, not the file.
    """
    out = []
    for name, svc in SERVICES.items():
        if svc.version >= 4:
            out.append(("%s $metadata" % name, V4, metadata4.metadata_document(svc)))
        else:
            out.append(("%s $metadata" % name, V2, metadata.metadata_document(svc)))
        if sap_annotations.has_annotations(svc):
            # a V4 envelope, served by a V2 service
            out.append(("%s annotations" % name, V4,
                        sap_annotations.annotations_document(svc)))
    return out


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

for _prefix, _uri in (("", "http://schemas.microsoft.com/ado/2008/09/edm"),
                      ("m", M2[1:-1]), ("sap", "http://www.sap.com/Protocols/SAPData"),
                      ("atom", "http://www.w3.org/2005/Atom")):
    # only so that a V2 failure names `sap:label` rather than `ns3:label`
    ET.register_namespace(_prefix, _uri)


def to_validate(kind, xml):
    """The texts handed to `xmllint`: the whole V4 document, or each V2 schema."""
    if kind == V4:
        return [xml]
    services = ET.fromstring(xml).find(EDMX2 + "DataServices")
    if services is None:
        return []  # wrapper_problems says what is wrong with it
    return [ET.tostring(schema, encoding="unicode")
            for schema in services.findall(EDM2 + "Schema")]


def validate(label, kind, xml, tmp, schemas, xmllint):
    """Validate one document. Returns the problems, which is usually none."""
    problems = []
    stem = os.path.join(tmp, re.sub(r"[^\w.-]", "_", label))
    for index, body in enumerate(to_validate(kind, xml)):
        path = "%s.%d.xml" % (stem, index)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        run = subprocess.run([xmllint, "--noout", "--schema", schemas[kind], path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr = run.stderr.decode("utf-8", "replace")
        if "Skipping the import" in stderr:
            # the schema did not fully compile, so "validates" would mean nothing
            problems.append(
                "%s: the %s schema did not compile - an import went unresolved:\n%s"
                % (label, kind.upper(), _indent(stderr, path, label)))
        elif run.returncode != 0:
            problems.append("%s is not valid %s CSDL:\n%s"
                            % (label, kind.upper(), _indent(stderr, path, label)))
    return problems


def _indent(text, path, label):
    """xmllint's complaint, trimmed to be read.

    One wrong property repeats the same line once per entity type, and every
    line carries the path of a temporary file, which is noise either way.
    """
    where = re.compile(re.escape(path) + r"(:\d+)?:? ?")
    seen, lines = set(), []
    for line in text.splitlines():
        line = where.sub("", line).strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    shown = ["      " + line for line in lines[:4]]
    if len(lines) > 4:
        shown.append("      ... and %d more" % (len(lines) - 4))
    return "\n".join(shown)


# --------------------------------------------------------------------------
# the primitive type names, read out of the same schemas
# --------------------------------------------------------------------------

def _enumeration(filename, type_name):
    root = ET.parse(os.path.join(SCHEMAS, filename)).getroot()
    for simple in root.iter(XS + "simpleType"):
        if simple.get("name") == type_name:
            return {e.get("value") for e in simple.iter(XS + "enumeration")}
    raise SystemExit("%s declares no simpleType %s - the vendored schema "
                     "changed shape" % (filename, type_name))


def primitive_types(kind):
    """What the published schema for this version says a primitive is."""
    if kind == V4:
        return _enumeration("edm.xsd", "TPrimitiveType")
    # Microsoft's enumeration carries the bare names; CSDL spells them Edm.X
    return {"Edm." + name
            for name in _enumeration("System.Data.Resources.CSDLSchema_2.xsd",
                                     "EDMSimpleType")}


def type_problems(label, kind, xml, allowed):
    """Every `Type=` that claims to be an EDM primitive has to be one."""
    problems = []
    for element in ET.fromstring(xml).iter():
        declared = element.get("Type")
        if not declared:
            continue
        inner = re.match(r"Collection\((.*)\)$", declared)
        name = inner.group(1) if inner else declared
        if name.startswith("Edm.") and name not in allowed:
            problems.append(
                "%s: <%s Name=%r> is typed %s, which is not an EDM primitive in "
                "%s" % (label, element.tag.split("}")[-1], element.get("Name"),
                        declared, kind.upper()))
    return problems


# --------------------------------------------------------------------------
# the V2 envelope, which no published schema covers
# --------------------------------------------------------------------------

def wrapper_problems(label, xml):
    """The three elements above a V2 `<Schema>`, asserted by hand.

    From OData Version 2.0's metadata: an `edmx:Edmx` of version 1.0 holding
    one `edmx:DataServices`, which carries `m:DataServiceVersion` and holds
    the CSDL schemas. This is the only part of a V2 document that is checked
    against this project's reading of the spec rather than against a published
    schema, because no published schema pairs this EDMX namespace with CSDL
    2.0. Keep it to what the spec fixes.
    """
    problems = []
    root = ET.fromstring(xml)
    if root.tag != EDMX2 + "Edmx":
        return ["%s: the root element is %s, not edmx:Edmx" % (label, root.tag)]
    if root.get("Version") != "1.0":
        problems.append("%s: edmx:Edmx is version %r, and EDMX is at 1.0"
                        % (label, root.get("Version")))
    children = list(root)
    if [child.tag for child in children] != [EDMX2 + "DataServices"]:
        return problems + [
            "%s: edmx:Edmx holds %s, and should hold one edmx:DataServices"
            % (label, ", ".join(child.tag for child in children) or "nothing")]
    services = children[0]
    if services.get(M2 + "DataServiceVersion") != "2.0":
        problems.append(
            "%s: edmx:DataServices declares m:DataServiceVersion=%r, and these "
            "are V2 services" % (label, services.get(M2 + "DataServiceVersion")))
    strays = [child.tag for child in services if child.tag != EDM2 + "Schema"]
    if strays:
        problems.append("%s: edmx:DataServices holds %s besides its schemas"
                        % (label, ", ".join(strays)))
    elif not list(services):
        problems.append("%s: edmx:DataServices holds no Schema" % label)
    return problems


# --------------------------------------------------------------------------
# negative controls: the check has to be able to fail
# --------------------------------------------------------------------------

# (name, kind, which document, what to break, what has to notice). Each is
# applied to a document the mock really serves, and each has to be refused.
CONTROLS = [
    ("an unknown attribute", V2, METADATA,
     lambda s: s.replace(' Nullable="false"', ' Nullible="false"', 1), "schema"),
    ("Key after the first Property", V2, METADATA,
     lambda s: re.sub(r"(<Key>.*?</Key>)(<Property[^>]*/>)", r"\2\1", s, count=1,
                      flags=re.S), "schema"),
    ("an unknown element", V2, METADATA,
     lambda s: s.replace("</EntityType>",
                         '<Propertie Name="x" Type="Edm.String"/></EntityType>', 1),
     "schema"),
    ("a type that is not an EDM primitive", V2, METADATA,
     lambda s: s.replace('Type="Edm.String"', 'Type="Edm.Str"', 1), "types"),
    ("an EDMX version that is not a version", V2, METADATA,
     lambda s: s.replace('<edmx:Edmx Version="1.0"', '<edmx:Edmx Version="2.0"', 1),
     "wrapper"),
    ("a missing DataServiceVersion", V2, METADATA,
     lambda s: s.replace(' m:DataServiceVersion="2.0"', "", 1), "wrapper"),
    ("an unknown attribute", V4, METADATA,
     lambda s: s.replace(' Nullable="false"', ' Nullible="false"', 1), "schema"),
    ("an unknown element", V4, METADATA,
     lambda s: s.replace("</EntityType>", '<Propertie Name="x"/></EntityType>', 1),
     "schema"),
    ("a CSDL version that is not a version", V4, METADATA,
     lambda s: s.replace('Version="4.0"', 'Version="3.0"', 1), "schema"),
    ("a type that is not an EDM primitive", V4, METADATA,
     lambda s: s.replace('Type="Edm.String"', 'Type="Edm.Str"', 1), "types"),
    # the annotation document is CSDL too, and is written by a module of its own
    ("a misspelt Term", V4, ANNOTATIONS,
     lambda s: s.replace("<Annotation Term=", "<Annotation Trem=", 1), "schema"),
    ("a stray Annotations inside an Annotation", V4, ANNOTATIONS,
     lambda s: s.replace("<Annotation Term=", "<Annotations Term=", 1), "schema"),
]


def control_problems(documents, tmp, schemas, xmllint):
    """Every control has to be refused, by the check that claims to catch it."""
    problems = []
    for name, kind, which, break_it, caught_by in CONTROLS:
        label, _kind, xml = next(
            (doc for doc in documents
             if doc[1] == kind and doc[0].endswith(which)), (None,) * 3)
        if xml is None:
            problems.append("no %s %s document to try %r against"
                            % (kind.upper(), which, name))
            continue
        broken = break_it(xml)
        if broken == xml:
            problems.append(
                "the control for %r did not change %s, so it proves nothing - the "
                "document changed shape and the control has to follow"
                % (name, label))
            continue
        if caught_by == "types":
            found = type_problems(label, kind, broken, primitive_types(kind))
        elif caught_by == "wrapper":
            found = wrapper_problems(label, broken)
        elif xmllint is None:
            continue  # the validation is skipped, so its controls are too
        else:
            found = validate(label, kind, broken, tmp, schemas, xmllint)
        if not found:
            problems.append(
                "%s with %s was accepted: the %s check is not catching what it "
                "is for" % (label, name, caught_by))
    return problems


# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--require", action="store_true",
                        help="fail instead of skipping when xmllint is missing")
    args = parser.parse_args(argv)

    problems = vendored_problems()
    if problems:
        _report(problems)
        return 1

    xmllint = shutil.which("xmllint")
    if xmllint is None and args.require:
        print("xmllint is not on PATH, and --require was given. It comes with "
              "libxml2: `apt-get install libxml2-utils`, `brew install libxml2`.",
              file=sys.stderr)
        return 1

    documents = served_documents()
    allowed = {kind: primitive_types(kind) for kind in (V2, V4)}
    tmp = tempfile.mkdtemp(prefix="check-csdl-")
    try:
        schemas = schemas_in(tmp)
        for label, kind, xml in documents:
            if kind == V2:
                problems.extend(wrapper_problems(label, xml))
            problems.extend(type_problems(label, kind, xml, allowed[kind]))
            if xmllint is not None:
                problems.extend(validate(label, kind, xml, tmp, schemas, xmllint))
        problems.extend(control_problems(documents, tmp, schemas, xmllint))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        _report(problems)
        return 1

    counts = {kind: sum(1 for doc in documents if doc[1] == kind) for kind in (V2, V4)}
    if xmllint is None:
        print("%d V2 and %d V4 CSDL documents name only EDM primitives the "
              "published schemas declare, and the V2 envelopes are well formed.\n"
              "xmllint is not on PATH, so the documents were not validated "
              "against the schemas themselves. Install libxml2 for that, or run "
              "with --require to make this a failure."
              % (counts[V2], counts[V4]))
        return 0
    print("%d V2 and %d V4 CSDL documents validate against the schemas OASIS and "
          "Microsoft published, name only EDM primitives those schemas declare, "
          "and %d deliberately broken documents were refused."
          % (counts[V2], counts[V4], len(CONTROLS)))
    return 0


def _report(problems):
    print("the served $metadata does not match the published CSDL schemas:\n")
    for problem in problems:
        print("  - %s" % problem)
    print("\n%d problem(s)." % len(problems))


if __name__ == "__main__":
    sys.exit(main())
