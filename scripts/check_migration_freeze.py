#!/usr/bin/env python3
"""Freeze a migration's executable content once it has run somewhere.

A migration that has already been applied to a database is never replayed there. Editing the file
therefore changes what FRESH installs get and nothing else — the machine it was developed against
keeps the old behaviour for ever, which is how 0070's withdrawn backfill stayed on the maintainer's
server and how 0082 shipped without the column it had gained. See
`.claude/docs/migration-ci-design.md`.

The fingerprint is the parsed AST with docstrings stripped, so docstrings, comments and
`ruff format` never trip this — only executable content does.

    python scripts/check_migration_freeze.py                    # check (CI, pre-commit)
    python scripts/check_migration_freeze.py --write            # stamp NEW migrations; never edits one
    python scripts/check_migration_freeze.py --amend 0063 --reason "..."
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = REPO_ROOT / "shortlist" / "server" / "db" / "alembic" / "versions"
MANIFEST = VERSIONS_DIR.parent / "frozen_migrations.txt"

#: Counted over LETTERS, not characters: "..............................." is 31 characters and
#: explains nothing, and padding is the obvious way past a length gate.
MIN_REASON_LETTERS = 30
PLACEHOLDER_REASONS = frozenset({"fix", "wip", "n/a", "na", "todo", "tbd", "-", "."})

#: Below this many frozen migrations, "every line mismatches" is not evidence of anything.
SYSTEMIC_MISMATCH_FLOOR = 5

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*["']([^"']+)["']""", re.M)
_AMENDED_RE = re.compile(r"^amended:\d{4}-\d{2}-\d{2}$")
_DOCSTRINGABLE = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

HEADER = """\
# Executable-content fingerprints for every Alembic migration. Generated and checked by
# scripts/check_migration_freeze.py; see .claude/docs/migration-ci-design.md.
#
# A migration already applied to a database is never replayed there, so editing one changes fresh
# installs ONLY. A fingerprint change is therefore a question that has to be answered in writing:
# why is every database already stamped at this revision fine without the change?
#
#   <revision>  <fingerprint>  frozen | amended:<date>  [why the stamped installs are fine]
#
# Docstrings, comments and formatting are excluded from the fingerprint — edit those freely.
"""


def _strip_docstrings(tree: ast.Module) -> ast.Module:
    """Remove every module/function/class docstring, in place."""
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRINGABLE):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


def _sort_import_runs(tree: ast.Module) -> ast.Module:
    """Sort each maximal run of consecutive module-level imports.

    `ruff check --fix` re-sorts imports (rule `I`), and the order of adjacent imports has no meaning
    in a migration — these files import only `json`, `sqlalchemy` and `alembic`. Without this a
    repo-wide isort sweep would fire on every migration at once, which is the noise that gets a
    check switched off.
    """
    body: list[ast.stmt] = []
    run: list[ast.stmt] = []
    for statement in [*tree.body, None]:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            run.append(statement)
            continue
        body.extend(sorted(run, key=_canonical))
        run = []
        if statement is not None:
            body.append(statement)
    tree.body = body
    return tree


def _canonical(node: object) -> str:
    """A version-stable canonical string for an AST node.

    `ast.dump` is NOT stable across Python minors — 3.9 and 3.14 disagree on every file in this
    repo, because later versions add `_fields` entries that render as empty values. Skipping empty
    fields makes the two agree, which matters because the fingerprint has to mean the same thing on
    the maintainer's laptop as it does on CI's pinned 3.12.
    """
    if isinstance(node, ast.AST):
        parts = []
        for field in node._fields:
            value = getattr(node, field, None)
            if value is None or value == [] or value == ():
                continue
            parts.append(f"{field}={_canonical(value)}")
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, list):
        return "[" + ",".join(_canonical(item) for item in node) + "]"
    return repr(node)


def fingerprint(source: str) -> str:
    """The SHA-256 of a migration's executable content, ignoring docstrings, comments and layout.

    Args:
        source: The full text of a migration module.

    Returns:
        A 64-character lowercase hex digest.

    Raises:
        SyntaxError: The source does not parse.
    """
    tree = _sort_import_runs(_strip_docstrings(ast.parse(source)))
    return hashlib.sha256(_canonical(tree).encode()).hexdigest()


