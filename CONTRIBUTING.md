# Contributing

Thanks for looking. mock-sap is a black box that speaks SAP's wire shapes so
that integrations can be built and tested without an SAP system. Everything
below follows from that one purpose.

## What the project values

These are not style preferences; they decide what gets merged.

**Fidelity over convenience.** If SAP behaves a certain way, the mock behaves
that way - even when the real behaviour is inconvenient. `BAPI_SALESORDER_CHANGE`
ignores fields the X structure does not flag, and a call that forgets the X
structure changes nothing, because that is what a real BAPI does. A mock that
accepts what SAP rejects teaches a client a lie it will discover in production.

**Say when you are guessing.** Where a detail cannot be verified, the code says
so rather than implying authority. The `sap-message` classes and numbers are
plausible rather than authentic, and both the module and the README state it.
Inventing an entity set, a field or a behaviour that SAP does not have costs more
than leaving a gap.

**No dependencies.** The Python standard library and SQLite, nothing else. A mock
you cannot install in a locked-down CI image is a mock nobody runs. This is not
negotiable, and it is why there is a hand-written `$filter` parser rather than a
library.

**Declare, do not hand-write.** Entity sets, complex types, services and UI
annotations are declarations in `mocksap/schema.py`; tables, `$metadata`, payload
shapes and key handling are derived from them. If you find yourself writing the
same shape in two places, the declaration is missing.

**Refuse rather than half-implement.** An unsupported `$filter` function, an
unknown `$apply` transformation and a table `RFC_READ_TABLE` does not know all
produce an error that names what *is* supported. Silently ignoring an option is
the one thing worse than not having it.

**The wire is the product.** Behaviour a client cannot observe does not need to
exist; behaviour it can observe needs to be right. Document numbers come from
number ranges and items are numbered 10, 20, 30 because a client sees those. No
ATP check runs, because no client can tell.

## Getting set up

Nothing to install:

```bash
git clone https://github.com/rseufert/mock-sap
cd mock-sap
python3 -m mocksap --port 8000        # it is already runnable
python3 -m unittest discover -s tests -v
```

Python 3.8 or newer. CI runs the suite on 3.8 through 3.13 across Linux, macOS
and Windows, so avoid syntax newer than 3.8.

Useful while working:

```bash
python3 tests/test_batch.py           # one surface
python3 tools/check_docs.py           # the docs coverage check CI runs
python3 tools/check_changelog.py      # and the changelog check
bash examples/demo.sh                 # the curl tour, against a running mock
```

## Where things live

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains how a request flows and
why the layering is what it is; [`docs/FILES.md`](docs/FILES.md) describes every
file. Read the first before a change of any size.

| To add | Edit | Notes |
| --- | --- | --- |
| An entity set | `mocksap/schema.py`, then seed rows in `mocksap/db.py` | Tables, `$metadata`, payload shapes and keys follow automatically |
| A service | `mocksap/schema.py` | `version=4` for OData V4; entity types can be shared between services |
| A `$filter` function | `mocksap/odata.py`, `_Filter.parse_function` | Return a `_Frag`; use `combine()` so a repeated argument binds twice |
| A function module | `mocksap/bapi.py` | `@function(NAME, CamelAlias)` makes it reachable over JSON *and* SOAP |
| An IDoc type | `mocksap/idoc.py` | Add a generator and register it in `GENERATORS` |
| A failure scenario | `mocksap/server.py`, `SCENARIOS` | Add the description too: `/_mock/services` serves the list |
| A UI annotation | `mocksap/schema.py`, the `UI` on the entity type | Rendered into CSDL XML and JSON from the one declaration |

## What a good pull request looks like

- **A test that goes over HTTP.** Every test in `tests/` drives a real mock on a
  real socket; nothing is stubbed. Put it in the module for the surface you
  touched, or add one and give it a row in `docs/FILES.md`.
- **Assertions that could fail.** Check a total against the rows it summarises,
  not against itself. Two of the nastiest bugs in this project's history - a
  delta read that dropped deletions, a `$count` that counted the page instead of
  the groups - returned plausible answers and were caught only by comparing
  against something independently derived.
- **Documentation that keeps up.** `tools/check_docs.py` fails the build if a
  tracked file has no row in `docs/FILES.md`, if a row names a file that is gone,
  or if a module is missing from the README's layout block. It checks coverage,
  not prose - keeping the prose true is on you.
- **A line in the changelog.** `tools/check_changelog.py` fails a pull request
  that touches `mocksap/` without adding an entry under `## [Unreleased]` - an
  entry, not merely a changed file, because the merge that lost one still
  touched the changelog. It is what a user of the published package reads. The
  same check holds released sections to being history and refuses to let an
  entry waiting for a release disappear. A change that genuinely needs no entry
  - a comment, a rename, a pure refactor - can carry the `no changelog` label,
  which lifts that one rule and leaves the others standing.
- **No new dependencies.** See above.
- **A commit message that says what changed and why.** The why is the part a
  reader cannot reconstruct. Wrap at 72 characters.

Small, focused pull requests are easier to take than large ones. If you are
unsure whether something fits, open an issue first and say what you are trying to
test against the mock - that is usually the fastest way to the right shape.

## Reporting a missing or wrong shape

The most useful bug report contains the payload a real SAP system returned, with
anything sensitive removed, beside what the mock returned. Field names, the exact
envelope and the status code all matter. If you cannot share a payload, the
service and the OData version plus a description of the difference is still
plenty to work with.

## Releasing (maintainers)

`pyproject.toml` is the only place the version is written; `mocksap.__version__`
reads it back from the installed package metadata.

```bash
# bump `version` in pyproject.toml, commit, then:
git tag v0.8.0 && git push origin v0.8.0
gh release create v0.8.0 --generate-notes     # or write the notes by hand
```

Publishing the GitHub Release runs the tests, builds the distributions, checks
that the tag, `pyproject.toml` and the built wheel agree, and uploads to PyPI
through [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) - there is
no API token anywhere. Running the `Publish` workflow by hand publishes to
TestPyPI instead. Add the release to [`CHANGELOG.md`](CHANGELOG.md) in the same
commit as the version bump.

One practical note: PyPI's index propagates per edge node, so an install
immediately after a release can still fetch the previous version. Pin the exact
version when verifying (`pip install mock-sap==0.8.0`) rather than trusting a
plain upgrade.

## Licence

By contributing you agree that your work is licensed under the
[MIT Licence](LICENSE), like the rest of the project.
