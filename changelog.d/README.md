# One file per changelog entry

An entry waiting for a release lives here, not under `## [Unreleased]` in
`CHANGELOG.md`. Two pull requests that each add an entry then add two files and
cannot conflict — where before they conflicted on the same line every time, and
a pull request that conflicts with its base runs no CI at all, so whatever it
said about being green was true of an older `main`.

This project had the failure that argument predicts: the entry for the V2
annotation document went missing between 0.8.0 and 0.9.0 and came back by luck.
The check that followed could tell an entry had been lost but not stop two pull
requests from fighting over the same lines, which is what this directory does.
mock-edi and mock-bank adopted it first; this is it coming home.

## Writing one

    changelog.d/<number>.<kind>.md

`<number>` is the issue the entry is about, or the pull request when there is no
issue. `<kind>` is `added`, `changed` or `fixed`, and decides which heading the
entry lands under.

The file holds the entry **exactly as it would appear under that heading** —
the leading `- `, and two spaces of indent on every line after the first:

```markdown
- **A note-to-payee substring no longer beats a structured reference** ([#86]).
  A line that names `INV-10` in its structured reference is no longer matched
  to `INV-1` because the shorter number appears inside the longer one.
```

Written that way, assembling a release is concatenation, so the section reads as
though a person had written it in one go. Anything that reformats could drift.

More than one bullet in a file is allowed: a change with two faces should not
need two files.

The reference — `[#86]` — is resolved from `CHANGELOG.md`'s link block at the
foot of the file. **Do not add the line yourself.** The release writes a
definition for every reference the new section uses, and the changelog check
fails on one that has none, so a missing link is caught without anyone editing
the foot of `CHANGELOG.md` — which is the shared line range this directory
exists to keep people out of.

That rule is mock-edi's lesson rather than ours: asking authors to add the
definition by hand brought back exactly the conflicts `changelog.d/` had just
ended, so nobody followed it and eight references rendered as literal text in a
released section. This tool arrived here with the fix already in it.

## Cutting a release

    python3 tools/check_changelog.py --release 0.15.0

writes the dated section into `CHANGELOG.md` with `### Added`, `### Changed` and
`### Fixed` in that order, adds the version's link reference, re-points
`[Unreleased]`, and deletes the files here.

It does not write the paragraph of prose a release section opens with, because
no tool can: that is the part saying why anyone should upgrade. The release pull
request is this command's output plus that paragraph.
