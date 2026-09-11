#!/usr/bin/env bash
# A guided tour of the mock, using nothing but curl.
# Start the server first:   python3 -m mocksap --port 8000
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
SO=$BASE/sap/opu/odata/sap/API_SALES_ORDER_SRV
BP=$BASE/sap/opu/odata/sap/API_BUSINESS_PARTNER_SRV

say() { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }

say "service catalog"
curl -s "$BASE/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection?\$format=json" |
  head -20

say "metadata (first 300 bytes of EDMX)"
curl -s "$SO/\$metadata" | head -c 300; echo

say "read a page of sales orders, newest first"
curl -s --get "$SO/A_SalesOrder" \
  --data-urlencode '$top=3' \
  --data-urlencode '$orderby=CreationDate desc' \
  --data-urlencode '$select=SalesOrder,SoldToParty,TotalNetAmount' \
  --data '$inlinecount=allpages&$format=json'

say "filter: open orders above 5000 EUR for one sales org"
curl -s --get "$SO/A_SalesOrder" \
  --data-urlencode "\$filter=SalesOrganization eq '1710' and TotalNetAmount gt 5000 and OverallSDProcessStatus ne 'C'" \
  --data-urlencode '$select=SalesOrder,TotalNetAmount,OverallSDProcessStatus' \
  --data '$top=3&$format=json'

say "expand a header with its items"
curl -s "$SO/A_SalesOrder?\$top=1&\$expand=to_Item&\$format=json" | head -40

say "fetch a CSRF token"
TOKEN=$(curl -s -D - -o /dev/null -H 'X-CSRF-Token: Fetch' "$SO/" |
  awk 'tolower($1)=="x-csrf-token:"{print $2}' | tr -d '\r')
echo "token: $TOKEN"

say "deep insert: create an order with two items"
CREATED=$(curl -s -X POST "$SO/A_SalesOrder?\$expand=to_Item" \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "SalesOrderType": "OR", "SalesOrganization": "1710",
        "DistributionChannel": "10", "OrganizationDivision": "00",
        "SoldToParty": "1000001", "TransactionCurrency": "EUR",
        "to_Item": [
          {"Material": "TG11", "RequestedQuantity": "2", "RequestedQuantityUnit": "PC", "NetAmount": "1998.00"},
          {"Material": "TG14", "RequestedQuantity": "4", "RequestedQuantityUnit": "PC", "NetAmount": "156.00"}
        ]
      }')
echo "$CREATED" | head -30
ORDER=$(echo "$CREATED" | sed -n 's/.*"SalesOrder": "\([0-9]*\)".*/\1/p' | head -1)
echo "created order: $ORDER"

say "patch it, then read it back"
curl -s -X PATCH "$SO/A_SalesOrder('$ORDER')" -H "X-CSRF-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"PurchaseOrderByCustomer":"PO-DEMO-1"}' -o /dev/null -w 'status %{http_code}\n'
curl -s "$SO/A_SalesOrder('$ORDER')/PurchaseOrderByCustomer/\$value"; echo

say "BAPI over JSON"
curl -s -X POST "$BASE/sap/bc/rfc/BAPI_SALESORDER_CREATEFROMDAT2" \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"ORDER_HEADER_IN":{"DOC_TYPE":"OR","SALES_ORG":"1710","DISTR_CHAN":"10","DIVISION":"00","CURRENCY":"EUR"},
       "ORDER_PARTNERS":[{"PARTN_ROLE":"AG","PARTN_NUMB":"0001000001"}],
       "ORDER_ITEMS_IN":[{"ITM_NUMBER":"000010","MATERIAL":"TG11","REQ_QTY":"5","COND_VALUE":"4995.00"}]}'

say "the same function over SOAP"
curl -s -X POST "$BASE/sap/bc/srt/rfc/sap/materialgetdetail/100/materialgetdetail/binding" \
  -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: text/xml' -d '<?xml version="1.0"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
 <soapenv:Body>
  <urn:MaterialGetDetail xmlns:urn="urn:sap-com:document:sap:soap:functions:mc-style">
   <Material>TG11</Material>
  </urn:MaterialGetDetail>
 </soapenv:Body>
</soapenv:Envelope>'; echo

say "outbound ORDERS05 IDoc for that order"
curl -s -X POST "$BASE/sap/bc/idoc/generate?format=xml" -H "X-CSRF-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d "{\"SalesOrder\":\"$ORDER\"}" | head -c 600; echo

say "post an inbound IDoc back in"
curl -s -X POST "$BASE/sap/bc/idoc" -H "X-CSRF-Token: $TOKEN" \
  -H 'Content-Type: application/xml' -H 'Accept: application/json' \
  --data-binary @- <<'IDOC'
<?xml version="1.0" encoding="utf-8"?>
<ORDERS05><IDOC BEGIN="1">
 <EDI_DC40 SEGMENT="1"><TABNAM>EDI_DC40</TABNAM><MANDT>100</MANDT>
  <IDOCTYP>ORDERS05</IDOCTYP><MESTYP>ORDERS</MESTYP><SNDPRN>PARTNER01</SNDPRN></EDI_DC40>
 <E1EDK01 SEGMENT="1"><CURCY>EUR</CURCY><BELNR>EXT-4711</BELNR></E1EDK01>
 <E1EDP01 SEGMENT="1"><POSEX>000010</POSEX><MENGE>10.000</MENGE><MENEE>PC</MENEE>
  <E1EDP19 SEGMENT="1"><QUALF>002</QUALF><IDTNR>TG11</IDTNR></E1EDP19></E1EDP01>
</IDOC></ORDERS05>
IDOC
echo

say "failure modes: one-off 500, then a simulated busy system"
curl -s -X POST "$BASE/_mock/faults" -H 'Content-Type: application/json' \
  -d '{"match":"A_SalesOrder","method":"GET","status":500,"message":"Backend unreachable","count":1}' >/dev/null
curl -s -o /dev/null -w 'first call:  %{http_code}\n' "$SO/A_SalesOrder?\$format=json"
curl -s -o /dev/null -w 'second call: %{http_code}\n' "$SO/A_SalesOrder?\$format=json"
curl -s -o /dev/null -w 'busy scenario: %{http_code}\n' -H 'sap-mock-scenario: busy' "$SO/A_SalesOrder?\$format=json"

say "what the mock saw"
curl -s "$BASE/_mock/requests?limit=5"