def revision_of(source: str, fallback: str) -> str:
    """The `revision = "…"` a migration declares, or `fallback` when it declares none."""
    match = _REVISION_RE.search(source)
    return match.group(1) if match else fallback


def migrations() -> dict[str, tuple[Path, str]]:
    """Every migration on disk, as `{revision: (path, fingerprint)}`."""
    found: dict[str, tuple[Path, str]] = {}
    for path in sorted(VERSIONS_DIR.glob("[0-9]*.py")):
        source = path.read_text(encoding="utf-8")
        revision = revision_of(source, path.stem[:4])
        # Last-file-wins would leave the EARLIER file entirely unchecked — the same badly-resolved
        # merge `read_manifest` already refuses, one layer down, where it is easier to miss because
        # the check still reports success for everything it did look at.
        if revision in found:
            sys.exit(
                f"two migration files declare revision {revision}: {found[revision][0].name} and "
                f"{path.name}. Resolve the duplicate before this check can mean anything."
            )
        found[revision] = (path, fingerprint(source))
    return found


def read_manifest() -> dict[str, tuple[str, str, str]]:
    """The manifest, as `{revision: (fingerprint, status, note)}`."""
    entries: dict[str, tuple[str, str, str]] = {}
    if not MANIFEST.exists():
        return entries
    for number, line in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            sys.exit(f"{MANIFEST.name}:{number}: expected '<revision>  <fingerprint>  <status>  [note]'")
        revision, digest, status = parts[:3]
        if status != "frozen" and not _AMENDED_RE.match(status):
            sys.exit(f"{MANIFEST.name}:{number}: status must be 'frozen' or 'amended:YYYY-MM-DD', not {status!r}")
        # A badly resolved merge is the way two lines for one revision get here, and the loser would
        # otherwise vanish silently — taking whichever fingerprint sorted last as the frozen truth.
        if revision in entries:
            sys.exit(f"{MANIFEST.name}:{number}: migration {revision} is listed twice")
        entries[revision] = (digest, status, parts[3] if len(parts) == 4 else "")
    return entries


def write_manifest(entries: dict[str, tuple[str, str, str]]) -> None:
    """Rewrite the manifest, revision order, header intact."""
    lines = [HEADER]
    for revision in sorted(entries):
        digest, status, note = entries[revision]
        lines.append(f"{revision}  {digest}  {status}" + (f"  {note}" if note else ""))
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _missing_line_problem(path: Path) -> str:
    return (
        f"{path.name}: migration is not in {MANIFEST.name}.\n"
        f"  Stamp it now, BEFORE you run it anywhere:\n"
        f"    python scripts/check_migration_freeze.py --write"
    )


def _vanished_problem(revision: str) -> str:
    return (
        f"migration {revision} is in {MANIFEST.name} but no longer exists on disk.\n"
        f"  Deleting an applied migration strands every database stamped at it — alembic can\n"
        f"  no longer resolve the revision and the container will not boot. If the removal is\n"
        f"  genuinely intended, drop its manifest line in the same commit."
    )


def _changed_problem(revision: str, path: Path) -> str:
    return (
        f"{path.name}: the executable content of migration {revision} changed.\n"
        f"  Any database already stamped {revision} will NEVER replay it, so this edit reaches\n"
        f"  fresh installs only — the machine you developed against keeps the old behaviour.\n"
        f"  This is how 0070's withdrawn backfill survived on the live server, and how 0082\n"
        f"  shipped without the column it had gained.\n"
        f"\n"
        f"  Normally the fix is a NEW migration that brings stamped databases forward, guarded\n"
        f"  by inspection so it is a clean no-op on a fresh one (see 0083, 0089).\n"
        f"\n"
        f"  If editing really is right — the revision has provably run nowhere, or only its\n"
        f"  downgrade() changed — record why:\n"
        f"    python scripts/check_migration_freeze.py --amend {revision} \\\n"
        f'        --reason "why every database already stamped {revision} is fine without this"'
    )


