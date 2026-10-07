# Changelog

Every release of [mock-sap](https://pypi.org/project/mock-sap/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

Entries waiting for a release are one file each in
[`changelog.d/`](changelog.d/), so that two pull requests adding an entry
cannot conflict. `tools/check_changelog.py --release X.Y.Z` assembles them
into a dated section here.

## [0.20.0] - 2026-10-07

This release is mostly this mock stopping saying yes. A dozen of these entries
are one shape: a request that was accepted, answered plausibly, and did
something other than what the client was told. `$search` matched nothing and
listed every row. A POST to `A_SupplierInvoice` returned a 201 for an invoice
that could never be paid. `BAPI_TRANSACTION_ROLLBACK` reported changes rolled
back when nothing had been. A property `$metadata` declared read-only took any
value a client sent. Each of those is worse than a 400, because a test built on
it passes here and fails against S/4 - which is the one thing a test double must
not do.

The other half is arithmetic. Every figure this mock produces is now computed in
`decimal.Decimal` and rounded the way a tax authority specifies, so the numbers
are the numbers SAP would have given.

And for the first time, something outside this project checks its output: the
`$metadata` every service serves is validated against the CSDL schemas OASIS and
Microsoft published ([#78]). Everything else here compares what the mock wrote to
what `mocksap/schema.py` says it should have written, and `schema.py` wrote it -
so both could be wrong together and nothing would notice.

**Read this before upgrading. Six things answer differently than they did in
0.19.0,** and each is a case where 0.19.0 was wrong rather than merely different:

- **A figure can move by a cent** ([#98]). Tax at 19% on 2.50 is 0.48, not the
  0.47 a binary float and banker's rounding gave. Taxes, gross amounts, order
  totals, unit prices on an IDoc and document balances all move, and so does the
  seeded demo data. A test asserting an exact amount against 0.19.0 will fail,
  and should.
- **A property `$metadata` calls read-only is refused** ([#97]). A POST that set
  `CreatedByUser`, or a PATCH that set `TotalNetAmount`, was accepted and is now
  a 400. Keys are not affected: a non-creatable key still means the server
  assigns a blank one, not that a client may not number its own item.
- **A POST to `A_SupplierInvoice` has side effects** ([#97]). It posts a real
  document now - an accounting document, an open item, a number from the range -
  so it is the same thing as an inbound `INVOIC`. It also refuses, by name,
  fields it used to accept and drop.
- **A V4 service refuses V2 `$filter` literals, and a V2 service refuses V4
  ones** ([#165]). `datetime'...'` and `100m` no longer work against a V4
  service; `2026-01-01T00:00:00Z` does. `$search` is a 501, `$skiptoken` a 400
  and `$format=xml` on entity data a 400, where all three were ignored.
- **A `DELETE` cascades all the way down** ([#163]). Deleting a sales order
  removes its items' pricing elements and its partners' addresses too, which
  were left behind, parentless and still addressable, before.
- **`BAPI_TRANSACTION_ROLLBACK` answers `E` after a call that wrote something**
  ([#167]). It answered `S` regardless, so a test relying on the rollback passed
  on a document that was never removed.

### Added

- **The served `$metadata` is held to schemas this project did not write**
  ([#78]). Every other check on this mock's output is a check against
  `mocksap/schema.py`, which is also what wrote it — so a reader and a writer
  built from one declaration agree with each other even when both are wrong
  about the format. `tools/check_csdl.py` validates all fourteen `$metadata`
  documents and the V2 annotation document against OASIS's CSDL XML 4.01
  schemas and Microsoft's CSDL 2.0 schema, vendored unmodified and checked
  against their published SHA-256 before use. It also holds every `Type=` to
  the primitive names those schemas enumerate, which an XSD cannot do because
  CSDL spells a type reference as a loose pattern, and refuses twelve
  deliberately broken documents on every run so that the check cannot quietly
  become a no-op. CI runs it as a job of its own.

- **`--rollback-type` and `--unsupported-option-status`** ([#170]). Two answers
  that depend on the client's conventions can be chosen when the mock starts.
  `--rollback-type W` makes `BAPI_TRANSACTION_ROLLBACK` answer a warning
  rather than an error when it follows a call it could not undo.
  `--unsupported-option-status 400` (or `501`) gives a refused `$search`,
  `$skiptoken` and non-JSON `$format` the same status, where by default
  `$search` is a 501 and the other two are 400s. Both are `Config` fields too,
  `GET /_mock/health` reports them as `rollbackType` and
  `unsupportedOptionStatus`, and neither has a value that restores the
  behaviour the fix replaced.

### Changed

- **A POST to `A_SupplierInvoice` posts the invoice** ([#97]). It used to store a
  row and nothing else: no accounting document, no open item, nothing saying it was
  not posted, and a 201 — an invoice that existed and could never be paid, because
  nothing downstream would ever select it. The POST now goes through
  `documents.post_supplier_invoice`, the one place a supplier invoice is made, so an
  invoice created this way is the same thing as one posted from an inbound `INVOIC`:
  the same accounting document, the same payable, the same number range. The entity
  publishes a gross and no split, so an invoice posted this way carries no input
  tax and its net is its gross; items are optional, and if they all state an amount
  they have to come to that net.
- **The POST no longer takes the number range's next value** ([#154]). It numbered
  from `MAX(existing) + 1`, which handed out the number the range was going to give,
  and the next inbound `INVOIC` then could not post at all. Fixed by the same
  routing, since that path draws from the range.
- **A field the POST does not act on is refused by name**, and the message says
  what the route does take. It used to accept and silently drop them, so a client
  could appear to set `AccountingDocument` on a document that had not been posted.
  The document number and fiscal year are refused outright: they are assigned when
  the invoice posts.

### Fixed

- **Concurrent requests no longer corrupt each other** ([#91]). The server takes
  each request on its own thread and has one SQLite connection, and nothing
  kept two requests off it at once: eight clients posting orders together had
  most of them answered 409 or 500, and rows were left behind from requests
  that failed. The mock now holds one lock for the part of a request that
  touches the database, so **requests are answered one at a time** and each
  gets the answer it would have got alone. The delay scenarios (`slow`,
  `timeout`, a fault rule's `delay_ms`, `--latency`) wait outside the lock and
  do not hold other requests up.
- **The listen queue is 128, not 5.** With the default, a pool of eight clients
  connecting at once had a connection refused in most runs on macOS.

- **A deep insert that fails partway leaves nothing behind** ([#92]). The
  header and each child were committed as they were written, so a refused
  third item left a header and two items for an order the client had been
  told did not exist. A deep insert is now one unit: any child failing takes
  the header and its siblings with it.
- **A changeset member that raises rolls the changeset back.** The rollback
  ran when a member answered with an error and not when one raised - a
  `sqlite3.Error`, or anything a handler did not turn into an answer - so the
  members before it stayed committed behind a 500. The same holds for a V4
  `atomicityGroup`.

- **An `INVOIC` whose totals disagree is status `51`, not a posted document**
  ([#93]). The balance check ran for `BAPI_ACC_DOCUMENT_POST` and nowhere else,
  so an inbound invoice whose total was not its net plus its tax was filed as
  `53` with an accounting document whose debits and credits differed. It is
  now declined before anything is written, with the figures and what is left
  over in the status text. The same goes for an invoice whose items all state
  a `NETWR` and do not come to its net.
- **A total and a tax with no net now balance.** With no `E1EDS01` `SUMID`
  `011` the net was taken for the gross, so 1190.00 with 190.00 of tax debited
  1380.00. The net is now what the tax leaves of the total. **An INVOIC that
  posted before can be refused now** if its own sums disagree; one that states
  only its total, as mock-acme's do, is unaffected.

- **A date `BAPI_ACC_DOCUMENT_POST` cannot read is a `RETURN` row, not a 500**
  ([#94]). An unparseable `BLINE_DATE` raised on the line it belonged to, with
  the header and the lines before it already written. `PSTNG_DATE`, `DOC_DATE`
  and every `BLINE_DATE` are now read before anything is posted, and each one
  that is not a date gets a row of type `E` naming its parameter, row and
  field. Nothing is written.
- **`YYYYMMDD` is a date on every Python.** SAP's own format was read by
  `date.fromisoformat`, which accepts it from Python 3.11 on, so `20260930` as
  a baseline date posted on 3.11 and was a 500 on 3.10 and earlier. As a
  posting or document date it was refused everywhere. All three fields now
  take `YYYYMMDD` or `YYYY-MM-DD` on every supported Python, and `00000000` is
  a date left blank.
- **An `INVOIC` dated on no day is status `51`.** `20261345` in `E1EDK03`
  `IDDAT` `026` was a 500 with the accounting document half written.

- **An order's delivery status is what its deliveries add up to** ([#95]). An order
  for 10 delivered as 5 and then 5 is now fully delivered, `C`. It used to end at
  `B` and stay there however many shipments arrived, because each delivery was
  asked on its own whether it covered the order and neither 5 did — so the split
  shipment, which is the common case this was wanted for, could not be
  represented. The status is now `A`, `B` or `C` from everything delivered against
  the order, and one function decides it wherever it is written.
- **Delivering more than was ordered is refused**, as SAP refuses past an item's
  over-delivery tolerance. `A_SalesOrderItem` grows
  `OverdelivTolrtdLmtRatioInPct` and `UnlimitedOverdeliveryIsAllowed` — SAP's
  UEBTO and UEBTK — both initial, so by default an order receives exactly what it
  asked for. Delivering 10 twice against an order for 10 was accepted and the
  order silently received double, because each call measured what was open against
  the order quantity rather than against what had already gone out. The refusal
  keeps the wording it had: an OData write gets a 400, a BAPI a BAPIRET2 error, an
  inbound `DELVRY` status 51 and nothing posted.
- **A generated `DELVRY07` ships what the order still has open**, so generating one
  twice sends the rest and then refuses, where it used to send the whole order
  quantity again. A line with no `DLV_QTY` likewise takes what is open rather than
  what was ordered.
- **The seeded orders no longer claim a delivery status their own deliveries
  contradict.** It was rolled at random, so an order could say `C` with nothing
  shipped against it. Every third seeded delivery now goes out half short, so all
  three statuses are in the data and are true of it.

- **A `DELVRY` that moved no sales order is status `51`, not `53`** ([#96]). An
  unknown `VGBEL`, or a `VGPOS` the order does not have, posts nothing and changes
  nothing; filing it as *Application document posted* told a client the opposite of
  what happened, which is the mistake the README's own rule exists to help catch.
  The status text names the order or the position. A position the order lacks was
  previously dropped from the line and the order reported on whatever was left -
  which, when nothing was, was a delivery of nothing reported as a delivery.
- **The refusal is still remembered.** `APPLIED` on a `51` says which order it was
  about, and reads back the same way afterwards, because the status being `51` is
  no reason to lose what was attempted. An unknown position is read back as that
  position rather than as "the order does not exist", which it is not - the
  position is stored and the sentence rebuilt from it, so a read cannot tell a
  different story from the receipt. One order moving is still enough for `53`, and
  the orders that did not move keep their own answer.

- **A property `$metadata` declares read-only can no longer be written** ([#97]).
  `sap:creatable="false"` and `sap:updatable="false"` were advertised and not
  enforced, so a POST could set `CreatedByUser` to anything and a PATCH could set
  `TotalNetAmount` to anything. A client generating its model from `$metadata` —
  which is why the mock ships it — was told a constraint it would have found out
  was fiction only against the real system. The check reads the same declaration
  the metadata is rendered from, so the two cannot drift, and it runs before
  anything is written, children of a deep insert included.
- **A key declared non-creatable is still a key a client may send.** That facet
  means the server will assign one that arrives empty, not that a caller may not
  number its own sales order item — which SAP allows and the BAPI layer passes
  straight through. Keys are left to `store`, which assigns the blank ones and
  refuses a change to one that is not.

- **An amount is a decimal, not a binary float** ([#98]). Tax at 19% on 2.50 is
  0.48 - what a tax authority specifies and what SAP does. It was 0.47, twice
  over: the float product lands just below 0.475, and `round()` takes a half to
  the nearer even digit anyway. Every figure this mock makes is now computed in
  `decimal.Decimal` and rounded with `ROUND_HALF_UP` - a tax, a gross, an order
  total, a unit price on an IDoc, a document's balance - so **a figure can
  differ from 0.19.0 by a cent**, in the direction a real system would have
  given. The seeded demo data moves with it.
- **`Edm.Decimal` is stored at the scale it declares.** The column is `TEXT`,
  because a `REAL` one has no scale to keep, so `Decimal(16,3)` gives back
  `2.500` whatever was written to it. Filtering, sorting and aggregating are
  unaffected: a decimal is cast before it is compared. A database file written
  by an earlier version keeps its `REAL` columns, since nothing migrates them -
  the rounding above still applies, but the declared type matches the storage
  only in a database this version created.
- **A value no decimal could hold is refused** instead of stored. `nan` and
  `inf` went into an amount and came back out on the wire, and `1e40` does not
  fit the `Decimal(16,3)` the property declares. All three now answer 400,
  naming the property, as any other invalid value does.

- **A field too long for SAP's dictionary is IDoc status `51`, not an OData 400**
  ([#101]). A 17-character reference on an inbound `INVOIC` came back as HTTP 400
  with a Gateway error envelope, from the surface whose whole contract is that a
  posting failure arrives as a status record on a `201`. So the one failure most
  likely to hit a client in production was the one case that never exercised its
  status-record handling. The IDoc is now filed, a docnum issued, and the status
  text names the field and its limit. Length and type checks still belong to the
  OData surface and still answer 400 there; only `_apply` is inside the catch, so a
  malformed IDoc is still a transport error.

- **A deep-insert child that is not a JSON object is a 400** ([#151]).
  `"to_Item": [5]` answered 500 with `'int' object has no attribute 'items'`
  as its message. It is now refused like any other malformed payload, naming
  the navigation property and which entry was wrong, at any depth.

- **`POST /_mock/reset` forgets the request log** ([#158]). The log survived a
  reset, and so did the counter behind every id, so a suite that reset between
  tests read the previous test's requests and got ids that depended on what
  had run before. `/_mock/requests` is empty after a reset now, and its ids,
  and those of `/_mock/rfc-log`, start again at 1. A reset refused for its
  arguments resets nothing.
- **Input the control plane cannot use is a 400 that says so**, where it was a
  500 with a Python message: a `limit`, a `seed` or a rule's `count` that is
  not a whole number, and a body that is JSON but not an object, on every
  `/_mock` endpoint.
- **A fault rule that could not be applied is refused when it is posted.**
  `{"status": "abc"}` was stored with a 201 and then failed every request it
  matched with `Unexpected mock failure`. A status that is not an HTTP status,
  a `match` that is not a regular expression and a `count` or `delay_ms` that
  is not a whole number are now a 400 naming the field. A status sent as text,
  `"503"`, works as it did.
- **A `Content-Length` that is not a count of bytes is a 400.** `-1` made the
  mock wait for a body that never ended, and `abc` closed the connection with
  no answer. Both are refused now, and the connection is closed, since the
  body cannot be told from the next request.

- **A payment document's own line carries the day it was cleared** ([#160]).
  It named itself as its clearing document and had a `ClearingDate` of `null`,
  so it was cleared by one field and open by the other, and a read of what was
  cleared by a key date left it out. Its `ClearingDate` and
  `ClearingCreationDate` are the document's posting date now, as they are on
  the invoice it paid. The same goes for the line a returned payment posts.
- **An item a returned payment reopened no longer says when it was cleared.**
  `ClearingCreationDate` stayed set after the clearing document and the
  clearing date had gone. It goes with them; `ClearingIsReversed` is still
  what tells a returned item from one nobody paid.

- **A GWSAMPLE_BASIC ETag changes when the entity does** ([#163]). Its
  `BusinessPartner` and `SalesOrder` render their ETag from `ChangedAt`, which
  no write ever moved, so an `If-Match` taken before a change still matched
  after it and a delta read never reported the entity as changed. A write
  moves `ChangedAt` now, as it always moved `LastChangeDate` elsewhere, and an
  entity created over HTTP gets `CreatedAt` and `ChangedAt` where it had
  `null` for both.
- **A `DELETE` removes everything beneath the entity, not one level of it.**
  Deleting a sales order took its items and partners and left the items'
  pricing elements and the partners' addresses, still served by their own
  entity sets with no parent. Deleting a GWSAMPLE business partner left the
  line items of the orders that went with it. Every level goes now, and each
  removed row is recorded for a delta read.

- **A V4 service reads V4 filter literals, and a V2 service V2 ones**
  ([#165]). Every `$filter` was read as V2, so a V4 service accepted
  `datetime'2026-01-01T00:00:00'` and `100m` and refused
  `2026-01-01T00:00:00Z`, which is how V4 writes it. A V4 service now takes a
  bare date, a date-time with `Z` or an offset, and an unsuffixed number, in
  `$filter`, in `$apply=filter(...)` and in a nested `$expand`. **It refuses
  the V2 spellings it used to accept**, and a V2 service refuses a V4 one;
  either way the 400 names the version the literal belongs to and how this
  service writes it.
- **`$search`, `$skiptoken` and `$format=xml` are refused instead of ignored.**
  Each was accepted and answered with what the request would have returned
  without it, so a `$search` that matched nothing listed every row. `$search`
  is a 501 pointing at `$filter`; a `$skiptoken` is a 400, since the mock
  never pages and so never issued one; and a `$format` other than JSON on
  entity data is a 400. The service document and `$metadata` still serve XML,
  and an `Accept` header asking for XML is still answered in JSON.

- **`BAPI_TRANSACTION_ROLLBACK` no longer claims a rollback it did not do**
  ([#167]). Every function call is committed as it runs, so a rollback has
  nothing it can undo - and it answered `S`, "Changes were rolled back",
  regardless. After a call that wrote something it now answers **`TYPE: "E"`**,
  naming the function modules whose work is still in place, so a test that
  relies on the rollback fails where it used to pass on a document that was
  never removed. A rollback that follows only reads, test runs, refused calls
  or a `BAPI_TRANSACTION_COMMIT` answers `S` as before.

## [0.19.0] - 2026-10-05

An invoice could still be paid twice, and these were the last two ways it
happened. 0.18.0 stopped this mock clearing invoices it had never paid; this
release stops it losing track of the ones it had. A credit on a statement was
read as a payment of ours coming back on the strength of nothing but its sign,
so a refund, a credit note settlement or a supplier returning an overpayment
that happened to quote an invoice already settled reopened it, and the next
payment run paid it again ([#89]). And nothing recorded that a run had paid an
item until the bank statement came back days later, so a second run started in
that gap selected the same invoice and paid it too ([#90]).

**Read this part before upgrading, because the first change is partly yours to
make.** 0.18.0's narrowings were all on this mock's side of the wire. This one
changes what a `FINSTA01` has to say, and a writer that does not say it loses
behaviour it has today.

- **A credit line has to declare which kind it is**, in `E1IDPF1-LINACTION`:
  `RET` for a payment of ours coming back, `RCV` for money arriving. Read in
  any case. A credit that declares **neither** reverses nothing and is listed
  under `UNPROCESSED` with the reason, because guessing `RET` pays an invoice
  twice and guessing `RCV` hides a payment that genuinely came back. So **a
  return that does not say `RET` stops reopening its invoice.** If you write
  statements for this mock and your returns are silent, they stop working on
  upgrade - visibly, with a reason, rather than by clearing the wrong thing.
  `LINACTION` on a debit is not consulted; money out has one reading.
- **A selection query of your own needs one more clause.** Open items now carry
  `PaymentRunID` and `PaymentRunDate`, and the fix depends on the selection
  excluding what another run already holds: `PaymentRunID eq ''` alongside the
  payment-block filter. Without it you keep the behaviour [#90] describes, and
  the bank will not catch it.
- **`RCV` is accepted, not acted on.** Money arriving reverses nothing, and
  posting it against a receivable is clearing receivables ([#65]), which is not
  built. A `RCV` line is reported as unprocessed saying exactly that. It is an
  honest refusal rather than a silent drop, and declaring the line is still
  worth doing today: it is what stops it being read as a return.

**If you drive this mock from mock-acme's packaged integrations, most of this
is already done.** mock-acme 0.2.0 on PyPI writes `LINACTION` on every credit
line - checked in the published wheel, not read off its changelog - so its
returns keep reopening and its receipts do not. It also still withholds the
structured reference on money arriving, which was its guard against the very
bug [#89] fixes. That guard is now unnecessary and harmless: a receipt
reverses nothing whether or not it quotes an invoice. Putting the reference
back, and raising its floor to this release, is
[rseufert/mock-acme#18](https://github.com/rseufert/mock-acme/issues/18).

### Added

- **An open item can say which payment run has it in flight** ([#90]). A
  payment run selected open items, paid them, and the fact that it had paid
  them lived only in its own memory until the bank statement came back days
  later: there was no state between *open* and *cleared*, so a second run
  started in between selected the same invoice and paid it again. One 1190.00
  invoice was paid 2380.00, and the bank cannot catch it, because its duplicate
  check is keyed on a `MsgId` the second run makes fresh. `A_SupplierInvoice`
  now takes F110's own key for a run - `PaymentRunID`, the six-character
  identification feature, and `PaymentRunDate` - and it reaches the open item
  the same way a payment block does, because what the next run reads is the
  item. The selection gains one clause: `PaymentRunID eq ''`. A claim is not a
  block, which is the point of it being the run's identification rather than
  one more blocking reason: a block says nobody should pay this at all, a claim
  says which run is paying it right now, and one character cannot say which.
  An identification longer than six characters is refused rather than
  truncated, since truncating would make two runs look like one. A claim does
  not outlive the item it was made about: clearing releases it, and so does a
  return, because an invoice that was paid and came back has to be selectable
  again - and both rows are released together, so the invoice and its item
  never disagree. Clearing itself does not consult the claim, since the
  statement arriving is what the payment was for.

### Fixed

- **Money arriving that quotes an invoice already paid no longer reopens it**
  ([#89]). A credit on a statement was read as a returned payment on the
  strength of nothing but its sign, so a refund, a credit note settlement or a
  supplier returning an overpayment that happened to quote an invoice the mock
  had paid reversed that clearing and left the invoice owed again - and the next
  payment run paid it a second time. camt.053 separates the two, where a
  received credit transfer carries `PMNT/RCDT/ESCT` and no `RtrInf`, and a
  `FINSTA01` line had nowhere to put it. A line now says which it is in
  `LINACTION`, whose domain pins no fixed values and so is free to carry two of
  this mock's own: `RET` a payment of ours coming back, `RCV` money arriving.
  `RCV` reverses nothing, and a credit that says **neither** reverses nothing
  either and is listed with the reason, because guessing `RET` pays an invoice
  twice and guessing `RCV` hides a payment that genuinely came back. The code is
  read whatever case it arrives in, a code nobody defined is undeclared rather
  than a return, and `LINACTION` on a debit is not consulted. **Any writer
  producing a `FINSTA01` for this mock has to declare its credits**, the same
  way it already has to write the direction as the amount's sign; a return that
  does not say so stops reopening. Posting the money a `RCV` line brings in is
  not built - that is clearing receivables ([#65]).

## [0.18.0] - 2026-10-05

Reconciliation stopped guessing. Three separate ways a statement line could
clear an invoice it had not paid all ended in the same place: an item marked
settled that nobody had settled, and the invoice that *was* paid left open for
the next payment run to pay a second time. A note to payee could beat the
structured reference the bank had already given ([#86]), two suppliers both
numbering an invoice `INV-100` were separated by whichever one the database
returned first ([#87]), and a line for 1190.00 USD cleared a payable of
1190.00 EUR because nothing compared the money the number was in ([#88]).

**Read this part before upgrading.** Every one of those is a narrowing: a line
that used to clear may now clear nothing, and where this mock cannot tell what
was paid it leaves the item open and says what it could not separate, rather
than picking a candidate. If you have assertions on a statement clearing
something, some of them are about to be right for a new reason - and a few will
fail.

- **A line that fits two suppliers' identically numbered invoices now clears
  neither.** Where the amount does not separate them either, both are reported
  and neither is settled. Two 1190.00 lines against two suppliers' 1190.00
  items used to clear both; they now clear nothing, because which line paid
  which supplier is not in the file - and since [#107] that choice also decides
  whose payment document the invoice lands in. Two invoices of the *same*
  supplier sharing a number are a duplicate, not this fault, and still settle
  once.
- **Currency decides alongside the amount.** An equal number in another
  currency is no longer a payment, both ways round rather than against a house
  currency, and a statement that names a currency nowhere clears nothing rather
  than assuming one.
- **A structured reference ends the comparison.** The note to payee is read
  only for a line that carries no reference, so a line whose reference is
  simply wrong no longer finds an invoice by appearing inside the note.
- **Every figure a refusal prints now carries its currency.** The reason
  strings are prose for a person reading why a line was left alone, but if
  anything of yours matches on their text, that text has changed.

**If you drive this mock from mock-bank's packaged payment run, read this
too.** mock-bank 0.7.0 on PyPI still writes `CUXWAERZ` and `FIIKWAER` as EUR
whatever account it is paying from, which is the error that hid [#88] for as
long as it lasted: its ACH run against 0.18.0 fails until mock-bank's next
release carries the fix that is already on their `main`. A euro run is
unaffected, which is exactly why their SEPA suite never caught this.

The code that sits *between* this mock and the others has moved out to
[mock-acme](https://github.com/rseufert/mock-acme) ([#137]), which now holds one
copy of each integration and runs them against all three mocks. One of them is
newer than it looks: the X12 820 remittance converter ([#132]) was written after
0.17.1 and moved before this tag, so **no release of this package ever carried
`examples/remittance.py`** - read its entry below for what the work was for
rather than for a file to go and find. `demo.sh` and `client.py` stay here.
Nothing else moved: no database migration, and `--port 0` finally prints the
port it bound ([#129]).

### Changed

- **An example that tells a supplier what was paid, as an X12 820**
  ([#132]). [#106] generates the `PEXR2002` payment advice; nothing converted
  it, so its segment choices had never been read by anything but their own
  tests. `examples/remittance.py` is the other direction from
  `invoice_check.py`: it reads an advice generated from a payment document and
  sends mock-edi the 820 a supplier reads, which is where the perspective
  flips — money leaving this account becomes `BPR03` `C`, a credit on theirs —
  and an amount that is not money out is refused rather than relabelled.
  `examples/test_remittance.py` runs against both mocks and shows the supplier
  accepting the advice and agreeing with it, plus the two refusals that make
  that worth anything: a total that is not the sum of its rows, and a
  settlement date ahead of the supplier's own clock.

- **The integrations in `examples/` have moved to mock-acme** ([#137]).
  `invoice_check.py`, `remittance.py` and their tests sat between this mock and
  the others, so they now live in [mock-acme](https://github.com/rseufert/mock-acme)
  with the rest of that code, one copy of each, tested against all three mocks.
  `examples/README.md` says which file became which. `demo.sh` and `client.py`
  stay. `examples/remittance.py` was added and moved between two releases, so no
  release of this package ever carried it.

### Fixed

- **A note-to-payee substring no longer beats a structured reference** ([#86]).
  A line whose structured reference names `INV-10` was also matched to `INV-1`,
  because the shorter number appears inside the longer one: the exact reference
  was compared first but failing it fell through to searching the note to payee
  instead of ending the comparison. The wrong invoice cleared and the right one
  stayed open for the next payment run to pay a second time; where the two
  invoices belonged to different suppliers, the line looked ambiguous under
  [#87] and cleared nothing at all. A bank that said which document it paid is
  now believed, and the note is read only for a line that carries no structured
  reference - the rule `references_in()` had documented all along without ever
  being called.

- **A statement line that fits two suppliers now clears neither** ([#87]). An
  invoice number is a supplier's own sequence, so two suppliers both numbering
  an invoice `INV-100` is ordinary - and nothing the mock reads off a statement
  line says which of them was paid: `E1IDPF1` gives it a reference, a note to
  payee and amounts, and the account the file names is the one being
  reconciled, not the payee's. Matching on reference and amount alone took
  whichever candidate the database happened to return first, which cleared one
  supplier's invoice with another's money and left an item open for the next
  payment run to pay a second time. Since [#107] it also decided which
  supplier's payment document the invoice landed in, so the wrong party was
  credited as well. Where the amount does not separate the candidates either,
  the line is now left alone and the reason names every item and its supplier
  - which is the difference between a line somebody can settle by hand and one
  they have to go and look up. Two items of the *same* supplier sharing a
  number are a duplicate invoice rather than this fault, and are still
  settled once.

- **A statement line is matched on its currency as well as its amount**
  ([#88]). A line for 1190.00 USD cleared a payable of 1190.00 EUR, because
  matching compared the reference and the number and never the money the
  number was in. An amount without a currency is not an amount, and two that
  happen to be equal are not a payment - which is the one disagreement that
  reads as agreement, and is why this survived as long as it did: mock-bank's
  payment run wrote `CUXWAERZ` and `FIIKWAER` as EUR even for a dollar
  account, and the two errors cancelled. The currency now decides alongside
  the amount, both ways round rather than against a house currency; a code is
  read whatever case it arrives in; and a statement that names a currency
  nowhere clears nothing, since filling in a default would invent the half of
  the amount that decides whether a line is a payment at all. Every figure a
  refusal prints now carries its currency, so `1190.00 EUR` against
  `1190.00 USD` reads as the disagreement it is.

- **The startup banner prints the port the server bound, not the one requested**
  ([#129]). `--port 0` asks the operating system to choose a free port, which is
  how several mocks run side by side and how CI avoids a clash. The banner was
  built from the parsed arguments, so every URL it offered ended `:0` - and
  since the banner is the only place the port is reported, there was no way to
  find the server short of `lsof`. The port now comes from the bound socket.
  The host is still the one that was typed: `0.0.0.0` reads back as what the
  caller asked to bind, where nobody typed the port.

## [0.17.1] - 2026-10-05

A patch for one startup failure, and the reason it is not part of 0.17.0 is
only that it landed an hour after that tag was cut.

Starting the mock ran a reverse DNS lookup on the address it had just bound, to
fill in a name nothing reads. The port was bound by then but not yet listening,
so where the resolver is slow to answer for that address - a CI runner, a
container with no reverse record - nothing answered until the lookup returned,
and whoever was polling `/_mock/health` gave up first and reported a start that
had failed for no visible reason. That is what happened to rseufert/mock-acme on
a macOS runner against 0.16.0.

Upgrade if you start this mock anywhere you do not control the resolver. If
startup has always been instant for you, nothing here changes anything: the only
visible difference is that `httpd.server_name` is now the bound address rather
than whatever the lookup returned, and nothing in the package reads it.

### Fixed

- **Startup no longer waits on a reverse DNS lookup of the bind address**
  ([#127]). The server bound through `http.server.HTTPServer.server_bind`,
  which calls `socket.getfqdn` on the address it has just bound to fill in a
  `server_name` nothing here reads. Where the resolver is slow to answer, the
  port did not open until it had, and a caller polling `/_mock/health` gave up
  first. The server now binds without the lookup, as mock-edi and mock-bank do;
  `server_name` is the bound address.

## [0.17.0] - 2026-10-04

The mock can tell a supplier what it paid them. Generating a `REMADV` from a
payment document renders a `PEXR2002` naming every invoice that payment settled,
with the number the supplier quotes and what came off it. That closes the one
leg of the procure-to-pay loop where SAP and the bank agreed and the supplier was
never told - and it is written from the payment's own data, so the advice says
what the system did rather than what something downstream composed on its behalf.

**Read this part before upgrading.** Making the advice worth sending meant
changing how a payment posts, and that is a behaviour change rather than an
addition. A statement that settles three invoices for one supplier now posts
*one* payment document covering all three, where it previously posted three. A
remittance advice generated from a payment that settled exactly one invoice has
one row and a total that trivially equals it, which is why the two go out
together: nothing the mock emitted could otherwise exercise a reader's check
that a total agrees with its parts.

Three things a client may have assertions on:

- **One payment document can now settle several invoices.** Code that read a
  clearing document and expected a single invoice behind it will see more.
- **`ClearingItem` is no longer always `000001`.** It points at the line of the
  payment document that paid that invoice, which is what makes an advice's rows
  agree with the payment.
- **A payment's `ReferenceDocument` is the statement that produced it**, not one
  of the invoices it covers - a field that stopped meaning anything at two. Each
  invoice's own reference is still on its item and in the posting outcome.

Returns now post after payments within one statement, so a statement that pays
an invoice and takes the money back reads the same whichever order the bank
listed those two lines in. Previously a credit could only reverse a clearing
made by a line above it.

Nothing else moved: no database migration, and the advice is a new generator
rather than a change to an existing one.

### Added

- **A supplier can be told what was paid: a `REMADV` generated from the
  payment** ([#106]). `POST /sap/bc/idoc/generate` with `mestyp=REMADV` and a
  clearing document renders a `PEXR2002` naming every invoice that payment
  settled — each with the number the supplier quotes and what came off it —
  and a total that is the sum of those rows rather than a figure of its own.
  The advice is dated by the payment's own posting date, so it cannot claim a
  settlement that has not happened; a document that settled nothing is refused
  rather than advised. It is the first generator whose source is not a sales
  order, so the route now asks what a message type reads instead of assuming.

### Changed

- **One payment document per supplier, not per statement line** ([#107]). A
  statement that settles three invoices for one supplier now posts one payment
  document covering all three — one supplier line per invoice, one credit to
  the bank for the total — and each item's `ClearingItem` points at the line
  that paid it, where it was previously hardcoded to `000001` because there
  was only ever one. This is how a payment run pays, and it is what makes a
  remittance advice worth sending: the supplier gets a single credit and has
  to be told which invoices it covers. Items that disagree on company code or
  currency still get separate documents, since a document header carries one
  of each. Two further changes follow from it: a payment's
  `ReferenceDocument` is now the statement that produced it rather than one of
  the invoices it covers, and returns are posted after payments, so a
  statement that pays an invoice and takes the money back reads the same
  whichever order the bank listed those lines in.

## [0.16.0] - 2026-10-03

0.15.0 made the mock remember what posting a bank statement decided. This does
the same for the other two message types that post something, which dropped
their answer in exactly the same way: a `DELVRY07` works out which sales orders
it moved and which it could not find, an `INVOIC02` posts an invoice and a
payable and reports both numbers, and in each case the POST receipt was the only
place that answer ever existed. A client that did not keep the response had no
way back to it - and for the invoice, those two numbers are the ones a payment
later quotes.

So the asymmetry is gone: a client no longer has to know which of the three
posting types remembers. That is the whole of this release, and it closes the
question 0.15.0 left open rather than adding a surface.

Nothing a client does against 0.15.0 behaves differently. `APPLIED` is the same
structure it already was, on a read that already existed, and an IDoc that
posted nothing - an `ORDERS05`, or one the application declined - still carries
no `APPLIED` at all rather than an empty one that would read as an answer.

Two things moved inside the package, neither part of the HTTP surface and both
introduced by 0.15.0 earlier the same day: `reconcile.outcome_of` is now
`outcome.of`, and
`reconcile.message_for` is now `outcome.statement_message`, because filing what
posting decided is no longer the statement's business alone. A database written
by 0.15.0 gains the two new tables when this version opens it.

### Changed

- **A posted DELVRY or INVOIC remembers what it decided, the way a FINSTA
  already did** ([#116]). Reading the IDoc back with
  `GET /sap/bc/idoc/<DOCNUM>` returns the same `APPLIED` the POST receipt
  carried: for a delivery, each sales order it moved and the one it could not
  find; for a supplier's invoice, the invoice and the accounting document a
  payment later quotes. Posting answered once and kept none of it, so a client
  that did not hold on to the response had no way back to either. An IDoc that
  posted nothing still has no outcome at all, rather than an empty one.

## [0.15.0] - 2026-10-03

The mock stops forgetting what it decided. Posting a `FINSTA01` already worked
out, line by line, which invoice it cleared and why it refused the rest; it
answered once and kept none of it, so a client that did not hold on to the
response had no way back. Reading the IDoc now returns that same answer, and
`?settled=` finds it from the invoice's side instead - which is the direction an
integration actually asks in: not what this IDoc did, but what settled my
invoice.

Nothing a client does against 0.14.0 behaves differently. Both additions are
additions - a key on a read that was already there, and a new parameter - so
this is a minor because the surface grew, not because anything moved. A database
written by 0.14.0 opens here and picks up the two new tables; an IDoc posted
before the upgrade carries no outcome and reads without one, which is the truth
about it rather than an empty answer implying it settled nothing.

The architecture notes also stop promising thread safety this mock has never
had. `check_same_thread=False` switches off Python's check, not the problem, and
anyone who read that section and load-tested a client against the mock was told
something untrue. One request at a time is the supported shape; #91 is open for
the rest.

### Added

- **Posting a `FINSTA01` is remembered, not just answered** ([#105]).
  Posting a bank statement already worked out, per line, which invoice it
  cleared and why a line was refused, returned that once on the POST receipt
  and then dropped it: the IDoc's own read gave nine columns and none of them
  was this, so a client that did not keep the response had no way back to it.
  `GET /sap/bc/idoc/<DOCNUM>` now returns the same `APPLIED` the POST did, read
  back from where posting filed it rather than recomputed, and
  `/_mock/idocs?settled=<AccountingDocument>` answers it from the other side -
  not "what did this IDoc do?" but "what settled my invoice?", which is the
  join an integration actually has. The listing itself stays narrow: one IDoc
  has one outcome, and a listing of fifty would carry fifty.

  The outcome is a header row and its lines rather than a JSON column, because
  a blob would mean reading every IDoc to answer the backwards question. Both
  tables are new, so a database written by an earlier version picks them up on
  open rather than needing a migration; an IDoc posted before this change
  carries no outcome, and reads without one, which is the truth about it.

### Fixed

- **The architecture notes stop promising something the mock does not do**
  ([#91]). *One connection, many threads* said `ThreadingHTTPServer` serves
  requests concurrently against one SQLite connection and that "SQLite
  serialises the access". `check_same_thread=False` switches off Python's
  *check* that a connection is used from the thread that opened it; it adds no
  synchronisation. SQLite serialises individual statements, not the read-decide-
  write sequences almost everything here is made of, and the only lock in
  `db.py` guards number-range allocation. Anyone who read that sentence and
  load-tested a client against this mock was told it would hold, and it does
  not. The section now says so, and says that one request at a time is the
  supported shape today.

  Documentation only - no behaviour changed, and the concurrency itself is still
  wrong. #91 stays open for the fix.

## [0.14.0] - 2026-09-30

Repeatability, and the logs admitting what they already hold. Until now every
date the mock produced came from the host's real UTC clock at the point of use,
so the same script wrote differently dated documents on different days and a
dated assertion went stale by itself overnight. There is one clock now, `--clock`
pins it and `POST /_mock/advance` moves it. It knows nothing about business days
- no cutoff, no weekend, no holidays. Those are a bank's questions.

### Added

- **One clock, which `--clock` pins and `POST /_mock/advance` moves.** Every
  date the mock computed - a posting date, a document date, a payment term's
  baseline, the timestamp on a log row - came from `datetime.utcnow()` at the
  point of use, about twenty-five call sites across eleven modules. So a run
  was not repeatable: the same script produced differently dated documents on
  different days, and an example whose assertion depended on a date went stale
  by itself overnight. `mocksap/clock.py` now holds system time, every one of
  those call sites reads it, `/_mock/health` and `/_mock/state` report it, and
  `POST /_mock/reset` returns it to the pinned moment rather than to real time,
  because `--clock` is configuration and a reset is not meant to undo
  configuration. It holds an offset rather than an instant, so it keeps ticking
  between advances - a frozen clock stamps every row in a run identically,
  which in a log is indistinguishable from a bug. It is naive UTC, which is
  exactly what `utcnow()` returned at every call site replaced, and it knows
  nothing about business days: no cutoff, no weekend, no holidays. Those are a
  bank's questions. ([#81])

- **`?verbose=1` on `/_mock/requests` and `/_mock/rfc-log` returns the payloads
  both logs were already recording.** `rfc_log` stores every call's parameters
  and full answer and `request_log` stores every request's headers and body;
  the two endpoints selected neither. The answer is where a BAPI's `RETURN`
  table lives, which made a *business* error invisible from outside - a BAPI
  reports one by returning normally with HTTP 200 and a `RETURN` row of type
  `E`, so through the narrow log a call refused by `/_mock/bapi-behaviour` and
  one that worked were the same two fields. It is opt-in because the payloads
  are capped at 20000 characters for an RFC call and 8000 for a request body,
  so a default `limit` of 25 could answer with half a megabyte; the default
  shape is unchanged, so a client already parsing these rows keeps what it was
  written against. Credential headers - `Authorization`, `Cookie`,
  `Set-Cookie`, `X-CSRF-Token`, `Proxy-Authorization` - are redacted, because
  `/_mock/requests` is not behind `--auth` and a mock that handed back a
  client's own token would be teaching a bad habit. The header name is kept
  with a marker in place of the value, since a client debugging an auth failure
  cannot tell an absent key from a header that was never sent. ([#80])

### Changed

- **The logs and the control plane agree on one timestamp shape.**
  `request_log.ts` and `rfc_log.ts` carried microseconds and no zone, an IDoc's
  `created_at` was truncated to the second and had no zone, and only
  `/_mock/health` appended a `Z`, so a client reading two of them parsed two
  shapes and guessed at the zone of both. All of them are now ISO-8601 to the
  second with a `Z`. The extra digits were never a tie-breaker, because both
  logs are ordered by `id`. OData's `/Date(milliseconds)/` on the entity
  payloads is unchanged - that is SAP's wire format, not a timestamp this mock
  chose. ([#81])

- **`STFC_CONNECTION` and a statement's fallback posting date read UTC rather
  than the host's local date.** Both used `date.today()` where everything
  around them used `utcnow()`, so on a machine behind UTC they disagreed with
  every other date in the same run by a day. Both now read the clock. ([#81])

- **The clearing path no longer assumes a payable.** `reconcile` selected open
  and cleared items with the supplier account type closed over, and
  `store.open_items` had `'K'` as a SQL literal; both now take the account type
  as an argument, defaulting to the supplier side. `_invoice_reference`
  dispatches on the item instead: a payable reads the supplier's own invoice
  number, because that is what we quote when we pay it, and a receivable reads
  the billing document we sent, because that is what a customer quotes.
  SAP's KOART constants moved from `documents` to `store`, next to the queries
  that select by them, and `documents` re-exports them under the same names.
  Nothing a client can see changes - no statement is matched differently and no
  item clears that did not before - and clearing a receivable is not built yet.
  This is the groundwork for it. ([#65])

[#81]: https://github.com/rseufert/mock-sap/issues/81
[#65]: https://github.com/rseufert/mock-sap/issues/65
[#80]: https://github.com/rseufert/mock-sap/issues/80

## [0.13.3] - 2026-09-29

Everything here is in `examples/`. `mocksap/` is unchanged from 0.13.2, so the
mock behaves exactly as it did - and because the wheel carries no examples, this
release reaches you through the source archive or a clone rather than through
`pip install`. It is a patch because no behaviour of the mock changes at all.

It is worth cutting anyway: `invoice_check` is what this project's walkthrough
tells a reader to run, the currency fix below was being described as shipped in
0.13.2 when it was not, and an example nobody can install is an example that
does not exist. Two test docstrings are corrected with it - one claimed
something the test did not do, and one printed cut in half under `-v`.

### Fixed

- **`examples/invoice_check.py` ordered in one currency and was billed in
  another, and its three-way match did not notice.** The `850` it sent carried no
  `CUR` segment, so mock-edi fell back to its own default and invoiced a EUR
  purchase order in dollars; `problems()` then compared `12.50` with `12.50` and
  passed it, because every check there subtracts bare decimals. A payment run
  downstream refused the item - a SEPA transfer is in EUR - which is where it
  surfaced. The order now declares its currency, and an invoice whose currency
  differs from its purchase order is blocked by name. An invoice that names no
  currency is not blocked: `CUR` is optional, and absence is not disagreement.
  ([#74])

[#74]: https://github.com/rseufert/mock-sap/issues/74

## [0.13.2] - 2026-09-28

Two fixes, both found on the first attempt to run all three mocks end to
end, and both the same fault: a mock that reported having done something it
had not. The loop the projects page describes - an EDI invoice approved into
SAP, then paid and cleared - had not run through since 0.12.0 added payables,
and four worked examples using two mocks each could not see it, because every
one of them asserted what SAP received rather than what posting it created.

**This release can turn a passing test red.** If you assert status `53` on an
inbound `INVOIC` or `DELVRY` that posts nothing, you will now get `51`. That
is the fix: `53` means *Application document posted*, and nothing was.

### Fixed

- **An inbound IDoc that posted nothing reported status 53.** `53` is
  *Application document posted*, and an `INVOIC` naming no supplier, or no
  total, applied nothing and still reported it - so a client doing exactly the
  right thing, reading the status rather than trusting the `201`, was told the
  invoice posted and owed nobody anything. The whole point of
  `/_mock/idoc-posting` is that an accepted IDoc is not a posted one; this was
  the mock making the same mistake it exists to expose. Such an IDoc is now
  **51**, *Application document not posted*, with a status text naming the
  segment that was missing, and the status is decided before the IDoc is filed
  so the stored record is the one that happened. A `DELVRY` naming no order
  line is the same and behaves the same, as is any of the three sent as a flat
  file, which cannot be posted because this mock has no fixed-width layout for
  the segments. An `ORDERS05`, which has no application step at all, still posts
  `53` in either dialect. Found on the first attempt to run all three mocks end
  to end: the payment run had nothing to pay, and nothing anywhere said why.
  The IDoc that prompted it is the one below. ([#67])

- **`examples/invoice_check.py` posted invoices that owed nobody anything.**
  `invoic_idoc()` built an `INVOIC02` that named the document but not the
  supplier: no `E1EDKA1` with `PARVW` `LF`, so SAP had nobody to owe and created
  **no supplier invoice and no open payable** - measured against a fresh mock,
  zero of each - while answering status `53`. Every invoice this example
  approved had been "posted" and left no money owed, so a payment run selecting
  open items found nothing to pay. It also omitted `NETWR` and `VGBEL`/`VGPOS`
  per item, leaving the payable's lines worth nothing and naming no purchase
  order, and sent no currency, terms or invoice date. All of them are there now,
  and `read_810` keeps the invoice's date (`BIG01`), currency (`CUR`) and net
  payment days (`ITD07`) instead of reading only what the three-way match needs.
  ([#68])

  The reason five green tests never noticed: they all asserted what SAP
  *received* - the IDoc and its status - and none asserted what posting it
  created. There are now tests for the supplier invoice, its item amounts and
  purchase-order references, the open payable, and the due date the invoice's
  own terms give it. Had the mock not been reporting `53` for it, this would
  have been loud from the day payables arrived in 0.12.0. ([#68])

[#67]: https://github.com/rseufert/mock-sap/issues/67
[#68]: https://github.com/rseufert/mock-sap/issues/68

## [0.13.1] - 2026-09-27

Two fixes found by integrating, not by reading. mock-bank built a payment run
against 0.13.0 and hit a mock that allowed something the real system refuses,
and an invoice whose block did not reach the money. Both are the kind a mock
has to get right to be worth pointing at.

### Fixed

- **The open-item cube accepted writes, and the real service does not.**
  `API_OPLACCTGDOCITEMCUBE_SRV` was declared `sap:creatable/updatable/deletable
  ="true"` and took `PATCH`, so a client could block or clear an item a way
  that works here and fails against S/4 - the one thing a mock must never
  allow. It is now read-only in `$metadata` **and** refuses writes with 405,
  because declaring it is not enough on its own. A `Service` can now say
  `read_only`. Arranging an item for a test moved to `PATCH /_mock/open-items`,
  which is plainly the mock's own control plane rather than a pretend SAP API.
  ([#62])

- **A blocked supplier invoice left its open item payable.** `PATCH`ing
  `PaymentBlockingReason` on `A_SupplierInvoice` returned 204 and showed the
  block, while the open item a payment run actually reads stayed blank - so the
  invoice said blocked and the money went out anyway. The invoice and the item
  its accounting document posted are separate rows, and they are now kept in
  step. ([#62])

- **A supplier outside the IBAN countries carried a German BIC.** The seed
  picked a `SWIFTCode` whatever the account's country, so a US supplier paid on
  a routing number was given `DEUTDEFF`. It now has no BIC, which is what it
  has. ([#62])

### Added

- **Four suppliers banked where mock-bank can act on them.** `1000013` GLOBEX,
  `1000014` INITECH, `1000015` EURODIS and `1000016` Umbrella Logistics, at the
  `NL…MOCK…` IBANs mock-bank holds, with BIC `MOCKNL2A`. Three carry mock-edi's
  partner names, so one trading partner is recognisable in EDI, in SAP and at
  the bank. Without them a payment run had to write its own master data before
  it could test anything. The suppliers numbered `1000009` to `1000012` are
  untouched, because other projects' tests name them. ([#62])

## [0.13.0] - 2026-09-27

Reconciliation, and with it the end of the chain 0.12.0 started. A supplier's
invoice became money owed; now the bank says what happened to it, and the mock
clears what settled and reopens what came back. Built in two halves by two
people: reading the statement, and acting on it.

### Added

- **Posting a `FINSTA01` clears the open items it paid.** Reading the statement
  said what it claimed; this acts on it. A debit line clears an open item when
  the reference **and** the amount agree - the structured `E1EDP02` reference
  first, the note to payee second, searched with its spaces removed as well
  because a bank wraps the note at 70 characters wherever it falls and
  `SUP-9001` can arrive as `SUP- 9001`. A credit line quoting a cleared item is
  a returned payment: the clearing is reversed, the item is open again so a
  payment run will try it once more, and `ClearingIsReversed` stays set so
  *paid and returned* can still be told from *never paid*. **Every line it
  cannot place is reported, not guessed at** - a wrong amount names both
  numbers - because that is the reconciliation gap a treasury team works
  through each morning. The clearing document clears its own supplier line, so
  a payment does not leave a payable behind it; a statement is checked against
  its own arithmetic and against the last one's closing balance, reported as
  two different findings; an interim statement is not chained to the previous
  one; and a statement that did not post clears nothing. ([#57])

- **A `FINSTA01` bank statement can be read.** `mocksap/statement.py` turns one
  into its account, number, date, balances and lines, with amounts as
  `Decimal`, and checks that opening plus credits less debits is closing. The
  balances are SAP's own `EDIF5025` qualifiers (`019`-`024`), not the UN/EDIFACT
  codes. Which side a line is on is the mock's own convention - the amount's
  sign, `1190.00-` a debit - because SAP pins no values for it; lockbox (`LOCKBX`) and flat-file statements are refused. Nothing acts on a
  statement yet - clearing the open items it pays is the rest of
  [#57](https://github.com/rseufert/mock-sap/issues/57).

## [0.12.0] - 2026-09-27

The payable side of the document chain. Until now a sales order became money
owed *to* you and nothing became money owed *by* you: an inbound `INVOIC` was
filed and forgotten, there was no open item to select, and the account to pay
into had to be kept somewhere outside SAP. Three of those are fixed here.

Reconciliation is not: posting a `FINSTA01` statement to clear what was paid is
[#57](https://github.com/rseufert/mock-sap/issues/57), and until it ships
[mock-bank#16](https://github.com/rseufert/mock-bank/issues/16) stays blocked.

### Added

- **An inbound `INVOIC` becomes a supplier invoice with an open payable.** It
  used to be filed and nothing else, so an invoice `invoice_check` had approved
  left nothing to pay. Now it posts: `API_SUPPLIERINVOICE_PROCESS_SRV` serves
  `A_SupplierInvoice` with the supplier's own number in
  `SupplierInvoiceIDByInvcgParty` (the reference a payment quotes back),
  `BPBankAccountInternalID` naming which of the supplier's accounts to pay, and
  `to_SuplrInvcItemPurOrdRef` recording the purchase order each line bills
  against, so a three-way match can be done over OData. The accounting document
  it posts balances and leaves an open payable for a payment run to select.
  **A failed posting creates nothing** - under a `51` rule there is no invoice
  and no payable - and **the same invoice twice creates two**, because SAP's
  duplicate check is configuration and inventing one would hide the commonest
  way companies pay twice. ([#54])

- **Open items: what is still owed, and what cleared it.** A payment run does
  not read journal entries, it asks what it still owes and whether it was due,
  so this serves `API_OPLACCTGDOCITEMCUBE_SRV` with `A_OperationalAcctgDocItemCube`
  - `NetDueDate`, `PaymentTerms`, `PaymentBlockingReason`,
  `ClearingAccountingDocument`, `ClearingDate`, `ClearingItem`,
  `ClearingIsReversed` - and the whole selection a payment run makes is one
  `$filter`. An item is open while its clearing document is **blank**, which is
  an empty string and not a missing field, because that is what clients filter
  on. **The cube is a view, not a copy:** it reads `A_JournalEntryItem`'s own
  rows, so the two services cannot disagree, it stores nothing of its own, and
  `/_mock/state` does not count it. An entity type can now declare `view_of`,
  which is how SAP publishes more than one read of the same document.
  `BAPI_ACC_DOCUMENT_POST` gained `ACCOUNTPAYABLE` and `ACCOUNTRECEIVABLE`, the
  tables a real one uses for the lines that are owed, carrying `PMNTTRMS`,
  `BLINE_DATE` and `PMTBLOCK` - so a line's due date follows from its terms
  rather than being supplied ready-made. ([#55])

- **Supplier bank details, so the account to pay into comes from SAP.**
  `A_BusinessPartnerBank` in `API_BUSINESS_PARTNER_SRV` and its V4 twin, reached
  as `to_BusinessPartnerBank`, keyed by `BusinessPartner` + `BankIdentification`
  and carrying `IBAN`, `SWIFTCode`, `BankCountryKey`, `BankNumber`,
  `BankAccount`, `BankAccountHolderName` and `IBANValidityStartDate`. Without it
  an integration keeps its own list of accounts, which is the shadow master data
  that goes stale and pays the wrong one. **A mistyped IBAN is refused when it
  is entered**, by ISO 13616's check digits rather than a length rule, on create
  and on patch, and the seeded IBANs pass the same check a client's do. The seed
  gives one supplier two accounts, because a client that pays whichever came
  back first is right by luck until a supplier has two - and the suppliers
  outside the IBAN countries carry a bank number and an account number and no
  IBAN at all, because a US supplier is paid on a routing number and inventing a
  US IBAN would be a shape no bank would take. ([#56])

## [0.11.2] - 2026-09-25

One fix, in what a token response says about its own lifetime. Nobody was being
hurt by the old value, which was short by at most a second; it was simply not the
number RFC 6749 asks for.

### Fixed

- **A token response reported the lifetime left rather than the lifetime issued.**
  `expires_in` was computed from the clock each time it was read, so the value in
  a token response was short by however long the mock spent building it - and
  because it is rounded, half a second was enough to answer `3599` for an hour or
  `0` for a one-second token. RFC 6749 defines a token response's `expires_in` as
  the lifetime of the token, which is a property of the token and not of the
  clock, so it is now derived from when the token was issued. `GET /_mock/tokens`
  still counts down, because remaining seconds are what a listing of live tokens
  is for. Found while hunting the flake that turned out to be [#49]; two tests
  asserted the exact value and would have failed on a slow enough response.
  ([#51])

## [0.11.1] - 2026-09-25

One fix, and it is worth reading if you use delta: a delta read could answer that
nothing had changed when something had, and never mention it again. Every release
with delta support has had this.

### Fixed

- **A delta read lost a change made in the same millisecond as the token.**
  Change timestamps carry milliseconds and nothing finer - `/Date(ms)/` cannot
  express more - and `changed_since` compared with `>`, so a change sharing the
  token's instant was dropped and never reported again: the client was told
  nothing had changed. Measured before the fix, 189 of 300 reads lost the change
  they were asked for, every one of them with the row's timestamp exactly equal
  to the token; after it, 0 of 300, with the boundary hit just as often. The
  token was also minted *after* the rows were read, so a change landing in
  between fell in the gap. It is now taken before the query, and the comparison
  is `>=`, matching what `deletions_since` has always done. A delta read is
  therefore at-least-once: a change at the boundary instant may be reported
  twice, which costs a client an idempotent write it must be able to do anyway,
  where losing one costs it the row. This is what surfaced as the intermittent
  `test_the_link_moves_on` failure, about one suite run in seven.
  ([#49])

## [0.11.0] - 2026-09-25

The other half of failure on demand: a BAPI that answers 200 and says no in the
payload. With 0.10.0's IDoc statuses, both places this mock can lie to a client
the way a real system does are now askable for.

### Added

- **A BAPI can fail for a business reason.** The `E` rows the mock returned all
  came from malformed input; a well-formed call could not be made to fail the way
  production does. `POST /_mock/bapi-behaviour` sets what a function module
  answers: `E` or `A` replaces the call, so no document is created and no number
  is drawn from the range, and `W` or `S` rides along with a call that does its
  work. The HTTP status stays `200` and SOAP returns a `<...Response>` envelope
  rather than a fault, because a business error is not a transport error - which
  is exactly the combination that gets a client to commit on a failure. Both
  transports go through the same path, so both honour it. Function modules with
  no `RETURN` table (`RFC_PING`, `STFC_CONNECTION`, `RFC_READ_TABLE`) are refused
  with the reason, since a real one raises an ABAP exception instead.
  ([#46])

## [0.10.0] - 2026-09-25

Failure on demand, above the transport: an inbound IDoc the mock accepts and
then declines to post. It found the bug it exists to find on the first try, in
this package's own worked example.

### Added

- **An inbound IDoc can be accepted and then fail to post.** Every failure this
  mock could simulate was at the transport level - an HTTP status and a message.
  `POST /_mock/idoc-posting` sets what the application does with an inbound IDoc
  instead: status `51` (application document not posted), `56` or `68`, filtered
  by `mestyp`/`idoctyp` and optionally spent after `count` IDocs. The HTTP status
  stays `201`, because the IDoc *was* received and a docnum *was* issued; the
  failure is in the status record, which is where a real client has to look for
  it. A `DELVRY07` that does not post leaves the order's delivery status where it
  was and creates no delivery - a failed posting does nothing, which is the whole
  difference between `53` and `51`. `POST /_mock/reset` clears the rules.
  ([#45])

### Fixed

- **`examples/invoice_check.py` treated an accepted IDoc as a posted invoice.**
  It read `DOCNUM` from the receipt and reported `posted` without looking at
  `STATUS`, so an invoice SAP declined to post was booked as paid-ready - and
  its number was added to the posted set, which would have made the resend after
  someone opened the posting period look like a duplicate. It now requires
  status `53`, and reports `not posted` with the status record's message
  otherwise. Found by the feature above, which is the whole argument for it.

## [0.9.2] - 2026-09-25

No change to the package itself. The release carries a second worked example,
`invoice_check`, which drives mock-sap and mock-edi together, and points the
PyPI Homepage link at the projects page that shows both mocks side by side.

### Added

- **An example integration, tested against mock-sap and mock-edi.**
  `examples/invoice_check.py` verifies a supplier's EDI invoices before posting
  them into SAP as `INVOIC` IDocs: prices against the purchase order, quantities
  against the supplier's ship notice, the total against its lines, and the
  invoice number against what has already been posted.
  `examples/test_invoice_check.py` runs it against
  [mock-edi](https://github.com/rseufert/mock-edi) as the supplier: a clean
  invoice, a short shipment billed as shipped, a price the supplier disagrees
  with, and the same invoice sent twice. CI runs it with the other documented
  examples.

### Changed

- The package's **Homepage** link on PyPI now points to
  [rickseufert.com](https://rickseufert.com/#projects), which shows this mock,
  its counterpart and the worked examples together. **Repository** still
  points to GitHub.

## [0.9.1] - 2026-09-11

Tooling only: the published package is unchanged from 0.9.0.

### Added

- `tools/check_changelog.py`, run by CI beside the docs check ([#36], [#37]).
  Two pull requests that each add a bullet under `## [Unreleased]` conflict on
  the same lines, and resolving that by hand is one keystroke from keeping one
  side and dropping the other - which is how the entry for the V2 annotation
  document went missing between 0.8.0 and 0.9.0 and came back by luck. The
  check holds released sections to being history, refuses to let an entry
  waiting for a release disappear, and asks a change to `mocksap/` to bring an
  entry with it. An *entry*, not merely a changed file: the merge that lost one
  did touch the changelog, adding a link reference while dropping the prose. A
  pull request labelled `no changelog` lifts that last rule - the right answer
  for a comment, a rename or a pure refactor - and leaves the other two
  standing. It also checks what a release is most likely to forget: that the
  `[Unreleased]` compare link names the newest version and that
  `pyproject.toml` agrees with it.

## [0.9.0] - 2026-09-11

The documents a sales order turns into, and the annotation document a V2 app
fetches.

### Added

- Deliveries, billing documents and journal entries ([#27]) - the documents a
  sales order turns into. `BAPI_OUTB_DELIVERY_CREATE_SLS` and
  `BAPI_ACC_DOCUMENT_POST` create them, and so do the `DELVRY07` and `INVOIC02`
  generators, which previously invented a number with nothing behind it. Three
  new OData services serve them, and an unbalanced accounting document is refused
  the way SAP refuses one.
- A V2 annotation document ([#29]), served at `<service>/annotations` and linked
  from the service document, carrying the same UI intent the V4 services render
  inside `$metadata`. `GWSAMPLE_BASIC` publishes one; the A2X APIs do not, as
  SAP's own do not.

### Fixed

- A key the server assigns is declared rather than named. `SalesOrderItem` and
  `PurchaseOrderItem` were filled in by a check against those two literal names,
  so the pricing elements added in 0.8.0 asked the client for a
  `PricingProcedureStep` that SAP hands out itself, and a complete
  `A_SalesOrder` deep insert was refused. Such keys now carry
  `creatable=False`, and any missing one is numbered within the keys the client
  did send - so two items' pricing elements are numbered independently. A key
  the client does owe, like a text's `LongTextID`, is still required.

## [0.8.0] - 2026-09-11

An object page with sections, and the rest of the shapes a client posting a
sales order sends.

### Added

- `UI.FieldGroup` and `UI.Facets` on the V4 services ([#28]), so an object page
  has sections and an items table rather than a flat list of fields. A facet can
  point through a navigation property at the item type's own line items, and the
  types those facets reference - addresses, roles, descriptions, plants, purchase
  order items, sales order partners - gained line items of their own.
- This changelog, and `CONTRIBUTING.md`: what the project values, where to add
  each kind of thing, what a pull request should carry, and how a release is cut.
- The rest of the sales-order write shapes: `A_SalesOrderPartnerAddress` (the
  one-time address a partner carries, reached through `to_Address`),
  `A_SalesOrderText`, and header and item pricing elements
  (`A_SalesOrderHeaderPrElement`, `A_SalesOrderItemPrElement`), with the header
  and item fields an order-management client sends alongside them —
  `CustomerPurchaseOrderDate`, `SDDocumentReason`, `ReferenceSDDocument`,
  `PricingDate` and the item's `PurchaseOrderByCustomer`. A client that posts a
  complete `A_SalesOrder` deep insert is no longer refused for properties the
  real service has.

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

[#91]: https://github.com/rseufert/mock-sap/issues/91
[#105]: https://github.com/rseufert/mock-sap/issues/105
[#116]: https://github.com/rseufert/mock-sap/issues/116
[#106]: https://github.com/rseufert/mock-sap/issues/106
[#107]: https://github.com/rseufert/mock-sap/issues/107
[#127]: https://github.com/rseufert/mock-sap/issues/127
[#86]: https://github.com/rseufert/mock-sap/issues/86
[#87]: https://github.com/rseufert/mock-sap/issues/87
[#88]: https://github.com/rseufert/mock-sap/issues/88
[#129]: https://github.com/rseufert/mock-sap/issues/129
[#132]: https://github.com/rseufert/mock-sap/issues/132
[#137]: https://github.com/rseufert/mock-sap/issues/137
[#89]: https://github.com/rseufert/mock-sap/issues/89
[#90]: https://github.com/rseufert/mock-sap/issues/90
[#78]: https://github.com/rseufert/mock-sap/issues/78
[#92]: https://github.com/rseufert/mock-sap/issues/92
[#93]: https://github.com/rseufert/mock-sap/issues/93
[#94]: https://github.com/rseufert/mock-sap/issues/94
[#95]: https://github.com/rseufert/mock-sap/issues/95
[#96]: https://github.com/rseufert/mock-sap/issues/96
[#97]: https://github.com/rseufert/mock-sap/issues/97
[#98]: https://github.com/rseufert/mock-sap/issues/98
[#101]: https://github.com/rseufert/mock-sap/issues/101
[#151]: https://github.com/rseufert/mock-sap/issues/151
[#154]: https://github.com/rseufert/mock-sap/issues/154
[#158]: https://github.com/rseufert/mock-sap/issues/158
[#160]: https://github.com/rseufert/mock-sap/issues/160
[#163]: https://github.com/rseufert/mock-sap/issues/163
[#165]: https://github.com/rseufert/mock-sap/issues/165
[#167]: https://github.com/rseufert/mock-sap/issues/167
[#170]: https://github.com/rseufert/mock-sap/issues/170
[Unreleased]: https://github.com/rseufert/mock-sap/compare/v0.20.0...HEAD
[0.20.0]: https://github.com/rseufert/mock-sap/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/rseufert/mock-sap/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/rseufert/mock-sap/compare/v0.17.1...v0.18.0
[0.17.1]: https://github.com/rseufert/mock-sap/compare/v0.17.0...v0.17.1
[0.17.0]: https://github.com/rseufert/mock-sap/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/rseufert/mock-sap/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/rseufert/mock-sap/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/rseufert/mock-sap/compare/v0.13.3...v0.14.0
[0.13.3]: https://github.com/rseufert/mock-sap/compare/v0.13.2...v0.13.3
[0.13.2]: https://github.com/rseufert/mock-sap/compare/v0.13.1...v0.13.2
[0.13.1]: https://github.com/rseufert/mock-sap/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/rseufert/mock-sap/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/rseufert/mock-sap/compare/v0.11.2...v0.12.0
[0.11.2]: https://github.com/rseufert/mock-sap/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/rseufert/mock-sap/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/rseufert/mock-sap/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/rseufert/mock-sap/compare/v0.9.2...v0.10.0
[0.9.2]: https://github.com/rseufert/mock-sap/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/rseufert/mock-sap/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/rseufert/mock-sap/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/rseufert/mock-sap/compare/v0.7.0...v0.8.0
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
[#27]: https://github.com/rseufert/mock-sap/issues/27
[#28]: https://github.com/rseufert/mock-sap/issues/28
[#29]: https://github.com/rseufert/mock-sap/issues/29
[#36]: https://github.com/rseufert/mock-sap/pull/36
[#37]: https://github.com/rseufert/mock-sap/pull/37
[#45]: https://github.com/rseufert/mock-sap/issues/45
[#46]: https://github.com/rseufert/mock-sap/issues/46
[#49]: https://github.com/rseufert/mock-sap/issues/49
[#51]: https://github.com/rseufert/mock-sap/issues/51
[#54]: https://github.com/rseufert/mock-sap/issues/54
[#55]: https://github.com/rseufert/mock-sap/issues/55
[#56]: https://github.com/rseufert/mock-sap/issues/56
[#57]: https://github.com/rseufert/mock-sap/issues/57
[#62]: https://github.com/rseufert/mock-sap/issues/62
