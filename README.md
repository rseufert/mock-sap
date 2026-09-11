# mock-sap

[![CI](https://github.com/rseufert/mock-sap/actions/workflows/ci.yml/badge.svg)](https://github.com/rseufert/mock-sap/actions/workflows/ci.yml)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/mock-sap)](https://pypi.org/project/mock-sap/)

A **black-box mock SAP endpoint**. It does not implement any SAP business logic —
it speaks the shapes: SAP Gateway **OData V2** services, **BAPI/RFC** calls over JSON
and SOAP, and **IDocs** in XML and EDI_DC40 flat-file form. Data is kept in SQLite.

Point your integration at it and develop, test and demo without an SAP system,
an SAP licence, or a VPN tunnel to someone's sandbox.

- **Zero dependencies.** Python 3.8+ standard library and SQLite, nothing else.
- **Real wire shapes.** `{"d":{"results":[…]}}`, `__metadata`, `__deferred`,
  `/Date(1700000000000)/`, decimals as strings, EDMX `$metadata` with `sap:` annotations,
  CSRF tokens, `$batch` with atomic changesets, BAPIRET2 return tables, EDI_DC40 control records.
- **Deterministic demo data.** Business partners, products, sales orders, purchase orders —
  same data every run for the same `--seed`.
- **Failure modes on demand.** Latency, 500s, locked documents, "no work process available",
  expired CSRF tokens — per request, or as programmable rules.

MIT licensed. SAP, S/4HANA, ABAP, NetWeaver, BAPI and IDoc are trademarks of SAP SE;
this project is not affiliated with, endorsed by, or connected to SAP SE, and imitates
publicly documented wire formats for testing purposes only.

---

## Quick start

```bash
pip install mock-sap
mock-sap --port 8000
```

```bash
curl "http://127.0.0.1:8000/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder?\$top=1&\$format=json"
```

```json
{
  "d": {
    "results": [
      {
        "__metadata": {
          "id": "http://127.0.0.1:8000/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder('0000004712')",
          "uri": "http://127.0.0.1:8000/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder('0000004712')",
          "type": "API_SALES_ORDER_SRV.A_SalesOrderType"
        },
        "SalesOrder": "0000004712",
        "SalesOrderType": "OR",
        "SalesOrganization": "1710",
        "SoldToParty": "1000004",
        "TransactionCurrency": "EUR",
        "TotalNetAmount": "123991.820",
        "CreationDate": "/Date(1754611200000)/",
        "to_Item": { "__deferred": { "uri": ".../A_SalesOrder('0000004712')/to_Item" } }
      }
    ]
  }
}
```

Or run it straight from a checkout, with no install at all, or in a container:

```bash
python3 -m mocksap --port 8000
docker build -t mock-sap . && docker run -p 8000:8000 mock-sap
```

A guided tour of every endpoint, in curl:

```bash
bash examples/demo.sh
```

## What it serves

| Surface | Endpoint |
| --- | --- |
| Service catalog | `GET /sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection` |
| Business Partner | `/sap/opu/odata/sap/API_BUSINESS_PARTNER_SRV` |
| Product | `/sap/opu/odata/sap/API_PRODUCT_SRV` |
| Sales Order | `/sap/opu/odata/sap/API_SALES_ORDER_SRV` |
| Purchase Order | `/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV` |
| BAPI over JSON | `POST /sap/bc/rfc/<FUNCTION_MODULE>` |
| BAPI over SOAP | `POST /sap/bc/srt/rfc/sap/<service>/<client>/<name>/<binding>` |
| IDoc inbound | `POST /sap/bc/idoc` (XML or flat file) |
| IDoc outbound | `POST /sap/bc/idoc/generate` → ORDERS05 |
| Mock control plane | `/_mock/health`, `/_mock/state`, `/_mock/requests`, `/_mock/faults`, `/_mock/reset` |

Entity sets carry the S/4HANA field names — `A_SalesOrder` with `SoldToParty`,
`TotalNetAmount`, `OverallSDProcessStatus`, `to_Item`; `A_BusinessPartner` with
`BusinessPartnerCategory`, `to_BusinessPartnerAddress`; and so on. Browse
`/sap/opu/odata/sap/<SERVICE>/$metadata` for the full picture, or open
`http://127.0.0.1:8000/` for an index page.

## OData V2 support

| Feature | Notes |
| --- | --- |
| `$filter` | `eq ne gt ge lt le`, `and or not`, parentheses, arithmetic, `substringof`, `startswith`, `endswith`, `contains`, `tolower`, `toupper`, `trim`, `length`, `concat`, `substring`, `indexof`, `year`…`second`. Compiled to parameterised SQL. |
| `$select` `$expand` | `$expand` follows to-one and to-many navigations, nested paths included |
| `$orderby` `$top` `$skip` | |
| `$inlinecount=allpages`, `/$count` | |
| `$format=json`, Accept negotiation | XML/Atom for the service document and errors |
| `$metadata` | EDMX 1.0 with associations, referential constraints and `sap:label`/`sap:creatable`/`sap:updatable` |
| `$batch` | multipart/mixed, changesets execute atomically and roll back as a unit |
| Writes | `POST` (incl. deep insert), `PATCH`/`MERGE`, `PUT`, `DELETE`, `POST` to a navigation |
| Conventions | CSRF tokens, `sap-client`, `DataServiceVersion`, `Location` on create, SAP error envelope with `/IWBEP/CX_MGW_*` codes |

### Writing requires a CSRF token

Exactly as against a real Gateway:

```bash
TOKEN=$(curl -s -D - -o /dev/null -H 'X-CSRF-Token: Fetch' \
  http://127.0.0.1:8000/sap/opu/odata/sap/API_SALES_ORDER_SRV/ \
  | awk 'tolower($1)=="x-csrf-token:"{print $2}' | tr -d '\r')

curl -X POST "http://127.0.0.1:8000/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder?\$expand=to_Item" \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"SalesOrderType":"OR","SalesOrganization":"1710","DistributionChannel":"10",
       "OrganizationDivision":"00","SoldToParty":"1000001","TransactionCurrency":"EUR",
       "to_Item":[{"Material":"TG11","RequestedQuantity":"2","NetAmount":"1998.00"}]}'
```

The document number comes from a number range, items are numbered `000010`, `000020`, …,
`TotalNetAmount` is recalculated from the items, administrative fields are filled in,
and unset fields come back as ABAP initial values (`""`, `0.000`) rather than `null`.
Run with `--no-csrf` to switch the check off.

## BAPI / RFC

The same function modules are reachable over JSON and over SOAP. Parameter names are
matched case- and underscore-insensitively, so `ORDER_HEADER_IN` and `OrderHeaderIn`
both work.

```bash
curl -X POST http://127.0.0.1:8000/sap/bc/rfc/BAPI_SALESORDER_CREATEFROMDAT2 \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"ORDER_HEADER_IN":{"DOC_TYPE":"OR","SALES_ORG":"1710","DISTR_CHAN":"10","DIVISION":"00"},
       "ORDER_PARTNERS":[{"PARTN_ROLE":"AG","PARTN_NUMB":"0001000001"}],
       "ORDER_ITEMS_IN":[{"ITM_NUMBER":"000010","MATERIAL":"TG11","REQ_QTY":"3","COND_VALUE":"1500.00"}]}'
```

```json
{
  "SALESDOCUMENT": "0000004737",
  "RETURN": [{
    "TYPE": "S", "ID": "V1", "NUMBER": "311",
    "MESSAGE": "Standard Order 4737 has been saved",
    "MESSAGE_V1": "Standard Order", "MESSAGE_V2": "4737",
    "LOG_NO": "", "LOG_MSG_NO": "000000", "PARAMETER": "", "ROW": 0, "FIELD": "",
    "SYSTEM": "MCKCLNT100"
  }]
}
```

Errors arrive the way BAPIs report them — `TYPE: "E"` in the `RETURN` table with a
message number, not an HTTP error. Unknown material, unknown customer, missing
sold-to party and test runs (`TESTRUN: "X"`) are all modelled.

Available: `BAPI_SALESORDER_CREATEFROMDAT2`, `BAPI_SALESORDER_GETLIST`,
`BAPI_SALESORDER_GETSTATUS`, `BAPI_PO_CREATE1`, `BAPI_PO_GETDETAIL1`,
`BAPI_MATERIAL_GET_DETAIL`, `BAPI_BUSINESS_PARTNER_GETDETAIL`,
`BAPI_TRANSACTION_COMMIT`, `BAPI_TRANSACTION_ROLLBACK`, `RFC_PING`, `STFC_CONNECTION`.
`GET /_mock/services` lists them; `POST /sap/bc/rfc/` with no name does too.

SOAP uses the `urn:sap-com:document:sap:soap:functions:mc-style` namespace, returns
`<…Response>` envelopes, and answers unknown functions with a SOAP fault.

## IDoc

```bash
# outbound: render a stored sales order as ORDERS05
curl -X POST "http://127.0.0.1:8000/sap/bc/idoc/generate?format=xml" \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"SalesOrder":"0000004712"}'

# inbound: post an IDoc and get its status record back
curl -X POST http://127.0.0.1:8000/sap/bc/idoc \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/xml' -H 'Accept: application/json' \
  --data-binary @order.xml
```

Generated IDocs carry a full `EDI_DC40` control record plus `E1EDK01`, `E1EDK14`,
`E1EDK03`, `E1EDKA1`, `E1EDP01`/`E1EDP19` segments. Inbound IDocs are parsed (XML, or a
flat file read at the documented EDI_DC40 offsets), stored with a 16-digit IDoc number
and status `53`, and can be inspected or re-statused:

```bash
curl http://127.0.0.1:8000/_mock/idocs
curl -X PUT http://127.0.0.1:8000/sap/bc/idoc/<DOCNUM>/status \
  -H "X-CSRF-Token: $TOKEN" -d '{"status":"51"}'
```

## Simulating a bad day

Per request, with a header or a query parameter:

```bash
curl -H 'sap-mock-scenario: busy' http://127.0.0.1:8000/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder
```

| Scenario | Effect |
| --- | --- |
| `slow` | delays by `--slow-ms` (default 3s) |
| `timeout` | sleeps past any client timeout |
| `error` | 500, Gateway technical exception |
| `busy` | 503 with `Retry-After`, "no dialog work process available" |
| `auth` / `forbidden` | 401 with a NetWeaver realm / 403 missing authorization |
| `lock` | 423, document locked by another user |
| `csrf` | 403 with `x-csrf-token: Required` |
| `notfound` | 404 |

As rules, matched by regex on the path:

```bash
curl -X POST http://127.0.0.1:8000/_mock/faults -H 'Content-Type: application/json' \
  -d '{"match":"A_SalesOrder","method":"POST","status":500,"message":"Backend unreachable","count":1}'
curl -X DELETE http://127.0.0.1:8000/_mock/faults     # clear all rules
```

Or globally, at startup: `--latency-ms 250 --error-rate 0.05`.

## Inspecting what your client did

Every request is recorded, which makes the mock useful as a contract check in CI:

```bash
curl "http://127.0.0.1:8000/_mock/requests?limit=10"   # method, path, query, status, duration
curl  http://127.0.0.1:8000/_mock/rfc-log              # which BAPIs were called
curl  http://127.0.0.1:8000/_mock/state                # row counts per entity
curl -X POST http://127.0.0.1:8000/_mock/reset \
     -H 'Content-Type: application/json' -d '{"seed":7,"orders":50}'
```

`POST /_mock/reset` restores a known dataset between test cases.

## Command line

```
mock-sap [--host 127.0.0.1] [--port 8000] [--db :memory:|path.db] [--client 100]
         [--user MOCKUSER] [--auth USER:PASSWORD] [--no-csrf] [--seed 42]
         [--latency-ms 0] [--error-rate 0.0] [--slow-ms 3000] [--no-request-log] [-q]
```

`--db mock.db` keeps data across restarts; the default in-memory system starts fresh
every time. `--auth` turns on HTTP basic authentication with a NetWeaver realm.

## Using it from tests

```python
from mocksap import Config, make_server

httpd = make_server(Config(port=0, db_path=":memory:", csrf=False, quiet=True))
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
# point the code under test at http://127.0.0.1:{port} ...
httpd.shutdown()
```

`examples/client.py` is a dependency-free client showing the token/cookie flow.

## Adding entity sets

Everything is generated from the declarations in
[`mocksap/schema.py`](mocksap/schema.py) — tables, `$metadata`, payload shapes and
key handling. Add an `EntityType` with its `Prop`s and `Nav`s, list it in a `Service`,
and it is fully queryable and writable:

```python
_register(EntityType("A_BillingDocument", label="Billing Document", props=[
    S("BillingDocument", key=True, nullable=False, max_length=10),
    S("BillingDocumentType", max_length=4),
    DEC("TotalNetAmount", precision=16, scale=3),
    DT("BillingDocumentDate"),
], navs=[Nav("to_Item", "A_BillingDocumentItem", "*", [("BillingDocument", "BillingDocument")])]))
```

Seed data for it goes in `mocksap/db.py`, a BAPI wrapper (if you need one) in
`mocksap/bapi.py`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

31 tests, every one of them over real HTTP against a running mock: metadata,
query options, error envelopes, CSRF, deep insert, `$batch` rollback, BAPI JSON and
SOAP, IDoc round-trip, fault injection and authentication.

CI runs them on Python 3.8-3.13 across Linux, macOS and Windows, and additionally
checks that `examples/demo.sh`, the packaged wheel and the Docker image still work.

## Releasing

Releases go to PyPI through [Trusted Publishing](https://docs.pypi.org/trusted-publishers/):
PyPI trusts this repository's `publish.yml` workflow directly, so no API token is
stored in the repository or on anyone's laptop. Publishing a GitHub Release runs the
test suite, builds the sdist and wheel, checks that the tag matches the version in
`pyproject.toml`, and uploads. Running the workflow manually publishes to TestPyPI
instead, to rehearse.

```bash
# bump version in pyproject.toml and mocksap/__init__.py first
git tag v0.1.0 && git push origin v0.1.0
gh release create v0.1.0 --generate-notes
```

## Layout

```
mocksap/schema.py     entity types, navigations, services   (add shapes here)
mocksap/db.py         SQLite schema, number ranges, seed data
mocksap/odata.py      $filter parser, key predicates, JSON shaping, error envelope
mocksap/metadata.py   EDMX / service document
mocksap/store.py      CRUD, deep insert, cascades, document defaults
mocksap/service.py    OData request dispatcher
mocksap/batch.py      $batch multipart and atomic changesets
mocksap/bapi.py       BAPI/RFC functions, JSON and SOAP transports
mocksap/idoc.py       IDoc inbox/outbox, ORDERS05 generation
mocksap/server.py     HTTP front end, CSRF, auth, fault injection, /_mock API
```

## Scope

This is a black box. It stores what you send and returns it in SAP's shapes.
It does **not** do ATP checks, pricing procedures, output determination,
authorization objects, workflow or any other real SAP logic. What it is good for:
developing and testing integrations, contract tests in CI, demos, and load-testing
your side of the wire.

Not implemented yet, and the obvious next contributions: OData V4 services,
complex (structured) types, `$links`, ETags with `If-Match`, and OAuth/SAML instead
of basic authentication.