def _systemic_problem(count: int) -> str:
    """The one failure that is never what it looks like: every fingerprint at once.

    Nobody edits 62 migrations in one commit, so a total mismatch is the canonicaliser reading the
    tree differently — a Python release that changed the AST, or an edit to `_canonical` itself. The
    aggregate guard exists because the per-migration check cannot tell the two apart: a systemic
    change agrees with itself on every line and reads as 62 independent edits. (Same shape as the
    orphan-collection guard in `.claude/rules/plex-safety.md` §4.)
    """
    return (
        f"every one of the {count} frozen migrations fingerprints differently.\n"
        f"  That is not {count} edits. It means this interpreter canonicalises the AST differently\n"
        f"  from the one that wrote {MANIFEST.name} — a new Python release, or a change to\n"
        f"  _canonical() in this script.\n"
        f"\n"
        f"  Do NOT regenerate the manifest to make this green: that would re-freeze whatever the\n"
        f"  files say today and silently bless any real edit hiding among them. Run the check on\n"
        f"  CI's interpreter (3.12) first — if it passes there, the manifest is fine and this\n"
        f"  script needs to absorb the new AST shape\n"
        f"  (tests/unit/test_migration_freeze.py::TestTheFingerprintSurvivesAPythonUpgrade)."
    )


def check(found: dict[str, tuple[Path, str]], entries: dict[str, tuple[str, str, str]]) -> list[str]:
    """Every disagreement between the tree and the manifest, as ready-to-print problems."""
    problems: list[str] = []
    changed: list[str] = []
    shared = 0
    for revision in sorted(set(found) | set(entries)):
        if revision not in entries:
            problems.append(_missing_line_problem(found[revision][0]))
        elif revision not in found:
            problems.append(_vanished_problem(revision))
        else:
            shared += 1
            if entries[revision][0] != found[revision][1]:
                changed.append(revision)

    if shared >= SYSTEMIC_MISMATCH_FLOOR and len(changed) == shared:
        return [*problems, _systemic_problem(shared)]
    return problems + [_changed_problem(revision, found[revision][0]) for revision in changed]


def _amend(
    revision: str, reason: str, found: dict[str, tuple[Path, str]], entries: dict[str, tuple[str, str, str]]
) -> int:
    """Re-freeze one deliberately edited migration. Returns a process exit status."""
    if revision not in found:
        print(f"no migration {revision} on disk", file=sys.stderr)
        return 2
    # A never-frozen migration has no stamped installs to reason about, and an `amended:` line would
    # claim a decision nobody made. It is a --write, and --write is the command that says so.
    if revision not in entries:
        print(f"migration {revision} was never frozen — stamp it with --write, not --amend", file=sys.stderr)
        return 2
    if entries[revision][0] == found[revision][1]:
        print(f"migration {revision} matches the manifest — nothing to amend", file=sys.stderr)
        return 2
    reason = " ".join(reason.split())
    letters = sum(character.isalpha() for character in reason)
    if letters < MIN_REASON_LETTERS or reason.lower().rstrip(".") in PLACEHOLDER_REASONS:
        print(
            f"--amend needs a --reason of at least {MIN_REASON_LETTERS} letters (this one has "
            f"{letters}) saying why every database already stamped {revision} is fine without this "
            f"change.",
            file=sys.stderr,
        )
        return 2
    entries[revision] = (found[revision][1], f"amended:{date.today().isoformat()}", reason)
    write_manifest(entries)
    print(f"amended {revision}: {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--write", action="store_true", help="add lines for NEW migrations; never changes an existing one"
    )
    parser.add_argument("--amend", metavar="REVISION", help="deliberately re-freeze an edited migration")
    parser.add_argument("--reason", default="", help="why databases already stamped at that revision are fine")
    args = parser.parse_args(argv)

    found = migrations()
    entries = read_manifest()

    if args.amend:
        return _amend(args.amend, args.reason, found, entries)

    if args.write:
        # ONLY revisions the manifest has never seen. This subtraction is what stops the routine
        # "stamp my new migration" command from doubling as a laundry: regenerating every line
        # instead would re-freeze whatever the tree says today and silently bless an edit.
        added = sorted(set(found) - set(entries))
        for revision in added:
            entries[revision] = (found[revision][1], "frozen", "")
        if added:
            write_manifest(entries)
            print(f"froze {len(added)} new migration(s): {', '.join(added)}")
        else:
            print("no new migrations to freeze")

    # Re-read from disk rather than reuse `entries`: --write is graded on the file it produced, not
    # on the in-memory copy it built, so a divergence between the two shows up here as a failure.
    problems = check(found, read_manifest())
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} migration freeze problem(s)", file=sys.stderr)
        return 1
    print(f"{len(found)} migrations match {MANIFEST.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
