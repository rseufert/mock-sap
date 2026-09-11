# Every file in this project

A guided index of the repository. If you are looking for *how the pieces fit
together* rather than *what each file is*, read [ARCHITECTURE.md](ARCHITECTURE.md)
first.

## Top level

| File | What it is |
| --- | --- |
| `README.md` | The user-facing documentation: quick start, the endpoint table, what the OData layer supports, how to simulate failures, how to extend the mock. |
| `LICENSE` | MIT, verbatim, so GitHub and `licensee` detect it. The SAP trademark disclaimer deliberately lives in the README instead - appending anything to the licence text breaks that detection. |
| `pyproject.toml` | Packaging metadata and the **single source of truth for the version**. Declares the `mock-sap` console script and, notably, zero dependencies. |
| `MANIFEST.in` | Adds the Dockerfile, examples and tests to the sdist; without it an sdist carries only the package itself. |
| `Dockerfile` | `python:3.12-slim`, `pip install .`, entrypoint bound to `0.0.0.0:8000`. Built and exercised by CI on every push. |
| `.gitignore` | Build output, virtualenvs, `*.db` files left behind by `--db`. |

## `mocksap/` - the package

Roughly in dependency order: `schema` sits at the bottom and depends on nothing,
`server` sits at the top and depends on everything.

| File | What it is | Edit it when |
| --- | --- | --- |
| `schema.py` | The declarative heart. `Prop`, `ComplexType`, `Nav`, `EntityType` and `Service` definitions for the entity types the mock serves - the S/4HANA `API_*` shapes and the classic `GWSAMPLE_BASIC` demo service. Tables, `$metadata`, payload shapes and key handling are all derived from here. | Adding or changing an entity set, property or navigation. |
| `db.py` | SQLite: DDL generated from the schema, number-range objects (`next_number`), the deterministic seed data, and the `request_log`, `idoc` and `rfc_log` tables. | Changing demo data, or adding a non-entity table. |
| `odata4.py` | The V4 shapes: `@odata.context`/`@odata.count`/`@odata.etag` annotations, ISO 8601 timestamps, decimals as numbers, the V4 error object and service document. Everything below the wire is shared with V2. |
| `apply.py` | The `$apply` pipeline: parsed into a plan of filter, grouping keys, aggregates, ordering and paging, and handed to SQLite as one `GROUP BY` statement. Refuses transformations it does not implement by name. |
| `delta.py` | Delta tokens: minting and reading them, finding a type's change timestamp, and reading back the deletions that would otherwise leave no trace. |
| `metadata4.py` | CSDL 4.0, in XML and in JSON. No Association elements - V4 navigation properties carry their target type, and the container binds them to entity sets. |
| `odata.py` | The OData V2 vocabulary: the `$filter` tokenizer and recursive-descent parser that compiles to parameterised SQL, key-predicate parsing and rendering, EDM ↔ JSON value conversion, the response envelopes, and the SAP error payload. | Adding a `$filter` function, a new EDM type, or changing how values are rendered. |
| `metadata.py` | Generates the EDMX `$metadata` document (entity types, associations, referential constraints, `sap:` annotations, the entity container) and the Atom/JSON service documents. | Changing what `$metadata` advertises. |
| `store.py` | CRUD over SQLite: query, deep insert, update with MERGE-vs-PUT semantics, cascading delete that records what it removed, auto-assigned document numbers, item numbering, total recalculation, ABAP initial values, and the monotonic change timestamp the ETag is derived from. | Changing write behaviour or the system-filled fields. |
| `service.py` | The OData request dispatcher, shared by both dialects: URL segmentation, query-option handling, `$links`/`$ref`, and the `Response`/`Context` types. Deliberately transport agnostic - `dispatch()` is called both by the HTTP server and by `$batch`. | Adding a URL shape or a query option. |
| `batch.py` | `$batch` in both dialects: multipart/mixed with changesets for V2, JSON with atomicity groups for V4, and the snapshot that makes either roll back as a unit. | Changing batch semantics. |
| `bapi.py` | The RFC layer: the function-module registry, the BAPIRET2 helper, the seventeen implemented function modules - including `BAPI_SALESORDER_CHANGE` with its X structures and `RFC_READ_TABLE` with its own guarded OPTIONS parser - and the SOAP transport (envelope parsing, response rendering, faults). | Adding a BAPI or changing RFC error shapes. |
| `idoc.py` | IDoc inbox and outbox: XML and EDI_DC40 flat-file parsing, status records, ORDERS05/INVOIC02/DELVRY07 generation from a stored sales order, and the posting of an inbound delivery against the order it references. | Adding an IDoc type or segment. |
| `messages.py` | The `sap-message` warnings: how they are rendered into the header, and the small set of rules that produce them from the data (a delivery date in the past, a blocked customer, a material flagged for deletion). |
| `oauth.py` | The mock authorization server: the token store, the grants (client credentials, password, SAML bearer, refresh), bearer validation and revocation. Opaque tokens held in memory - there is no cryptography here, by design. |
| `server.py` | The HTTP front end: routing, CSRF tokens, basic auth, `sap-client` validation, latency and fault injection, the request log, the `/_mock` control plane and the human-readable index page. | Adding an endpoint, a failure scenario or a cross-cutting header. |
| `__init__.py` | Re-exports `Config` and `make_server`, and derives `__version__` from the installed package metadata (falling back to `pyproject.toml` in a source checkout, where it reports `…+source`). | Rarely. |
| `__main__.py` | The `mock-sap` / `python -m mocksap` command line: argument parsing and the startup banner. | Adding a CLI flag. |

