# Changelog

Every release of [mock-sap](https://pypi.org/project/mock-sap/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

### Added

- This changelog, and `CONTRIBUTING.md`: what the project values, where to add
  each kind of thing, what a pull request should carry, and how a release is cut.

## [0.7.0] - 2026-09-11

Aggregation, delta handling and the annotations a Fiori elements app reads.

### Added

- **`$apply` aggregations** on the V4 services ([#14]). `filter`, `groupby`,
  `aggregate`, `orderby`, `top` and `skip` compose with `/`; the methods are
  `sum`, `min`, `max`, `average`, `countdistinct` and `$count`. The pipeline
  compiles to a single `GROUP BY` statement. Result rows are not entities: they
  carry the grouping keys and the aggregates, with an `@odata.context` naming
  exactly those columns, and `$count=true` counts the groups rather than the page.
- **Delta handling** in both dialects ([#15]). A read with
  `Prefer: odata.track-changes` hands back `@odata.deltaLink` (V4) or `__delta`
  (V2); coming back with the token returns what changed since, including removals.
  Deletions are recorded as they happen, since a deleted row leaves nothing behind.
  Types without a change timestamp refuse rather than returning everything.
- **UI vocabulary annotations** on the V4 services ([#17]): `UI.HeaderInfo`,
  `UI.LineItem`, `UI.SelectionFields`, `UI.Identification`, `Common.Label` on
  every property and `Capabilities.Insert/Update/DeleteRestrictions` on every
  entity set, in CSDL XML and JSON, with each vocabulary referenced by its
  published URL. The V2 services keep their `sap:` attributes.

### Fixed

- A delta read dropped every deletion made in the same second the token was
  issued: deletions were stamped to the second while tokens carry milliseconds.
  Nothing errored - changes and creations came back correctly and removals simply
  never appeared.

## [0.6.0] - 2026-09-11

More of the RFC layer, and IDocs that do something when they arrive.

### Added

- **Seven more function modules** ([#11]), seventeen in total:
  `BAPI_CUSTOMER_GETLIST`, `BAPI_CUSTOMER_GETDETAIL2`, `BAPI_VENDOR_GETDETAIL`,
  `BAPI_MATERIAL_GETLIST`, `BAPI_SALESORDER_CHANGE` and `RFC_READ_TABLE`.
- `BAPI_SALESORDER_CHANGE` honours the X structures: only fields flagged in
  `ORDER_HEADER_INX` / `ORDER_ITEM_INX` change, and a call that omits them changes
  nothing and says why. `UPDATEFLAG` `I`, `U` and `D` insert, update and delete items.
- `RFC_READ_TABLE` reads the same tables the OData services serve, with `FIELDS`,
  `OPTIONS`, `DELIMITER`, `ROWSKIPS` and `ROWCOUNT`. The table must resolve to a
  known entity type, every field name is checked, and every literal in `OPTIONS`
  is bound - nothing from the caller reaches SQL as text.
- **`INVOIC02` and `DELVRY07`** ([#12]). Generation is keyed by message type
  (`{"mestyp": "INVOIC", …}`), with `ORDERS` still the default, and each draws a
  billing or delivery number from its own range.
- An inbound `DELVRY07` **posts**: it compares delivered quantities against the
  order's items and moves `OverallDeliveryStatus`, reporting what it did.

## [0.5.0] - 2026-09-11

Every API in both dialects, and warnings that do not fail a request.

### Added

- **Product and purchase order in OData V4** ([#13]); all four A2X services now
  answer in both dialects over the same rows.
- **`sap-message` warnings** ([#16]) in the header, and `SAP__Messages` on the
  entity in V4 with the type declared in CSDL. Three rules produce warnings from
  the data - a delivery date in the past (which is rescheduled and reported), a
  blocked sold-to party, a material flagged for deletion - and any warning can be
  injected through `/_mock/faults`.

### Fixed

- Resolving which service describes an expanded entity could land on the other
  dialect once a type belonged to a V2 and a V4 service, putting V2 URLs and V2
  shapes inside a V4 response.

## [0.4.0] - 2026-09-11

Authentication as an S/4HANA Cloud client meets it.

### Added

- **OAuth 2.0 and SAML bearer** ([#5]): a token endpoint at
  `/sap/bc/sec/oauth2/token`, bearer validation across the whole surface, refresh
  with rotation and RFC 7009 revocation. Four grants - client credentials,
  password, SAML bearer, refresh - and the principal follows the token, so a
  document created with a SAML-derived token names that user in `CreatedByUser`.
  `--token-ttl` makes expiry testable in seconds; `/_mock/tokens` lists what is
  outstanding. None of it is cryptography, and the README says so.

### Fixed

- The CLI line-buffers stdout, so the banner and access log appear when piped or
  run in a container.
- The test harness closes each mock's database, quieting the `ResourceWarning`s
  the suite printed.

## [0.3.0] - 2026-09-11

Two dialects, one implementation.

### Added

- **OData V4** ([#1]) for the sales order and business partner APIs, at the long
  versioned `odata4` paths: `@odata.context` / `value` / `@odata.count` /
  `@odata.etag`, ISO timestamps, decimals as numbers, `$ref`, CSDL 4.0 in XML and
  JSON, JSON `$batch` with atomicity groups, and `$expand` with nested options.
  The dialects are kept apart: a V2 option on a V4 service, or the reverse, is an
  error naming the dialect it belongs to.
- **Complex (structured) types** ([#2]): nested on the wire with their own
  `__metadata.type`, flat underneath, and `Address/City` works in `$filter`,
  `$orderby` and `$select`.
- **`GWSAMPLE_BASIC`**, the classic Gateway demo service, at
  `/sap/opu/odata/IWBEP/GWSAMPLE_BASIC` - which brought services outside the
  `/sap/` prefix and entity sets named apart from their types.

## [0.2.0] - 2026-09-11

Association links and optimistic concurrency.

### Added

- **`$links`** ([#3]): reads as bare URIs for to-one and to-many associations,
  with paging and counting; writes re-point the foreign key, and refuse a change
  that would rewrite a key - which is every composition, exactly as SAP refuses it.
- **ETags and `If-Match`** ([#4]): a weak ETag derived from the properties a type
  marks `ConcurrencyMode="Fixed"`, in `__metadata.etag` and the `ETag` header.
  `If-Match` guards updates and deletes (412 when stale), `If-None-Match` answers
  304, and a failed precondition rolls a changeset back. `--require-if-match`
  refuses a modifying request without a validator, as newer Gateway services do.
- `docs/ARCHITECTURE.md` and `docs/FILES.md`, with a CI check that fails when a
  file is added and left undocumented.
- The test suite is split by surface.

### Changed

- `__version__` is derived from the installed package metadata, with
  `pyproject.toml` the single source of truth. CI fails a release whose tag,
  `pyproject.toml` and built wheel disagree.

## [0.1.0] - 2026-09-11

First release.

### Added

- **OData V2** in SAP Gateway style: four S/4HANA `API_*` services plus the
  service catalog, EDMX `$metadata` with `sap:` annotations, the
  `{"d":{"results":[…]}}` envelope, `__metadata` / `__deferred`, `/Date(ms)/`
  timestamps and `/IWBEP/CX_MGW_*` error envelopes.
- A `$filter` parser compiled to parameterised SQL, plus `$select`, `$expand`,
  `$orderby`, `$top`/`$skip`, `$inlinecount`, `$count` and `$value`.
- Writes: CSRF tokens, deep insert, `PATCH`/`MERGE`/`PUT`/`DELETE` with cascade,
  number-range document numbers, ABAP initial values, and `$batch` with changesets
  that roll back atomically.
- **BAPI/RFC** over JSON and SOAP with BAPIRET2 return tables, and **IDocs**
  inbound (XML and EDI_DC40 flat file) and outbound (ORDERS05).
- A control plane at `/_mock`: failure scenarios, fault rules, a request log and
  a reset endpoint.

[Unreleased]: https://github.com/rseufert/mock-sap/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/rseufert/mock-sap/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/rseufert/mock-sap/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/rseufert/mock-sap/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/rseufert/mock-sap/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/rseufert/mock-sap/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rseufert/mock-sap/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rseufert/mock-sap/releases/tag/v0.1.0
[#1]: https://github.com/rseufert/mock-sap/issues/1
[#2]: https://github.com/rseufert/mock-sap/issues/2
[#3]: https://github.com/rseufert/mock-sap/issues/3
[#4]: https://github.com/rseufert/mock-sap/issues/4
[#5]: https://github.com/rseufert/mock-sap/issues/5
[#11]: https://github.com/rseufert/mock-sap/issues/11
[#12]: https://github.com/rseufert/mock-sap/issues/12
[#13]: https://github.com/rseufert/mock-sap/issues/13
[#14]: https://github.com/rseufert/mock-sap/issues/14
[#15]: https://github.com/rseufert/mock-sap/issues/15
[#16]: https://github.com/rseufert/mock-sap/issues/16
[#17]: https://github.com/rseufert/mock-sap/issues/17
