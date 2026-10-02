# Security

## What this is, which decides what counts

`mock-sap` is a **test double**. It exists so that code which talks to SAP can
be exercised without SAP, and it is meant to run on a developer's machine or in
CI, against data somebody made up. It is not a hardened service and is not
built to be one.

That is not a disclaimer to get out of fixing things. It is what tells a report
from a non-report, so this file says plainly which is which.

It binds to `127.0.0.1` by default. `--auth` and `--oauth` exist so a client can
exercise its own authentication code path — they are there to be *tested
against*, not to protect the mock. `--db` writes a SQLite file with no
encryption. All of that is deliberate.

## In scope

Something that makes the mock dangerous beyond its remit — where running it as
intended, on a loopback port with invented data, could still harm the machine
it runs on or the project that installed it:

- reading or writing files outside its own database from a crafted request
- executing anything a request supplies, or any other escape from parsing data
  into running code
- a crafted payload that corrupts the host rather than the mock's own state
- anything wrong with the published artifact: the sdist or wheel on PyPI
  containing something the tagged source does not, or the release workflow being
  divertible into publishing from somewhere other than a `v*` tag of this
  repository

The last one is the one we would most want to hear about. There is no stored
PyPI token to revoke — publishing is Trusted Publishing over OIDC — and PyPI
refuses a re-upload of a version it already has, so a bad artifact cannot be
replaced, only yanked.

## Not in scope

These are known, intended, and documented; a report about one of them will be
closed with a pointer here:

- no authentication by default, and weak credentials when it is switched on
- no TLS
- no rate limiting, and a large or deeply nested payload making it slow
- a request reading or changing data another client wrote — there are no tenants
  here, and `POST /_mock/reset` is meant to be reachable
- the mock accepting a document real SAP would reject, or rejecting one real SAP
  would accept. That is a bug, and a valuable one, but it is an issue rather
  than a vulnerability — open it in the tracker.

Exposing the mock to a network you do not control is outside its intended use.
If you have done that, the finding is almost certainly in the "not in scope"
list above.

## Reporting

Use GitHub's private vulnerability reporting: the **Security** tab of this
repository, then **Report a vulnerability**. That keeps the report private until
there is something to say about it, and it does not require you to find an email
address.

Please do not open a public issue for anything in the "in scope" list.

Expect a first reply within a week. This is a small project maintained in
spare time; there is no on-call rotation and no paid support, and saying so is
more useful than promising an hour.

## Which versions get fixed

The latest release, and nothing else. There are no maintenance branches and no
backports: while the major version is `0`, the fix goes into the next release
from `main`. If you are pinned to an older version, the upgrade is the fix.