## `tests/` - one module per surface

Every test drives a real mock over real HTTP; nothing is stubbed. Run them all
with `python3 -m unittest discover -s tests -v`, or a single surface with
`python3 tests/test_batch.py`.

| File | Covers |
| --- | --- |
| `support.py` | The shared harness, not a test module: `MockServerCase` starts a server on an ephemeral port in a background thread, and provides `request`/`get`/`csrf_token` helpers. Subclasses set `config_kwargs` to change the server's configuration. |
| `test_metadata.py` | The service document, EDMX `$metadata`, and the service catalog. |
| `test_odata_read.py` | Response envelopes and value formats, `$filter` (including string functions), `$top`/`$skip`/`$orderby`/`$inlinecount`, `$select`/`$expand`, navigation, `$count`, `$value`, and the error envelope for a bad filter. |
| `test_odata_v4.py` | The V4 dialect: envelopes and annotations, value formats compared against the same row read through V2, nested `$expand` options, `$ref`, CSDL 4.0 in both encodings, JSON `$batch` with an atomicity group, and the refusal to mix dialects. |
| `test_apply.py` | Aggregation: totals checked against the rows they summarise, grouping on several properties, composition with `filter`/`orderby`/`top`/`skip`, counting groups rather than pages, and the refusals - unknown transformations, non-numeric aggregates, options that cannot be combined with `$apply`. |
| `test_delta.py` | The delta cycle in both dialects: a tracked read hands back a link, a later read returns only changes and removals, the link moves on, a filter survives in it, and types without a change timestamp refuse. |
| `test_odata_write.py` | CSRF enforcement, deep insert with item numbering and total recalculation, PATCH, DELETE, validation failures, and POST to a navigation. |
| `test_complex.py` | Structured properties: the nested wire shape, `Address/City` in `$filter`/`$orderby`/`$select`, nested writes and partial updates, the `ComplexType` in `$metadata`, and the GWSAMPLE_BASIC service itself. |
| `test_links.py` | `$links` reads for to-one and to-many associations, paging and counting over them, and the write paths - including the refusal to re-point a composition, which would rewrite a key. |
| `test_etag.py` | The read-modify-write cycle: ETags on entity and header, conditional reads, `If-Match` on update and delete, `If-Match: *`, a changeset rolled back by a failed precondition, and the strict mode that demands a validator. |
| `test_batch.py` | A mixed batch of a GET and a changeset, and the rollback of a changeset whose second request fails. |
| `test_rfc.py` | BAPI create over JSON, the error `RETURN` table, unknown function modules, and the same functions over SOAP including a fault. |
| `test_idoc.py` | ORDERS05 generation, posting it back in, reading it, setting a status, and a flat-file IDoc. |
| `test_oauth.py` | The token endpoint and its failure modes, bearer validation across OData and RFC, the principal a SAML token carries into `CreatedByUser`, expiry and refresh-token rotation, revocation, and OAuth beside basic auth. |
| `test_messages.py` | Warnings from the data and from injection: the header shape, several messages travelling together, the V4 `SAP__Messages` collection and its declaration in CSDL, and the line between a warning and a failure. |
| `test_operations.py` | Failure scenarios, fault rules and their `count`, `sap-client` rejection, the `/_mock` endpoints, and reset. |
| `test_auth.py` | Basic authentication, against a server started with `--auth` and CSRF disabled. |

## `examples/`

| File | What it is |
| --- | --- |
| `demo.sh` | A guided tour of every endpoint in curl - catalog, metadata, filters, expand, CSRF, deep insert, BAPI over JSON and SOAP, IDoc out and back in, fault injection, request log. CI runs it on every push so the documented commands cannot rot. |
| `client.py` | A dependency-free client showing the flow a real SAP OData client needs: fetch a CSRF token, keep the session cookie, read with query options, write a deep insert. |

## `tools/`

| File | What it is |
| --- | --- |
| `check_docs.py` | Guards this index against drift: fails if a tracked file is not documented here, if a row names a file that no longer exists, or if a module is missing from the README's layout block. It checks coverage, not prose. Run it with `python3 tools/check_docs.py`; CI runs it on every push. |

## `.github/workflows/`

| File | What it is |
| --- | --- |
| `ci.yml` | Five jobs: the suite on Python 3.8-3.13 (Linux) plus 3.12 on macOS and Windows; a docs job running `tools/check_docs.py`; a smoke job running `demo.sh` and `client.py` against a live mock; a package job that builds, `twine check`s, installs the wheel and asserts the version agrees with `pyproject.toml`; and a job that builds and runs the Docker image. |
| `publish.yml` | Releases to PyPI via Trusted Publishing (OIDC, no stored token). A published GitHub Release goes to PyPI; a manual run goes to TestPyPI. Both build from a green test run and refuse a tag that disagrees with the built package. |

## `docs/`

| File | What it is |
| --- | --- |
| `FILES.md` | This file. |
| `ARCHITECTURE.md` | How a request flows through the mock, why the layering is the way it is, and where to extend it. |
