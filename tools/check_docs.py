#!/usr/bin/env python3
"""Guard docs/FILES.md against drift.

This checks *coverage*, not prose: it cannot tell whether a description is
still true, only whether a file exists that nobody documented, or a file is
documented that no longer exists.  That catches the common failure - a module
added without a line in the index - and leaves the judgement calls to review.

Three checks:

1. every tracked file is named in docs/FILES.md
2. every file named in docs/FILES.md exists
3. every module of the package appears in the README's layout block

And two that are not about documentation, but about a documented command -
`python3 tests/test_batch.py`, CONTRIBUTING.md's way to run one surface - doing
what it says (#155). They live here because this already walks every tracked
file and CI already runs it:

4. nothing follows a module's `if __name__ == "__main__":` block. A class below
   `unittest.main()` does not exist yet when it fires, so its tests never run
   and the run still says OK
5. a test module imports `support` before `mocksap`, because `support` is what
   puts the repository on the path; the other way round, the module only runs
   under `discover`

Run it directly (`python3 tools/check_docs.py`); CI runs it on every push.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join("docs", "FILES.md")
README = "README.md"

# Files that are their own documentation, or carry nothing worth describing.
EXEMPT = {".gitignore"}

# Directories whose contents are transient rather than part of the repository's
# furniture. `changelog.d/` holds one file per entry waiting for a release, and
# they come and go with every pull request: a row each would put this index in
# the way of exactly the pull requests the directory freed from CHANGELOG.md,
# and move the conflict here instead of removing it. Its README is documented.
EXEMPT_DIRECTORIES = ("changelog.d/",)

# Tokens in the index that look like a path and are therefore checked to exist.
PATH_RE = re.compile(r"`([\w./-]+\.(?:py|md|yml|yaml|toml|in|sh|cfg))`")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                         stdout=subprocess.PIPE).stdout.decode()
    return sorted(line for line in out.splitlines() if line)


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def _is_main_guard(node):
    """Whether a top-level statement is `if __name__ == "__main__":`."""
    return (isinstance(node, ast.If)
            and any(isinstance(part, ast.Name) and part.id == "__name__"
                    for part in ast.walk(node.test)))


def _imports(node):
    """The top-level package names an import statement brings in."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        return [node.module.split(".")[0]]
    return []


def runs_as_a_script(path, source):
    """What stops `python3 <path>` from running everything the file defines."""
    try:
        body = ast.parse(source).body
    except SyntaxError as exc:
        return ["%s does not parse: %s" % (path, exc)]
    problems = []

    for position, node in enumerate(body):
        if _is_main_guard(node) and position < len(body) - 1:
            after = body[position + 1]
            problems.append(
                "%s:%d has an `if __name__` block with code after it (line %d) - "
                "run as a script, that code does not exist yet when the block "
                "fires; move the block to the end of the file"
                % (path, node.lineno, after.lineno))
            break

    if path.startswith("tests/"):
        seen = [name for node in body for name in _imports(node)]
        if ("mocksap" in seen and "support" in seen
                and seen.index("mocksap") < seen.index("support")):
            problems.append(
                "%s imports `mocksap` before `support` - `support` is what puts "
                "the repository on the path, so run as a script this fails; "
                "import `support` first" % path)
    return problems


def main():
    index = read(INDEX)
    readme = read(README)
    problems = []

    # 1. undocumented files
    for path in tracked_files():
        name = os.path.basename(path)
        if name in EXEMPT or path == INDEX:
            continue
        if path.startswith(EXEMPT_DIRECTORIES) and name != "README.md":
            continue
        if ("`%s`" % path) not in index and ("`%s`" % name) not in index:
            problems.append(
                "%s is not documented in %s - add a row describing it" % (path, INDEX))

    # 2. documented files that no longer exist
    existing = set(tracked_files())
    basenames = {os.path.basename(p) for p in existing}
    for token in sorted(set(PATH_RE.findall(index))):
        if token in existing or token in basenames:
            continue
        if token.startswith(EXEMPT_DIRECTORIES):
            continue
        problems.append(
            "%s mentions `%s`, which no longer exists - update or remove the row"
            % (INDEX, token))

    # 3. the README layout block must list every module of the package
    layout = re.search(r"## Layout\n+```\n(.*?)```", readme, re.S)
    if layout is None:
        problems.append("could not find the layout block in %s" % README)
    else:
        for path in existing:
            if path.startswith("mocksap/") and path.endswith(".py"):
                base = os.path.basename(path)
                if base.startswith("__"):
                    continue  # dunder modules are not part of the map
                if path not in layout.group(1):
                    problems.append(
                        "%s is missing from the layout block in %s" % (path, README))

    # 4 and 5. a module run as a script runs all of itself
    for path in existing:
        if path.endswith(".py") and os.path.exists(os.path.join(ROOT, path)):
            problems.extend(runs_as_a_script(path, read(path)))

    if problems:
        print("out of date:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    print("docs/FILES.md covers every tracked file, names nothing that is gone, "
          "the README layout block lists every module, and every module "
          "runs whole as a script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
