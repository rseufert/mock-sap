# The CSDL schemas: where each one came from

These five files were written outside this project. They are the ground truth
`tools/check_csdl.py` holds the served `$metadata` to.

The reason they are here is #78. `mocksap/schema.py` drives this mock's writers
*and* its readers, and the assertions in `tests/` compare what the mock wrote
to what that same declaration says it should have written — so a reader and a
writer built from one declaration agree with each other even when both are
wrong about the format, and nothing derived from `schema.py` can notice. SAP's
OData payloads and its IDocs have no published corpus, so the trick mock-bank
uses — validate files other people wrote — is not available here. The CSDL
specification is the one genuine third party within reach, and `$metadata` is
the one document this mock serves that CSDL describes.

Every file is **unmodified**, byte-for-byte as published. `check_csdl.py`
verifies each SHA-256 below before it uses the file and refuses to run if one
has changed: editing a schema until a document passes is the single failure
this whole directory exists to make impossible.

They are not in the sdist. `MANIFEST.in` ships `tests/**.py`, and nothing in
`tests/` reads these — only `tools/`, which is not packaged either.

## OASIS: CSDL XML 4.01, for the V4 documents

| File | Source | SHA-256 |
| --- | --- | --- |
| `edmx.xsd` | [OData CSDL XML v4.01, OASIS Standard, 11 May 2020](https://docs.oasis-open.org/odata/odata-csdl-xml/v4.01/os/schemas/edmx.xsd) | `ceb670c2b45569a0387d6dadc8388c0fe61ca5583774e93fdd90f34fdd08ee25` |
| `edm.xsd` | [same release](https://docs.oasis-open.org/odata/odata-csdl-xml/v4.01/os/schemas/edm.xsd) | `c812c4409477d820bb35374dd9ca3cf38c86c441d3d38587e00c0aca6fc8ac58` |

An OASIS Standard, not a branch: the URLs are the frozen `os/` release, which
is why there is no commit to pin. The copyright notice and the TC's IPR
statement are inside each file, where OASIS put them, and `NOTICE-oasis-odata.txt`
beside them says so.

These validate the four V4 services' `$metadata` whole, **and** the V2
annotation document — `mocksap/annotations.py` writes a CSDL 4.01 envelope,
because that is what a V2 service publishes its UI intent in.

`edmx.xsd` imports the `edm` namespace without a `schemaLocation`, correctly,
which leaves `xmllint` nothing to resolve. `check_csdl.py` writes a two-line
schema of its own into a temporary directory to point at both; it declares
nothing, so it cannot loosen anything these two say.

## Microsoft: CSDL 2.0, for the V2 documents

| File | Source | SHA-256 |
| --- | --- | --- |
| `System.Data.Resources.CSDLSchema_2.xsd` | [dotnet/ef6](https://github.com/dotnet/ef6) @ `v6.5.2` (`a2d935973e540b69d73d1446f2ab217a89a797b7`), `src/EntityFramework/Resources/System/Data/EntityModel/` | `cfce3bc142edd4a512594583587721f232492d50e8ec094ac93b1359c368f014` |
| `System.Data.Resources.CodeGenerationSchema.xsd` | same | `1defd67cae1b1c0147faf583c5c7be90eb755a98b104ba6f47f1971f46606af0` |
| `System.Data.Resources.AnnotationSchema.xsd` | same | `6080358091073d44949e49e8691b1de72adfe1ab4e63b850c17726e4dc5f9209` |

`CSDLSchema_2.xsd` is the schema for `http://schemas.microsoft.com/ado/2008/09/edm`,
which is the namespace an OData V2 service's `<Schema>` is in — SAP Gateway's
included. The other two are there because it imports them by exactly those
names from the same directory; without them the schema does not compile, and a
schema that does not compile is how a check becomes a no-op. The filenames are
kept as published for that reason, and for provenance.

Entity Framework 6 is **MIT**, copyright (c) .NET Foundation and Contributors;
the licence is beside them as `LICENSE-MIT-ef6.txt`, as it asks.

### What this pair does *not* cover, and why

Only the `<Schema>` subtree of a V2 document is validated against a published
schema. No published schema covers the envelope above it: Microsoft's own EDMX
1.0 schema binds `edmx:DataServices` to CSDL **1.0**, which is Entity
Framework's pairing of those two namespaces, not OData's. Rather than write an
XSD of this project's own and let it look third-party, `check_csdl.py` asserts
those three elements in Python, in `wrapper_problems`, and says there that this
one part is the project's word.

## The `sap:` annotations

Nothing is stripped, and nothing needs to be. Both schemas end their attribute
lists with `xs:anyAttribute namespace="##other" processContents="lax"`, and
CSDL 2.0's `TSchema` ends its content model with `xs:any namespace="##other"` —
which is where the `atom:link` SAP puts after the entity container goes. So
`sap:label`, `sap:creatable` and the rest are admitted by the schemas' own
rules and read past **unexamined**: nothing published describes
`http://www.sap.com/Protocols/SAPData`. This check covers CSDL. The vendor
extensions are outside it, and `tests/test_metadata.py` is still the only thing
that looks at them.
