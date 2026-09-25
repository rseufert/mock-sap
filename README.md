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

By [Rick Seufert](https://rickseufert.com). The [projects page](https://rickseufert.com/#projects)
has this mock, [mock-edi](https://github.com/rseufert/mock-edi) and the worked examples
that use them together.

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
| Outbound Delivery | `/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV` |
| Billing Document | `/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV` |
| Journal Entry | `/sap/opu/odata/sap/API_JOURNALENTRY_SRV` |
| GWSAMPLE_BASIC (the classic demo service) | `/sap/opu/odata/IWBEP/GWSAMPLE_BASIC` |
| Sales Order, **OData V4** | `/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001` |
| Business Partner, **OData V4** | `/sap/opu/odata4/sap/api_businesspartner/srvd_a2x/sap/api_businesspartner/0001` |
| Product, **OData V4** | `/sap/opu/odata4/sap/api_product/srvd_a2x/sap/api_product/0001` |
| Purchase Order, **OData V4** | `/sap/opu/odata4/sap/api_purchaseorder/srvd_a2x/sap/api_purchaseorder/0001` |
| BAPI over JSON | `POST /sap/bc/rfc/<FUNCTION_MODULE>` |
| BAPI over SOAP | `POST /sap/bc/srt/rfc/sap/<service>/<client>/<name>/<binding>` |
| IDoc inbound | `POST /sap/bc/idoc` (XML or flat file) |
| IDoc outbound | `POST /sap/bc/idoc/generate` → ORDERS05, INVOIC02 or DELVRY07 |
| OAuth token endpoint | `POST /sap/bc/sec/oauth2/token`, `POST /sap/bc/sec/oauth2/revoke` |
| Mock control plane | `/_mock/health`, `/_mock/state`, `/_mock/services`, `/_mock/requests`, `/_mock/rfc-log`, `/_mock/idocs`, `/_mock/tokens`, `/_mock/faults`, `POST /_mock/reset` |

The four `API_*` services carry the S/4HANA field names; `GWSAMPLE_BASIC` is the
classic Gateway demo service every SAP OData tutorial uses, with its structured
`CT_Address` and its entity sets named apart from their types (`BusinessPartnerSet`
holds a `BusinessPartner`).

Entity sets carry the S/4HANA field names — `A_SalesOrder` with `SoldToParty`,
`TotalNetAmount`, `OverallSDProcessStatus`, `to_Item`; `A_BusinessPartner` with
`BusinessPartnerCategory`, `to_BusinessPartnerAddress`; and so on. Browse
`/sap/opu/odata/sap/<SERVICE>/$metadata` for the full picture, or open
`http://127.0.0.1:8000/` for an index page.

## OData V4

Every A2X service is served in V4 as well, over the same rows: what you write
through the V2 sales order service you read back through the V4 one. The dialects are kept
honestly apart - a V2 option on a V4 service is an error, and the other way round.

| | V2 | V4 |
| --- | --- | --- |
| Collection | `{"d":{"results":[…]}}` | `{"@odata.context":"…","value":[…]}` |
| Entity | `{"d":{…}}` | the entity object itself |
| Timestamps | `/Date(1754611200000)/` | `2025-08-08T00:00:00Z` |
| Decimals | `"123991.820"` | `123991.82` |
| Count | `$inlinecount=allpages` → `__count` | `$count=true` → `@odata.count` |
| ETag | `__metadata.etag` | `@odata.etag` |
| Links | `__deferred`, `$links` | omitted, `$ref` |
| Expanded | `{"results":[…]}` | a bare array |
| Nested options | - | `$expand=to_Item($select=Material;$top=2;$count=true)` |
| Aggregation | - | `$apply=groupby((SoldToParty),aggregate($count as Orders))` |
| Delta | `__delta`, `!deltatoken='…'` | `@odata.deltaLink`, `$deltatoken=…` |
| Annotations | `sap:` attributes | UI and Capabilities vocabularies |
| Metadata | EDMX 1.0 | CSDL 4.0, XML or JSON |
| Batch | multipart/mixed | JSON, with `atomicityGroup` |
| Errors | `message: {lang, value}` | `message` as a string |

```bash
curl "http://127.0.0.1:8000/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001/SalesOrder?\$top=1&\$count=true"
```

### Annotations for Fiori elements

`$metadata` describes structure; a Fiori elements app needs to be told what to
draw. The V4 services carry the handful of vocabulary terms it reads:

| Term | What it decides |
| --- | --- |
| `UI.HeaderInfo` | the title and subtitle of an object page |
| `UI.LineItem` | the columns of a list report |
| `UI.SelectionFields` | the filter bar |
| `UI.Identification` | the fields on the object page |
| `UI.FieldGroup` | the sections that group those fields |
| `UI.Facets` | the object page's layout, including its items table |
| `Common.Label` | the label of every property |
| `Capabilities.Insert/Update/DeleteRestrictions` | which buttons appear |

Each vocabulary is referenced by its published URL, so the terms resolve rather
than dangling, and the same content is served in CSDL JSON for clients that ask
for it. What a list report would draw is visible directly:

```bash
curl "$V4/\$metadata?\$format=json" | jq '."com.sap.gateway.srvd_a2x.api_salesorder.v0001".SalesOrderType."@UI.LineItem"'
```

An object page gets its sections from `UI.FieldGroup` and its layout from
`UI.Facets` - including a facet pointing through a navigation property at the
item type's own line items, which is how the items table appears:

```
Facet("Items", "to_Item/@UI.LineItem")
```

The annotations live beside the entity types in `mocksap/schema.py`, so a column
added to a list report is a line in the same file that defines the property.

The V2 services keep their `sap:` attributes in `$metadata`, which is what the V2
smart controls read for labels and visibility. The layout terms a V2 list report
needs live in a document of their own, the way a V2 app expects:

```bash
curl "$BASE/sap/opu/odata/IWBEP/GWSAMPLE_BASIC/annotations"
```

`GWSAMPLE_BASIC` publishes one - the demo service those tutorials use - and the
service document points at it. The A2X APIs publish none, because SAP's own do
not: they are integration APIs, not UI services.

A caveat worth stating: a SAPUI5 application normally names its annotation URL in
its manifest rather than discovering it, so the link from the service document is
the mock's own convenience, not a protocol SAP defines.

### Aggregation

An analytical client opens with an `$apply`, and the V4 services answer one:

```bash
curl --get "$V4/SalesOrder" --data-urlencode \
  '$apply=filter(TotalNetAmount gt 50000)/groupby((SoldToParty),aggregate($count as Orders,TotalNetAmount with average as Avg))/orderby(Orders desc)/top(3)'
```

```json
{
  "@odata.context": ".../$metadata#SalesOrder(SoldToParty,Orders,Avg)",
  "value": [
    {"SoldToParty": "1000003", "Orders": 3, "Avg": 152921.147},
    {"SoldToParty": "1000018", "Orders": 2, "Avg": 123650.425}
  ]
}
```

`filter`, `groupby`, `aggregate`, `orderby`, `top` and `skip` compose with `/`;
the aggregation methods are `sum`, `min`, `max`, `average`, `countdistinct` and
`$count`. The rows are not entities - they carry the grouping keys and the
aggregates and nothing else. `$count=true` beside an `$apply` counts the groups,
not the page. Anything the mock does not implement is refused by name rather than
half-honoured.

## OData V2 support

| Feature | Notes |
| --- | --- |
| `$filter` | `eq ne gt ge lt le`, `and or not`, parentheses, arithmetic, `substringof`, `startswith`, `endswith`, `contains`, `tolower`, `toupper`, `trim`, `length`, `concat`, `substring`, `indexof`, `year`…`second`. Compiled to parameterised SQL. |
| `$select` `$expand` | `$expand` follows to-one and to-many navigations, nested paths included |
| `$orderby` `$top` `$skip` | |
| `$inlinecount=allpages`, `/$count` | |
| Complex types | structured properties such as `CT_Address` nest on the wire with their own `__metadata.type`, and `Address/City` works in `$filter`, `$orderby` and `$select` |
| ETags | concurrency-controlled types carry a weak ETag in `__metadata.etag` and the `ETag` header; `If-Match` guards updates and deletes (412 when stale, `*` matches anything), `If-None-Match` answers 304 |
| `$links` | reads the association as bare URIs, to-one and to-many, with `$top`/`$skip`/`$inlinecount`/`$count`; writes re-point the foreign key, and refuse a change that would rewrite a key |
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

Available: `BAPI_SALESORDER_CREATEFROMDAT2`, `BAPI_SALESORDER_CHANGE`,
`BAPI_SALESORDER_GETLIST`, `BAPI_SALESORDER_GETSTATUS`, `BAPI_PO_CREATE1`,
`BAPI_PO_GETDETAIL1`, `BAPI_CUSTOMER_GETLIST`, `BAPI_CUSTOMER_GETDETAIL2`,
`BAPI_VENDOR_GETDETAIL`, `BAPI_MATERIAL_GETLIST`, `BAPI_MATERIAL_GET_DETAIL`,
`BAPI_OUTB_DELIVERY_CREATE_SLS`, `BAPI_ACC_DOCUMENT_POST`,
`BAPI_BUSINESS_PARTNER_GETDETAIL`, `BAPI_TRANSACTION_COMMIT`,
`BAPI_TRANSACTION_ROLLBACK`, `RFC_READ_TABLE`, `RFC_PING`, `STFC_CONNECTION`.
`GET /_mock/services` lists them; `POST /sap/bc/rfc/` with no name does too.

`BAPI_SALESORDER_CHANGE` honours the X structures the way a real BAPI does: only
fields flagged in `ORDER_HEADER_INX` / `ORDER_ITEM_INX` are changed, and a call
that forgets them changes nothing and says so. `ORDER_ITEM_INX` takes `UPDATEFLAG`
`I`, `U` and `D` to insert, update and delete items.

`RFC_READ_TABLE` reads the same tables the OData services serve, with `FIELDS`,
`OPTIONS` as a WHERE clause, `DELIMITER`, `ROWSKIPS` and `ROWCOUNT`, and returns
the fixed-width `DATA` rows people expect. It answers only for tables it knows -
ask it for `VBAK` and it tells you the mock has `A_SalesOrder` instead - and every
field name and literal in `OPTIONS` is checked and bound, so nothing from the
caller reaches SQL as text.

SOAP uses the `urn:sap-com:document:sap:soap:functions:mc-style` namespace, returns
`<…Response>` envelopes, and answers unknown functions with a SOAP fault.

### A valid call that fails anyway

The errors above come from malformed input. The ones production throws do not: a
credit limit, a closed posting period, a material blocked for sales. They arrive
as **HTTP 200 with `TYPE: "E"` in `RETURN`**, which is how an integration that
checks the status code and commits comes to exist. Ask for one:

```bash
curl -X POST http://127.0.0.1:8000/_mock/bapi-behaviour \
  -H 'Content-Type: application/json' \
  -d '{"function":"BAPI_SALESORDER_CREATEFROMDAT2","type":"E","id":"V1","number":"849",
       "message":"Credit limit exceeded for customer 0000000001","count":1}'
```

| Type | Effect |
| --- | --- |
| `E` | Error - the handler never runs, so no document and no number drawn |
| `A` | Abort, the same way |
| `W` | The call does its work and carries a warning as well |
| `S` | The call does its work and carries an extra success message |

The status stays `200` and SOAP returns a `<…Response>` envelope, not a fault - a
business error is not a transport error. `W` and `S` are there because a client
that treats any non-empty `RETURN` as failure has its own bug, and this is how you
find out. `count` spends the rule, `GET` lists them, `DELETE` and
`POST /_mock/reset` clear them.

`RFC_PING`, `STFC_CONNECTION` and `RFC_READ_TABLE` are refused: they are not BAPIs
and have no `RETURN` table, so a real one reports trouble by raising an ABAP
exception. Use `sap-mock-scenario` for those.

## IDoc

```bash
# outbound: render a stored sales order as ORDERS05, INVOIC02 or DELVRY07
curl -X POST "http://127.0.0.1:8000/sap/bc/idoc/generate?format=xml" \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mestyp":"INVOIC","SalesOrder":"0000004712"}'

# inbound: post an IDoc and get its status record back
curl -X POST http://127.0.0.1:8000/sap/bc/idoc \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/xml' -H 'Accept: application/json' \
  --data-binary @order.xml
```

Generated IDocs carry a full `EDI_DC40` control record and the segment tree their
type calls for - `E1EDK01`/`E1EDK14`/`E1EDKA1`/`E1EDP01` for an order, `E1EDK02`
references and `E1EDS01` totals for an invoice, `E1EDL20`/`E1EDL24` for a delivery.
An invoice draws a billing document number and a delivery a delivery number, each
from its own number range.

An inbound `DELVRY07` is not merely filed: its items name the order they came from
in `VGBEL`/`VGPOS`, so posting one moves that order's `OverallDeliveryStatus` to
`C` or `B` depending on whether the quantities cover it, and the receipt says what
it did. If the delivery it announces is one the mock has never seen, the delivery
is created too.

### Accepted is not posted

Receiving an IDoc and posting it are two events, and SAP reports them separately:
the port answers, and then the application either posts the document or does not.
By default every inbound IDoc posts, status `53`. Ask for something else:

```bash
curl -X POST http://127.0.0.1:8000/_mock/idoc-posting \
  -H 'Content-Type: application/json' \
  -d '{"mestyp":"INVOIC","status":"51","message":"Posting period 08 2026 is not open","count":1}'
```

| Status | Meaning |
| --- | --- |
| `53` | Application document posted - the default |
| `51` | Application document not posted |
| `56` | IDoc with errors added |
| `68` | Error - no further processing |

`mestyp` and `idoctyp` are optional filters, `count` spends the rule after that
many IDocs, `GET` lists the rules and `DELETE` clears them, as does
`POST /_mock/reset`.

Two things about this are the point. **The HTTP status stays `201`**: the IDoc
*was* received, a docnum *was* issued, and the failure is in the status record
where a client has to go looking for it. And **a failed posting does nothing** -
a `DELVRY07` that ends in `51` leaves the order's delivery status exactly where it
was and creates no delivery, which is the entire difference between `53` and `51`.

That combination is the expensive one. A `503` is loud and retryable; an IDoc that
is accepted and never posts looks like success from the sending side, and nobody
finds out until someone asks why the invoice was never paid.

### The documents an order turns into

A sales order becomes a delivery, an invoice and the journal entry that invoice
posts - each carrying the reference back to what it came from, and each readable
over OData:

```bash
curl "$BASE/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV/A_OutbDeliveryHeader?\$expand=to_DeliveryDocumentItem&\$format=json"
```

They are created three ways, and a document created one way looks like a document
created another: `BAPI_OUTB_DELIVERY_CREATE_SLS` and `BAPI_ACC_DOCUMENT_POST`,
generating an `INVOIC02` or `DELVRY07`, or posting an inbound delivery. Every
number the mock hands out now addresses a document that exists - an invoice adds
up to its items, and its journal entry balances. Inbound IDocs are parsed (XML, or a
flat file read at the documented EDI_DC40 offsets), stored with a 16-digit IDoc number
and status `53`, and can be inspected or re-statused:

```bash
curl http://127.0.0.1:8000/_mock/idocs
curl -X PUT http://127.0.0.1:8000/sap/bc/idoc/<DOCNUM>/status \
  -H "X-CSRF-Token: $TOKEN" -d '{"status":"51"}'
```

## Authentication

By default the mock is open. Two mechanisms can be switched on, and with both on a
request passes if it satisfies either.

```bash
mock-sap --auth sapuser:secret                        # HTTP basic, NetWeaver realm
mock-sap --oauth SAP_CLIENT:s3cret --token-ttl 60     # OAuth 2.0 bearer tokens
```

With `--oauth`, the flow an S/4HANA Cloud client has to implement works end to end:

```bash
TOKEN=$(curl -s -u SAP_CLIENT:s3cret -X POST \
  http://127.0.0.1:8000/sap/bc/sec/oauth2/token \
  -d 'grant_type=client_credentials' | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder?\$top=1&\$format=json"
```

| Grant | Notes |
| --- | --- |
| `client_credentials` | client id and secret, in the body or as HTTP basic |
| `password` | the `username` becomes the principal |
| `urn:ietf:params:oauth:grant-type:saml2-bearer` | the assertion's `NameID` becomes the principal |
| `refresh_token` | rotates: the old refresh token is spent |

The principal follows the token: a document created with a SAML-derived token has
that user in `CreatedByUser`. `--token-ttl 60` makes tokens expire quickly so a
client's refresh path can actually be exercised, `POST /sap/bc/sec/oauth2/revoke`
ends one early, and `GET /_mock/tokens` shows what is outstanding.

`expires_in` means two different things in the two places it appears, deliberately.
In a token response it is the lifetime the token was *issued* with, as RFC 6749
defines it, so `--token-ttl 60` always answers `60` no matter how long the mock
took to reply. In `GET /_mock/tokens` it is the seconds that token has *left*,
counting down, which is what a listing of live tokens is for.

**None of this is cryptography.** Tokens are opaque strings the mock remembers in
memory, and a SAML assertion is read for its `NameID` and otherwise believed - no
signature is checked, no issuer is verified. It exists so a client can exercise
fetch, use, expire, refresh and retry, not to stand in for an authorization server.

## Delta: what changed since last time

A replication client reads once with `Prefer: odata.track-changes`, keeps the link
it is handed, and comes back with it later:

```bash
curl -H 'Prefer: odata.track-changes' "$V4/SalesOrder"      # → "@odata.deltaLink": "…?$deltatoken=D20260911T175753033"
curl "$V4/SalesOrder?\$deltatoken=D20260911T175753033"      # → only what changed, and what went
```

```json
{
  "@odata.context": ".../$metadata#SalesOrder/$delta",
  "value": [
    {"SalesOrder": "0000004712", "PurchaseOrderByCustomer": "PO-DELTA", "…": "…"},
    {"@id": ".../SalesOrder('0000004714')", "@removed": {"reason": "deleted"}}
  ],
  "@odata.deltaLink": ".../SalesOrder?$deltatoken=D20260911T175812004"
}
```

The V2 services do the same in their own spelling: `__delta` in the payload,
`!deltatoken='…'` on the way back, and deleted entries marked
`"__metadata": {"deleted": true, …}`.

Entity types with a change timestamp support this - sales orders, products,
business partners - and the ones without say so rather than quietly returning
everything. Deletions are remembered in a table of their own, because a deleted row
leaves nothing behind for a reader to find; `POST /_mock/reset` forgets them along
with the rest.

**A delta read is at-least-once, and so is SAP's.** Change timestamps carry
milliseconds and nothing finer, because that is all `/Date(ms)/` can express, so a
change made in the token's own millisecond cannot be told apart from the token's
instant. This mock reports it again rather than risk dropping it: the token is
taken before the rows are read, and the comparison is *at or after*, not after.
A client may therefore see a change it has already applied, which costs it an
idempotent write it has to be capable of anyway. The alternative costs it the row,
silently - it is told nothing changed, and nothing tells it otherwise.

## Warnings that do not fail the request

SAP answers are not binary: a request can succeed and still carry messages. The
mock puts them in the `sap-message` header, and on the V4 services in a
`SAP__Messages` collection on the entity, which is where SAPUI5's message popover
reads them from.

```http
HTTP/1.1 201 Created
sap-message: {"code":"V1/302","message":"Requested delivery date 2020-01-01 is in the
              past; it was moved to 2026-09-11","severity":"warning",
              "target":"RequestedDeliveryDate","numericSeverity":3,"details":[]}
```

Three rules produce warnings from the data itself, so a client can be tested
against messages that arise rather than only injected ones:

| Situation | Message |
| --- | --- |
| a requested delivery date in the past | the order is rescheduled to today and the move is reported |
| a sold-to party blocked centrally | the block is reported |
| an item whose material is flagged for deletion | the flag is reported |

Any warning can also be injected, which is what an integration test usually wants:

```bash
curl -X POST http://127.0.0.1:8000/_mock/faults -H 'Content-Type: application/json' \
  -d '{"match":"A_SalesOrder","method":"GET","message":"Credit limit exceeded","count":1}'
```

A rule with a `message` and no `status` warns without failing; a rule with a
`status` fails, and a failure belongs in the error body rather than a warning
header. The message classes and numbers are plausible rather than authentic - what
a client depends on is the shape, the severity and the target.

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
| `precondition` | 412, as when the entity was changed after it was read |
| `expired-token` | 401 `invalid_token`, as when a bearer token has run out |
| `csrf` | 403 with `x-csrf-token: Required` |
| `notfound` | 404 |

As rules, matched by regex on the path:

```bash
curl -X POST http://127.0.0.1:8000/_mock/faults -H 'Content-Type: application/json' \
  -d '{"match":"A_SalesOrder","method":"POST","status":500,"message":"Backend unreachable","count":1}'
curl -X DELETE http://127.0.0.1:8000/_mock/faults     # clear all rules
```

Or globally, at startup: `--latency-ms 250 --error-rate 0.05`.

Every one of these is a *transport* failure. For the application-level kind, where
the response is a success and the payload says otherwise, see
[A valid call that fails anyway](#a-valid-call-that-fails-anyway) for BAPIs and
[Accepted is not posted](#accepted-is-not-posted) for IDocs.

## Inspecting what your client did

Every request is recorded, which makes the mock useful as a contract check in CI:

```bash
curl "http://127.0.0.1:8000/_mock/requests?limit=10"   # method, path, query, status, duration
curl  http://127.0.0.1:8000/_mock/rfc-log              # which BAPIs were called
curl  http://127.0.0.1:8000/_mock/state                # row counts per entity
curl  http://127.0.0.1:8000/_mock/idoc-posting         # how inbound IDocs will post
curl  http://127.0.0.1:8000/_mock/bapi-behaviour       # what a BAPI will answer
curl -X POST http://127.0.0.1:8000/_mock/reset \
     -H 'Content-Type: application/json' -d '{"seed":7,"orders":50}'
```

`POST /_mock/reset` restores a known dataset between test cases.

## Command line

```
mock-sap [--host 127.0.0.1] [--port 8000] [--db :memory:|path.db] [--client 100]
         [--user MOCKUSER] [--auth USER:PASSWORD] [--no-csrf] [--seed 42]
         [--latency-ms 0] [--error-rate 0.0] [--slow-ms 3000] [--no-request-log]
         [-q] [--version]
```

`--db mock.db` keeps data across restarts; the default in-memory system starts fresh
every time. `--auth` turns on HTTP basic authentication with a NetWeaver realm, and
`--oauth` turns on bearer tokens.
`--require-if-match` makes the mock refuse to modify a concurrency-controlled entity
that arrives without a validator, the way newer Gateway services do.

## Using it from tests

```python
import threading

from mocksap import Config, make_server

httpd = make_server(Config(port=0, db_path=":memory:", csrf=False, quiet=True))
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
# point the code under test at http://127.0.0.1:{port} ...
httpd.shutdown()
```

`examples/client.py` is a dependency-free client showing the token/cookie flow.

Two fuller examples use this mock together with
[mock-edi](https://github.com/rseufert/mock-edi), a mock EDI trading partner:

- [`examples/invoice_check.py`](examples/invoice_check.py) checks a supplier's
  X12 invoices against the purchase order in `API_PURCHASEORDER_PROCESS_SRV` and
  the supplier's ship notice, and posts the ones that match here as `INVOIC`
  IDocs. [`examples/test_invoice_check.py`](examples/test_invoice_check.py) covers
  a clean invoice, a short shipment, a price disagreement and a duplicate invoice.
- mock-edi's [`examples/po_bridge.py`](https://github.com/rseufert/mock-edi/blob/main/examples/po_bridge.py)
  sends purchase orders from here to the supplier as 850s and posts the 855
  confirmations back as `ORDRSP` IDocs, and its tests use this mock's fault rules
  to take the IDoc endpoint down mid-run.

Both are walked through, test by test, in
[Testing an SAP-to-EDI Integration Without SAP or a Trading Partner](https://rickseufert.com/blog/2026/09/24/testing-an-sap-to-edi-integration).

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
python3 -m unittest discover -s tests -v   # everything
python3 tests/test_batch.py                # one surface
```

196 tests, every one of them over real HTTP against a running mock, split by
surface: `test_metadata`, `test_odata_read`, `test_odata_write`, `test_odata_v4`,
`test_apply`, `test_delta`, `test_annotations`, `test_v2_annotations`,
`test_complex`, `test_links`, `test_etag`, `test_batch`, `test_rfc`, `test_idoc`,
`test_documents`, `test_oauth`, `test_messages`, `test_operations` and `test_auth`,
over the shared harness in `tests/support.py`.

CI runs them on Python 3.8-3.13 across Linux, macOS and Windows, and additionally
checks that `examples/demo.sh`, the packaged wheel and the Docker image still work,
and that every file in the repository is accounted for in
[docs/FILES.md](docs/FILES.md):

```bash
python3 tools/check_docs.py        # every file is documented
python3 tools/check_changelog.py   # the changelog is intact and says what changed
```

## Releasing

Releases go to PyPI through [Trusted Publishing](https://docs.pypi.org/trusted-publishers/):
PyPI trusts this repository's `publish.yml` workflow directly, so no API token is
stored in the repository or on anyone's laptop. Publishing a GitHub Release runs the
test suite, builds the sdist and wheel, checks that the tag matches the version in
`pyproject.toml`, and uploads. Running the workflow manually publishes to TestPyPI
instead, to rehearse.

`pyproject.toml` is the only place the version is written: `mocksap.__version__`
reads it back from the installed package metadata, and CI fails the release if the
tag, `pyproject.toml` and the built wheel disagree.

```bash
# bump `version` in pyproject.toml, then:
git tag v0.2.0 && git push origin v0.2.0
gh release create v0.2.0 --generate-notes
```

## Layout

```
mocksap/schema.py     entity types, navigations, services   (add shapes here)
mocksap/db.py         SQLite schema, number ranges, seed data
mocksap/odata.py      $filter parser, key predicates, V2 shaping, error envelope
mocksap/odata4.py     the V4 shapes: annotations, ISO dates, plain numbers
mocksap/apply.py      $apply pipelines compiled to GROUP BY
mocksap/delta.py      delta tokens, and what changed since one
mocksap/metadata.py   EDMX 1.0 / service document
mocksap/annotations.py  the V2 annotation document
mocksap/metadata4.py  CSDL 4.0, XML and JSON
mocksap/store.py      CRUD, deep insert, cascades, document defaults
mocksap/service.py    OData request dispatcher
mocksap/batch.py      $batch multipart and atomic changesets
mocksap/bapi.py       BAPI/RFC functions, JSON and SOAP transports
mocksap/documents.py  creating deliveries, invoices and journal entries
mocksap/idoc.py       IDoc inbox/outbox, ORDERS05 generation
mocksap/messages.py   sap-message warnings, and the rules that produce them
mocksap/oauth.py      the token store: grants, bearer validation, refresh
mocksap/server.py     HTTP front end, CSRF, auth, fault injection, /_mock API

tests/                one module per surface, all driven over real HTTP
examples/             the curl tour and a dependency-free Python client
docs/                 ARCHITECTURE.md and FILES.md
CHANGELOG.md          what each release changed
CONTRIBUTING.md       how to work on the project
.github/workflows/    ci.yml (tests, examples, wheel, image) and publish.yml
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how a request flows through
the mock and why the layering is the way it is;
[docs/FILES.md](docs/FILES.md) walks through every file in the repository.

## Scope

This is a black box. It stores what you send and returns it in SAP's shapes.
It does **not** do ATP checks, pricing procedures, output determination,
authorization objects, workflow or any other real SAP logic. What it is good for:
developing and testing integrations, contract tests in CI, demos, and load-testing
your side of the wire.

Two roadmaps are done: OData V4, complex types, `$links`, ETags and OAuth, then
more function modules, more IDoc types, `$apply`, delta handling, `sap-message`,
the UI annotations, and then the documents a sales order turns into, an object
page worth opening and a V2 annotation document.

Open an issue if you need something else - a shape the mock gets wrong is worth
one, and so is a shape it does not have yet.

Pull requests are welcome - [CONTRIBUTING.md](CONTRIBUTING.md) covers how to work
on the project and what the code values, and [CHANGELOG.md](CHANGELOG.md) records
what each release changed. Adding an entity set is usually a single declaration in
`mocksap/schema.py`; everything else - tables, `$metadata`, payload shapes - follows
from it.

## See also

[mock-edi](https://github.com/rseufert/mock-edi) is the same idea for EDI: a mock
trading partner that answers an X12 850 or EDIFACT `ORDERS` with the
acknowledgment, order response, ship notice and invoice a real one sends, and
misbehaves on demand. An IDoc `ORDERS05` and an X12 850 are the same business
document, so the two mocks make a reasonable pair of ends for testing the
middleware between SAP and a trading partner.
[`examples/invoice_check.py`](examples/invoice_check.py) is one.

[Testing an SAP-to-EDI Integration Without SAP or a Trading Partner](https://rickseufert.com/blog/2026/09/24/testing-an-sap-to-edi-integration)
uses the two mocks together: purchase orders out and confirmations in, then
invoices checked against what was ordered and shipped, with the failure modes
each test exercises.

[rickseufert.com](https://rickseufert.com/#projects) lists both mocks side by
side, with the worked examples and how to run each one.
