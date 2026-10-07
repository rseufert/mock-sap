"""`tools/check_changelog.py`'s release table: who installs this, and whether
this release was run against them.

The table exists because the fact it holds was already being written down and
still reached nobody. 0.20.0's release notes worked out that mock-acme's floor
would take the release the day it published, that its last green nightly ran
eight hours before the cent-level change merged, and that several of its tests
assert exact `Decimal` amounts - in prose, in the second-to-last section of an
85-line document, which the maintainer it was about had no reason to read
(#189).

So `--release` writes the answer as a placeholder the checks refuse, the same
way it already does for the paragraph saying why anyone should upgrade. A
release that was run against nothing and a release nobody thought about read
identically otherwise, and `no` is an answer: having to type it is the point.

The sections already released have no table and must keep passing, so these
assertions run against the real `CHANGELOG.md` rather than a fixture - if the
check ever starts demanding a table, it fails here first, on history.
"""
from __future__ import annotations

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool():
    """`check_changelog.py` as a module.

    It is a script in `tools/`, not part of the package, so it is loaded by
    path rather than imported.
    """
    path = os.path.join(ROOT, "tools", "check_changelog.py")
    spec = importlib.util.spec_from_file_location("check_changelog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class TestTheTableARelease(unittest.TestCase):
    """What `--release` writes, and where the rows come from."""

    def setUp(self):
        self.tool = _tool()

    def section(self):
        """The assembled section for a release with one entry waiting."""
        out = self.tool.assemble(
            _read("CHANGELOG.md"), "0.99.0",
            {"999.added.md": "- **A thing** ([#999]).\n"},
            today="2026-10-08")
        start = out.index("## [0.99.0]")
        return out[start:out.index("## [", start + 1)]

    def test_a_row_for_every_consumer_and_a_placeholder_in_each(self):
        section = self.section()
        self.assertIn("| Consumer | How it takes a release "
                      "| Run against this release |", section)
        for name, takes in self.tool.CONSUMERS:
            self.assertIn("| %s | %s | %s |"
                          % (name, takes, self.tool.RAN_TODO), section)
        self.assertEqual(section.count(self.tool.RAN_TODO),
                         len(self.tool.CONSUMERS))

    def test_the_table_comes_before_the_paragraph_nobody_can_write(self):
        section = self.section()
        self.assertLess(section.index(self.tool.RAN_TODO),
                        section.index(self.tool.INTRO_TODO),
                        "the table is the part a downstream reader is after")

    def test_the_rows_are_the_declaration_and_nothing_else(self):
        """One place to add a consumer. Change the declaration and the table
        changes with it; nothing is spelled out twice."""
        self.tool.CONSUMERS = (("mock-nothing", "does not exist"),)
        self.assertEqual(
            self.tool.consumer_table(),
            "| Consumer | How it takes a release | Run against this release |"
            "\n| --- | --- | --- |"
            "\n| mock-nothing | does not exist | %s |" % self.tool.RAN_TODO)

    def test_the_entries_still_land_under_their_heading(self):
        """The table is inserted ahead of the entries, not instead of them."""
        section = self.section()
        self.assertIn("### Added", section)
        self.assertIn("- **A thing** ([#999]).", section)
        self.assertLess(section.index("| mock-acme |"),
                        section.index("### Added"))


class TestTheCheckOnThatTable(unittest.TestCase):
    """The placeholder fails the check, an answer passes it, and history -
    which has no table at all - is left alone."""

    def setUp(self):
        self.tool = _tool()
        self.text = _read("CHANGELOG.md")
        self.pyproject = _read("pyproject.toml")

    def problems(self, text: str):
        return self.tool.check_structure(text, self.pyproject)

    def with_table(self, answer: str) -> str:
        """The newest release section, given a table whose answers are
        `answer`."""
        table = self.tool.consumer_table().replace(self.tool.RAN_TODO, answer)
        heading = "## [0.21.0] - 2026-10-07\n"
        self.assertIn(heading, self.text)
        return self.text.replace(heading, heading + "\n" + table + "\n")

    def test_a_released_section_with_no_table_passes(self):
        self.assertEqual(self.problems(self.text), [])

    def test_the_placeholder_is_refused_and_named(self):
        problems = self.problems(self.with_table(self.tool.RAN_TODO))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(self.tool.RAN_TODO, problems[0])

    def test_an_answered_table_passes(self):
        self.assertEqual(self.problems(self.with_table("no")), [])

    def test_no_is_an_answer_and_so_is_a_date(self):
        for answer in ("no", "not yet", "2026-10-07, green"):
            self.assertEqual(self.problems(self.with_table(answer)), [],
                             answer)

    def test_one_consumer_left_unanswered_is_still_refused(self):
        """A table half filled in is the case worth catching: the person was
        there, answered the row they knew, and left the other."""
        table = self.tool.consumer_table()
        first = "| %s |" % self.tool.CONSUMERS[0][0]
        rows = table.split("\n")
        rows = [row.replace(self.tool.RAN_TODO, "no")
                if row.startswith(first) else row for row in rows]
        heading = "## [0.21.0] - 2026-10-07\n"
        text = self.text.replace(heading, heading + "\n" + "\n".join(rows) + "\n")
        problems = self.problems(text)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(self.tool.RAN_TODO, problems[0])


if __name__ == "__main__":
    unittest.main()
