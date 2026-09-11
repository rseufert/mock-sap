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
| `schema.py` | The declarative heart. `Prop`, `Nav`, `EntityType` and `Service` definitions for the S/4HANA entity types the mock serves. Tables, `$metadata`, payload shapes and key handling are all derived from here. | Adding or changing an entity set, property or navigation. |
| `db.py` | SQLite: DDL generated from the schema, number-range objects (`next_number`), the deterministic seed data, and the `request_log`, `idoc` and `rfc_log` tables. | Changing demo data, or adding a non-entity table. |
| `odata.py` | The OData V2 vocabulary: the `$filter` tokenizer and recursive-descent parser that compiles to parameterised SQL, key-predicate parsing and rendering, EDM ↔ JSON value conversion, the response envelopes, and the SAP error payload. | Adding a `$filter` function, a new EDM type, or changing how values are rendered. |
| `metadata.py` | Generates the EDMX `$metadata` document (entity types, associations, referential constraints, `sap:` annotations, the entity container) and the Atom/JSON service documents. | Changing what `$metadata` advertises. |
| `store.py` | CRUD over SQLite: query, deep insert, update with MERGE-vs-PUT semantics, cascading delete, auto-assigned document numbers, item numbering, total recalculation, ABAP initial values. | Changing write behaviour or the system-filled fields. |
| `service.py` | The OData request dispatcher: URL segmentation, query-option handling, and the `Response`/`Context` types. Deliberately transport agnostic - `dispatch()` is called both by the HTTP server and by `$batch`. | Adding a URL shape or a query option. |
| `batch.py` | `$batch`: multipart/mixed parsing, nested changesets, response assembly, and changeset atomicity via a SQLite backup snapshot. | Changing batch semantics. |
| `bapi.py` | The RFC layer: the function-module registry, the BAPIRET2 helper, the eleven implemented BAPIs, and the SOAP transport (envelope parsing, response rendering, faults). | Adding a BAPI or changing RFC error shapes. |
| `idoc.py` | IDoc inbox and outbox: XML and EDI_DC40 flat-file parsing, status records, and ORDERS05 generation from a stored sales order. | Adding an IDoc type or segment. |
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
| `test_odata_write.py` | CSRF enforcement, deep insert with item numbering and total recalculation, PATCH, DELETE, validation failures, and POST to a navigation. |
| `test_batch.py` | A mixed batch of a GET and a changeset, and the rollback of a changeset whose second request fails. |
| `test_rfc.py` | BAPI create over JSON, the error `RETURN` table, unknown function modules, and the same functions over SOAP including a fault. |
| `test_idoc.py` | ORDERS05 generation, posting it back in, reading it, setting a status, and a flat-file IDoc. |
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
