# `migration-ci` — freezing a migration's content once it has run

Item: **`migration-ci`** (Wave 7 — Process). Origin: `.claude/docs/audit-2026-09-programme.md`, which
records it as _"CI check that a merged migration's blob hasn't changed since first commit"_ and its
justification as _"The `0032` no-op bug shape recurred in `0082`/`0083` because the fast dev loop runs
against the live database. Nothing prevents a third occurrence except memory."_

Everything below was verified against the repo and its git history on `dev` at `7a189ac`. Where the
audit's framing turned out to be wrong, the correction is stated rather than smoothed over.

---

## 1. The premise, checked — the three are NOT one bug shape

### `0032` — a no-op **when first written**

`shortlist/server/db/alembic/versions/0032_merge_ollama_provider.py` today is a neutered stub. Its
original content (`git show efaf9f9:...0032...`) read a setting like this:

```python
def _get(bind, key: str):
    row = bind.execute(sa.text("select value from settings where key = :k"), {"k": key}).scalar()
    return json.loads(row) if isinstance(row, str) else row
...
if _get(bind, "curator.provider") != "ollama":
    return
```

`SettingsStore.set` wraps every value in a `{"v": <value>}` envelope, so `_get` returned
`{"v": "ollama"}` and the comparison to the bare string `"ollama"` never matched. It returned early on
every real database. Its `_set` had the mirror-image bug (it stored the value _unwrapped_), so fixing
only the reader would have made `row.value["v"]` raise for every setting the app reads.

**This was wrong on the day it was committed.** It was never edited-after-running. It was fixed
forward by `0034`, in commit `1bd41ce` — which also edited `0032` itself, to neuter it.

### `0082` — edited after it had already run, **but before it was ever committed**

`git log` on the exact path returns exactly one commit for each of `0082` and `0083`:

```
4352559 2026-08-25 fix(watching-account): replicate a watch history exactly, not "everything is finished"
```

Both files entered git in the _same_ commit. `0083`'s own docstring says what happened:

> 0082 gained the column after an earlier version of it had already run — against the development
> server, via the rsync/`docker build` loop.

So the edit that made `0082` dangerous happened in the working tree, after the fast dev loop had run
the earlier version against the maintainer's live database, and before any commit existed. A check
comparing a file against **its own first commit** would have seen nothing: by the time `0082` was
first committed it was already in its final form, and `0083` — the correct fix-forward — shipped
alongside it.

### `0083` — not an instance of the bug at all

`0083` is the _remedy_ for `0082`: an idempotent, inspection-guarded `add_column` for the database
that ran the earlier `0082`. It is the pattern this design wants people to reach for.

### What the premise actually is

|                                   | `0032`                  | `0082`                  | `0083` |
| --------------------------------- | ----------------------- | ----------------------- | ------ |
| No-op when first written          | **yes**                 | no                      | no     |
| Edited after it had run somewhere | no                      | **yes**                 | no     |
| Edit visible in git history       | n/a                     | **no** (pre-commit)     | n/a    |
| Handled correctly                 | fixed forward by `0034` | fixed forward by `0083` | —      |

**Two different bugs share one symptom** — "the migration did nothing on the database that mattered".
A content freeze addresses only the second, and would not have caught either `0032` or `0082` as they
actually occurred. That changes what to build: the freeze is worth building anyway (§2 shows it
catches four occurrences the audit never counted), but it must ship with the complementary check in
§6, and the doc must not claim it prevents `0032`.

---

## 2. The occurrences nobody counted — the freeze's real value

The audit names three. History has more. Sweeping every migration's exact-path commit list and
comparing a **semantic** fingerprint (AST with docstrings stripped — §3) of each version:

```
FIRES  0001_initial.py                      (3 commits)  5c44a03 c33a1a1
FIRES  0032_merge_ollama_provider.py        (2 commits)  1bd41ce
quiet  0060_watched_user_rating.py          (2 commits)  -
FIRES  0063_drop_auto_web_search.py         (2 commits)  ea779cc
FIRES  0070_row_fallback_name.py            (3 commits)  f8876b8 29cb475
FIRES  0078_shared_row_watches.py           (2 commits)  6ddc021
FIRES  0081_backfill_shared_pick_media_type.py (2 commits)  77c1283
FIRES  0088_row_show_days.py                (2 commits)  8c5310f
```

Nine content-changing edits to already-committed migrations, across 62 migrations and ~14 months —
roughly one every six weeks. Classified by hand:

| Edit              | What changed                                                             | Paired new migration? | Verdict                          |
| ----------------- | ------------------------------------------------------------------------ | --------------------- | -------------------------------- |
| `0001` @`5c44a03` | added `placement_friends` to the initial table + default-row seed        | `0042`                | correct                          |
| `0001` @`c33a1a1` | seed INSERT stopped naming columns `0065` drops                          | `0065`                | correct                          |
| `0032` @`1bd41ce` | neutered to a no-op                                                      | `0034`                | correct                          |
| `0063` @`ea779cc` | `downgrade()` narrowed from `DELETE … WHERE key` to `… AND value IN (…)` | none                  | safe (downgrade had run nowhere) |
| `0070` @`f8876b8` | backfill logic rewritten                                                 | none                  | **the bug**                      |
| `0070` @`29cb475` | backfill **removed entirely** (75 lines)                                 | none                  | **the bug**                      |
| `0078` @`6ddc021` | `sa.DateTime` → `sa.DateTime(timezone=True)` on two columns              | `0079`, unrelated     | **the bug** (benign)             |
| `0081` @`77c1283` | `upgrade()` gained a muted-collection skip and an int-parse guard        | none                  | **the bug**                      |
| `0088` @`8c5310f` | `shown_state` column add removed                                         | `0089`                | correct                          |

`0070` @`29cb475` is the clearest: the commit message is _"drop 0070's backfill — guessing a name for
the operator was the whole problem"_, 49 minutes after `0070` was first committed and pushed to `dev`.
Any database already stamped `0070` — the maintainer's, via the host build or watchtower — kept the
backfilled names the commit spent 25 lines of message explaining were harmful. The file says one
thing; every database that had already run it says the other.

`0081` @`77c1283` is the same shape with a privacy edge: the added guard exists to avoid minting a
credit against a row a person had muted, and it can never run on a database already stamped `0081`.

**So: four undetected occurrences, five deliberate-and-correct edits.** A check that fires on all nine
would have caught four real bugs and asked five reasonable questions in fourteen months. That is a
good trade, and it is the evidence for building this.

---

## 3. What exactly the check compares

### The three candidates

**(a) `git log` history analysis in CI.** Walk each migration path's commits; fail if any commit after
the introducing one touched it. Rejected, on four verified grounds:

- `actions/checkout@v7` in `.github/workflows/ci.yml` runs with the default `fetch-depth: 1` in all
  five jobs. History analysis needs `fetch-depth: 0` — a real clone cost on every job that runs it,
  paid on every push, to answer a question that does not need git at all.
- History lies about content. `0066`, `0067` and `0068` each appear twice under `git log --follow`
  with **identical tree hashes** (`e54869d`/`2cbf403` both point at tree `def4add`) — a rebase
  duplicated the commit. A commit-counting check calls those three edits; a content check calls them
  what they are, unchanged.
- `git log --follow` also reports `0033` as a two-commit file because it was created by renaming
  another file. Not an edit.
- It cannot run locally in the fast dev loop without the same full clone, so the developer only learns
  about it after pushing — which, per `CLAUDE.local.md`, is _after_ the code has reached the live
  server.

**(b) A test that re-derives the schema.** Cannot detect this bug at all, and §6 shows the repo already
has three of these. A freshly-migrated database is correct in every one of the nine cases above,
including `0082`'s — the whole point is that the damage is confined to databases already stamped.

**(c) A committed manifest of fingerprints.** Recommended.

### Recommendation: a committed manifest of **AST fingerprints**

`shortlist/server/db/alembic/frozen_migrations.txt`, one line per revision:

```
0001  7ff0aa5486a2c6f38d9528aed930319df76539d4cb4adc99eba0f8652364ef90  frozen
0029  54ed9a33fb4bd1f9679760da26a407f0fd841cee92b218b5eba22bf0abedb8a5  frozen
0030  f1b7e9851150402bbc1a2acfbf6b0da78f535d6d91277c7c0207487d6a9278d9  frozen
...
0088  64e0bb8af1603f9349b8c3e8065624f257723611076da5b07c8ae97ecff39134  frozen
0089  50a7aca6aaf2d858c425e52402b7754651495c99114705b59c9d93d1ca72019a  frozen
```

(Real values, generated from the tree at `7a189ac`. 62 lines.)

The manifest sits **outside** `versions/`, so Alembic — which scans that directory — never sees it.

**The fingerprint is not a hash of the file.** It is a hash of a canonical dump of the parsed AST with
every module/function/class docstring removed. That choice is what makes the check usable:

| Change                                             | Raw blob hash | AST fingerprint |
| -------------------------------------------------- | ------------- | --------------- |
| Rewriting the module docstring (`0060` @`3919435`) | fires         | **quiet** ✓     |
| `# comment` added or corrected                     | fires         | **quiet** ✓     |
| `ruff format` re-wrapping a long line              | fires         | **quiet** ✓     |
| Blank lines, trailing whitespace                   | fires         | **quiet** ✓     |
| `sa.DateTime` → `sa.DateTime(timezone=True)`       | fires         | **fires** ✓     |
| Deleting a backfill                                | fires         | **fires** ✓     |

Verified: replaying every historical version of every migration through this fingerprint reproduces
the table in §2 exactly — `0060`'s docstring rewrite is the only one of the twelve multi-commit files
that goes quiet, and it is the only one whose executable content did not change.

### The canonicaliser, and why it isn't `ast.dump`

`ast.dump` is **not stable across Python versions** — measured on this repo's
`0083_snapshot_complete_column.py`:

```
Python 3.9.6 : f93a78961be1a616
Python 3.14.6: 4a70061bc1a508d1
```

A developer on a different minor would see every line of the manifest fail at once. The cause is new
`_fields` entries (`type_params` on `FunctionDef` in 3.12, and so on) rendering as empty values. So
the fingerprint uses a hand-rolled dump that **skips any field whose value is `None`, `[]` or `()`**.
With that one rule, the same three files fingerprint **identically on 3.9.6 and 3.14.6** — a
five-release span, which is far more headroom than this repo needs (CI and the image are both pinned
to 3.12).

### Trade-offs, stated plainly

- The manifest is a file in the repo, so it can be edited in the same commit as the migration. **The
  check does not make the edit impossible; it makes it loud.** A changed fingerprint appears in the
  diff as an `amended:` line with a written reason, which is exactly the artefact
  `.claude/CLAUDE.md`'s Architecture Review rule needs — it already mandates a review for any diff
  that "adds or changes an Alembic migration", and this gives that review something to read.
- It needs no git history, so it runs at `fetch-depth: 1`, in the existing `lint` job, and locally in
  the fast dev loop with no setup.
- It costs one extra step when adding a migration (`--write`). Forgetting it fails CI with a
  one-command fix, which is the right failure mode.

---

## 4. Where the boundary is

The audit's hypothesis was _"frozen once merged to dev"_. **That is the wrong boundary, and `0082`
proves it.** `0082`'s dangerous edit happened before its first commit — the fast dev loop
(`CLAUDE.local.md`) rsyncs the **working tree** to the plex host and builds an image from it, so a
migration runs against the maintainer's live `/config/shortlist` database with no commit, no push, no
CI and no registry image involved.

The boundary that actually matters is: **a migration is frozen the moment its revision has been
stamped on any database that will not replay it.** For this project that is the first `docker build`
on the host, or the first local `uvicorn` boot — both of which happen before the first commit.

CI cannot observe either. So the rule splits in two:

**The rule CI enforces:** a migration's fingerprint is frozen from the moment it appears in
`frozen_migrations.txt`, and every migration file must have a line. Changing a fingerprint requires an
`amended:` line with a written reason.

**The rule the developer enforces:** run `python scripts/check_migration_freeze.py --write`
**when you create the migration** — before you run it anywhere, before the rsync, before the first
boot. That puts the line in the working tree at the moment the migration becomes real, so the local
`pytest tests/unit/test_migration_freeze.py` (a 40 ms test, runnable in the fast loop) catches the
`0082` edit _pre-commit_, which is the only place it can be caught.

Note the boundary is deliberately _not_ "merged to dev" even for the CI half. A `dev` push is a
production deploy on a ≤4h watchtower timer, and the fast loop deploys sooner than that — so "not yet
merged" is no evidence a migration hasn't run. `0070`'s two edits were 40 and 49 minutes after its
first commit, well inside the window where the answer is genuinely unknown.

### Closing the pre-commit gap mechanically (recommended, separable)

The fast dev loop is currently four ad-hoc shell commands pasted from `CLAUDE.local.md`. Committing it
as `scripts/devbuild.sh` and starting it with:

```bash
python scripts/check_migration_freeze.py --write   # stamp any new migration BEFORE it runs anywhere
python scripts/check_migration_freeze.py           # and refuse to deploy an unexplained edit
```

moves the freeze point from "first commit" to "first time it runs on a real database", which is the
boundary that matters and the one `0082` crossed. This is the single highest-value part of the design
and the only part that would have caught `0082`. It is separable from the CI work and can land first.

---

## 5. The escape hatch

**The default answer is a new migration.** The repo already does this correctly three times —
`0034` for `0032`, `0083` for `0082`, `0089` for `0088` — and
`tests/integration/test_migration_recovery.py::test_every_revision_is_re_runnable_after_a_crash`
already forces every new revision to be idempotent, which is exactly what a fix-forward needs. The
failure message leads with this, not with the override.

**When editing is genuinely right**, it is done with one command that writes an auditable line:

```bash
python scripts/check_migration_freeze.py --amend 0063 \
  --reason "downgrade() only; no database has ever been downgraded past 0063, so no stamped install carries the old code"
```

which rewrites that manifest line to:

```
0063  2f9c…  amended:2026-09-05  downgrade() only; no database has ever been downgraded past 0063, …
```

The properties that matter:

- `--write` **cannot** update an existing fingerprint. It only adds lines for revisions the manifest
  has never seen. So the routine "stamp my new migration" command can never silently launder an edit.
- `--amend` requires `--reason`, rejects a reason under 30 characters, and rejects the obvious
  placeholders (`fix`, `wip`, `n/a`, `todo`). The reason must answer one question: _why is every
  database already stamped at this revision fine without this change?_
- An already-`amended` line that changes again fails again. Each amendment is its own decision.
- The `amended:` line is a diff hunk in `frozen_migrations.txt`, in the same commit as the migration
  edit, which is already in the mandatory-Architecture-Review category.

Applied to history, the five correct edits in §2 would each have produced a one-line reason
(`0001` twice: "fresh-install schema kept in step with `0042`/`0065`, which does the same thing for
stamped installs"), and the four bugs would have had no honest reason to write — which is the point.

---

## 6. The second half of the bug — and what already exists

### An empty-database schema test already exists. Three of them.

Verified in the repo, all currently passing:

| Test                                                                                                                       | What it does                                                                                                                                                                                                                        |
| -------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/unit/test_migrations.py::TestTheMigratedSchemaMatchesTheORM::test_a_migrated_database_has_no_drift_from_the_models` | `run_migrations(tmp_path)` on an empty dir, then `alembic.autogenerate.compare_metadata` against `Base.metadata` — catches any column, index, unique constraint, FK or `ondelete` the migrations build differently from `models.py` |
| `tests/unit/test_migration_initial.py::test_initial_migration_schema_matches_the_models`                                   | column sets of the squashed `0001` vs the ORM                                                                                                                                                                                       |
| `tests/unit/test_migrations.py::TestUpgradingAnOldInstall::test_an_upgraded_old_database_has_no_drift_from_the_models`     | an early-beta database migrated to head, then the same drift check                                                                                                                                                                  |

Plus `tests/integration/test_migration_recovery.py::test_every_revision_is_re_runnable_after_a_crash`,
parametrised over every revision read from the script directory, which replays each one against a
database that already has its changes.

**Do not propose a new one.** The gap is not coverage of the schema; it is that none of these can see
the two things that actually went wrong:

1. **Data.** `compare_metadata` compares schema. `0032`, `0038`, `0063`, `0065`, `0081` and eleven
   others rewrite rows, and a drift test is blind to all of it.
2. **The already-stamped database.** Every one of these builds its database by running the _current_
   files. A database stamped `0082` by an older version of that file cannot be reconstructed from the
   working tree at all — that is the definition of the bug, and no fresh-database test can reach it.

### The complementary check: every data migration must be named in a test

`0032` was a no-op on the day it was written because nothing seeded a `settings` row in the shape the
product writes and asserted the migration changed it. Mechanically detectable, and — measured on the
current tree — nearly free to adopt:

```
migrations with DML: 17
named in some test : 16
NOT named anywhere : ['0038']
```

Sixteen of seventeen already comply. `0038_drop_watch_events.py` is the one gap, and it is real: line
43 is `bind.execute(sa.text("delete from settings where key = 'plex.db_path'"))`, and no test asserts
that row goes away.

The rule: **a migration whose `upgrade()` or `downgrade()` executes SQL must have its revision id
appear in a test file.** Implementation in §7.3.

It is a coverage rule, not a correctness rule, and it has a real limit worth stating: a developer who
writes the migration against the wrong storage shape will usually write the _test_ against the same
wrong shape, and both will pass. `tests/unit/test_migrations.py` already blunts this for the exact
case that bit `0032` — `_write_setting` is a shared helper whose docstring says why:

> A bare value here would test a shape the product never writes — the "fake must be no easier than the
> real server" rule.

So the honest claim is: the rule forces the question to be asked, and the shared seeding helper makes
the wrong answer hard for the one table that has actually caused this. It is not a proof.

---

## 7. The code

### 7.1 `scripts/check_migration_freeze.py`

Standard library only — it runs in the `lint` job, which installs nothing but `ruff`.

```python
#!/usr/bin/env python3
"""Freeze a migration's executable content once it has run somewhere.

A migration that has already been applied to a database is never replayed there. Editing the file
therefore changes what FRESH installs get and nothing else — the machine it was developed against
keeps the old behaviour for ever, which is how `0070`'s withdrawn backfill stayed on the maintainer's
server and how `0082` shipped without the column it had gained. See
`.claude/docs/migration-ci-design.md`.

The fingerprint is the parsed AST with docstrings stripped, so docstrings, comments and `ruff format`
never trip this — only executable content does.

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

MIN_REASON_CHARS = 30
PLACEHOLDER_REASONS = frozenset({"fix", "wip", "n/a", "na", "todo", "tbd", "-", "."})

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
    repo-wide isort sweep would fire on every migration at once, which is the noise that gets a check
    switched off.
    """
    body: list[ast.stmt] = []
    run: list[ast.stmt] = []
    for statement in [*tree.body, None]:
        if isinstance(statement, ast.Import | ast.ImportFrom):
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

    `ast.dump` is NOT stable across Python minors — 3.9 and 3.14 disagree on every file in this repo,
    because later versions add `_fields` entries that render as empty values. Skipping empty fields
    makes the two agree, which matters because the fingerprint has to mean the same thing on the
    maintainer's laptop as it does on CI's pinned 3.12.
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
        found[revision_of(source, path.stem[:4])] = (path, fingerprint(source))
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
        entries[revision] = (digest, status, parts[3] if len(parts) == 4 else "")
    return entries


def write_manifest(entries: dict[str, tuple[str, str, str]]) -> None:
    """Rewrite the manifest, revision order, header intact."""
    lines = [HEADER]
    for revision in sorted(entries):
        digest, status, note = entries[revision]
        lines.append(f"{revision}  {digest}  {status}" + (f"  {note}" if note else ""))
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check(found: dict[str, tuple[Path, str]], entries: dict[str, tuple[str, str, str]]) -> list[str]:
    """Every disagreement between the tree and the manifest, as ready-to-print problems."""
    problems: list[str] = []
    for revision in sorted(set(found) | set(entries)):
        if revision not in entries:
            path = found[revision][0]
            problems.append(
                f"{path.name}: migration {revision} is not in {MANIFEST.name}.\n"
                f"  Stamp it now, BEFORE you run it anywhere:\n"
                f"    python scripts/check_migration_freeze.py --write"
            )
        elif revision not in found:
            problems.append(
                f"migration {revision} is in {MANIFEST.name} but no longer exists on disk.\n"
                f"  Deleting an applied migration strands every database stamped at it — alembic can\n"
                f"  no longer resolve the revision and the container will not boot. If the removal is\n"
                f"  genuinely intended, drop its manifest line in the same commit."
            )
        elif entries[revision][0] != found[revision][1]:
            path = found[revision][0]
            problems.append(
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
    return problems


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="add lines for NEW migrations; never changes an existing one")
    parser.add_argument("--amend", metavar="REVISION", help="deliberately re-freeze an edited migration")
    parser.add_argument("--reason", default="", help="why databases already stamped at that revision are fine")
    args = parser.parse_args(argv)

    found = migrations()
    entries = read_manifest()

    if args.amend:
        revision = args.amend
        if revision not in found:
            print(f"no migration {revision} on disk", file=sys.stderr)
            return 2
        reason = " ".join(args.reason.split())
        if len(reason) < MIN_REASON_CHARS or reason.lower().rstrip(".") in PLACEHOLDER_REASONS:
            print(
                f"--amend needs a --reason of at least {MIN_REASON_CHARS} characters saying why every\n"
                f"database already stamped {revision} is fine without this change.",
                file=sys.stderr,
            )
            return 2
        if revision in entries and entries[revision][0] == found[revision][1]:
            print(f"migration {revision} matches the manifest — nothing to amend", file=sys.stderr)
            return 2
        entries[revision] = (found[revision][1], f"amended:{date.today().isoformat()}", reason)
        write_manifest(entries)
        print(f"amended {revision}: {reason}")
        return 0

    if args.write:
        added = sorted(set(found) - set(entries))
        for revision in added:
            entries[revision] = (found[revision][1], "frozen", "")
        if added:
            write_manifest(entries)
            print(f"froze {len(added)} new migration(s): {', '.join(added)}")
        else:
            print("no new migrations to freeze")

    problems = check(found, read_manifest())
    for problem in problems:
        print(f"::error::{problem}" if problem.count("\n") == 0 else problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} migration freeze problem(s)", file=sys.stderr)
        return 1
    print(f"{len(found)} migrations match {MANIFEST.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### 7.2 The CI wiring

**No new job, and no new status context.** `.claude/CLAUDE.md` records that renaming the required
contexts is what blocked the v1.7.0 release PR with every check green, and `master` requires exactly
`lint` / `test-python` / `test-web` / `e2e`. This adds a _step_ to the existing `lint` job, whose name
does not change:

```yaml
lint:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v7
    - uses: actions/setup-python@v7
      with:
        python-version: "3.12"
    - run: pip install ruff
    - run: ruff check .
    - run: ruff format --check .

    # A migration already applied to a database is never replayed there, so an edit to one reaches
    # fresh installs only — the machine it was developed against keeps the old behaviour for ever.
    # Four occurrences in this repo's history (0070 twice, 0078, 0081); see
    # .claude/docs/migration-ci-design.md. Standard library only, and needs no git history, so it
    # runs here at the default fetch-depth rather than paying for a full clone in its own job.
    - name: No merged migration's content has changed
      run: python scripts/check_migration_freeze.py

    # ... the existing requirements.lock step follows unchanged
```

That is the whole CI change. The check also runs under `test-python` via §7.3's test file, so both
required contexts cover it, and neither is renamed.

### 7.3 `.pre-commit-config.yaml`

The hook is where this catches the edit _before_ it becomes a commit. Appended to the existing config:

```yaml
# Local, because it must run against the working tree with no network and no install — the whole
# value is catching the edit before it is committed, in the fast dev loop.
- repo: local
  hooks:
    - id: migration-freeze
      name: no merged migration's content has changed
      entry: python scripts/check_migration_freeze.py
      language: system
      pass_filenames: false
      files: ^shortlist/server/db/alembic/
```

### 7.4 `tests/unit/test_migration_freeze.py`

```python
"""The freeze check itself — and the coverage rule for data migrations.

`tests/unit/test_migrations.py` proves the migrated schema matches the ORM on a FRESH database. That
can never see this bug: a database already stamped at a revision does not replay it, so an edited
migration is correct on every fresh install and wrong only where it already ran. These tests guard the
one mechanism that can see it.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_migration_freeze.py"
VERSIONS_DIR = REPO_ROOT / "shortlist" / "server" / "db" / "alembic" / "versions"


def _load():
    """Import the checker by path — `scripts/` is not a package, deliberately."""
    spec = importlib.util.spec_from_file_location("check_migration_freeze", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


freeze = _load()

BASE = '''\
"""A migration."""

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"


def upgrade() -> None:
    op.add_column("rows", sa.Column("flag", sa.Boolean(), nullable=True))
'''


class TestTheFingerprintIgnoresWhatDoesNotRun:
    """Anything that fires on a comment gets switched off. These are the changes it must NOT see."""

    @pytest.mark.parametrize(
        "edited",
        [
            pytest.param(BASE.replace('"""A migration."""', '"""A migration.\n\nRewritten prose.\n"""'), id="docstring"),
            pytest.param(BASE.replace("def upgrade", "# why this exists\ndef upgrade"), id="comment"),
            pytest.param(BASE.replace("import sqlalchemy as sa\n", "import sqlalchemy as sa\n\n\n"), id="blank-lines"),
            pytest.param(
                BASE.replace(
                    'op.add_column("rows", sa.Column("flag", sa.Boolean(), nullable=True))',
                    'op.add_column(\n        "rows",\n        sa.Column("flag", sa.Boolean(), nullable=True),\n    )',
                ),
                id="reformatted",
            ),
            pytest.param(
                BASE.replace("import sqlalchemy as sa\nfrom alembic import op", "from alembic import op\nimport sqlalchemy as sa"),
                id="import-order",
            ),
        ],
    )
    def test_a_change_that_cannot_alter_behaviour_keeps_the_fingerprint(self, edited: str):
        assert freeze.fingerprint(edited) == freeze.fingerprint(BASE)

    @pytest.mark.parametrize(
        "edited,why",
        [
            (BASE.replace("nullable=True", "nullable=False"), "a changed keyword"),
            (BASE.replace("sa.Boolean()", "sa.String(length=16)"), "a changed column type"),
            (BASE.replace('    op.add_column("rows", sa.Column("flag", sa.Boolean(), nullable=True))', "    pass"), "a removed body"),
            (BASE + '\n\ndef downgrade() -> None:\n    op.drop_column("rows", "flag")\n', "an added function"),
            (BASE.replace('"rows"', '"collections"'), "a changed table name"),
        ],
    )
    def test_a_change_that_can_alter_behaviour_changes_the_fingerprint(self, edited: str, why: str):
        assert freeze.fingerprint(edited) != freeze.fingerprint(BASE), why

    def test_the_fingerprint_does_not_depend_on_the_python_minor(self):
        """`ast.dump` disagrees between 3.9 and 3.14 on every file in this repo; the canonicaliser
        skips the empty fields that cause it. Pinned as a literal so a future interpreter that breaks
        the property fails HERE rather than invalidating all 62 manifest lines at once."""
        assert freeze.fingerprint(BASE) == "0f1a…"  # regenerate deliberately, never to make CI green


class TestTheCheckerCatchesTheRealThing:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_the_repo_as_committed_passes(self):
        assert self._run().returncode == 0

    def test_a_changed_migration_fails(self, tmp_path: Path, monkeypatch):
        """The deliberately-broken input: 0089's real content with one keyword flipped."""
        victim = VERSIONS_DIR / "0089_drop_row_shown_state.py"
        original = victim.read_text()
        edited = original.replace("def upgrade() -> None:", "def upgrade() -> None:\n    return")
        assert edited != original, "the edit must actually change something"
        victim.write_text(edited)
        try:
            result = self._run()
        finally:
            victim.write_text(original)
        assert result.returncode == 1
        assert "executable content of migration 0089 changed" in result.stderr
        assert "NEW migration" in result.stderr, "the message must lead with fix-forward, not with --amend"

    def test_a_new_migration_with_no_manifest_line_fails(self, monkeypatch):
        new = VERSIONS_DIR / "0999_unstamped.py"
        new.write_text(BASE.replace('"0099"', '"0999"'))
        try:
            result = self._run()
        finally:
            new.unlink()
        assert result.returncode == 1
        assert "not in frozen_migrations.txt" in result.stderr

    @pytest.mark.parametrize("reason", ["", "fix", "wip", "too short to be a reason"])
    def test_amend_refuses_a_reason_that_explains_nothing(self, reason: str):
        result = self._run("--amend", "0089", "--reason", reason)
        assert result.returncode == 2
        assert "at least 30 characters" in result.stderr

    def test_write_never_launders_an_edit(self):
        """`--write` is the routine command, so it must be incapable of hiding a changed fingerprint."""
        victim = VERSIONS_DIR / "0089_drop_row_shown_state.py"
        original = victim.read_text()
        victim.write_text(original.replace("def upgrade() -> None:", "def upgrade() -> None:\n    return"))
        try:
            result = self._run("--write")
        finally:
            victim.write_text(original)
        assert result.returncode == 1, "--write must still fail on an edited migration"


#: Migrations whose DML predates this rule and has no test. Shrink this list; never grow it.
UNTESTED_DML = {"0038"}


def test_every_data_migration_is_named_in_a_test():
    """A migration that rewrites ROWS is invisible to every schema test in this repo.

    `0032` was a no-op on every real database on the day it was written — it compared the whole
    `{"v": …}` settings envelope to a bare string — and no test seeded a settings row and asserted it
    changed. Schema drift tests cannot see that; only a seeded before/after can.
    """
    suite = "\n".join(p.read_text(errors="ignore") for p in (REPO_ROOT / "tests").rglob("*.py"))
    missing = set()
    for path in sorted(VERSIONS_DIR.glob("[0-9]*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        executes = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"execute", "bulk_insert"}
            for node in ast.walk(tree)
        )
        if not executes:
            continue
        revision = re.search(r'^revision = "([^"]+)"', source, re.M).group(1)
        if revision not in suite:
            missing.add(revision)
    assert missing <= UNTESTED_DML, (
        f"migration(s) {sorted(missing - UNTESTED_DML)} rewrite rows but are named in no test. "
        "Seed the pre-state and assert the post-state — use test_migrations.py::_write_setting for "
        "settings, so the fixture cannot be an easier shape than the product writes."
    )
```

---

## 8. Proving the check works

The deliberately-broken inputs are §7.4's tests. Each names the exact failure it forces:

| Input                                                                                | Expected                                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------- |
| The repo as committed                                                                | pass                                                        |
| `0089` with `return` inserted at the top of `upgrade()`                              | fail, message names `0089` and leads with "a NEW migration" |
| A new migration file with no manifest line                                           | fail, tells you to `--write`                                |
| A manifest line whose migration was deleted                                          | fail, explains the stranded-revision risk                   |
| `--amend` with `""`, `fix`, `wip`, or a 22-character reason                          | exit 2, no manifest written                                 |
| `--write` while a migration is edited                                                | fail — `--write` must not be a laundering path              |
| Docstring rewritten / comment added / blank lines / reformatted / imports reordered  | fingerprint unchanged                                       |
| `nullable=True` → `False`, type changed, body removed, function added, table renamed | fingerprint changed                                         |
| Same source on two Python minors                                                     | identical fingerprint                                       |

Two of those deserve breaking the code to prove they have teeth (`.claude/CLAUDE.md`: do this where
the logic is risky or subtle, not everywhere):

- **`test_write_never_launders_an_edit`** — delete the second `read_manifest()` call in `main()` so
  `--write` checks its own freshly-written entries. The test must fail. This is the one property that
  makes the routine command safe.
- **`test_a_change_that_cannot_alter_behaviour_keeps_the_fingerprint[docstring]`** — remove
  `_strip_docstrings`. The test must fail. Without it the check fires on prose, and a check that fires
  on prose gets switched off.

The one-off historical verification behind §2 is not a test (CI clones at depth 1). It is reproducible
on a full clone by fingerprinting every version of every migration from `git log --format=%h --reverse
-- <path>` and reporting where consecutive fingerprints differ; the expected output is the block in §2.

---

## 9. What could regress — when this fires falsely

Measured on real history, not guessed. Over 62 migrations and ~14 months the check would have fired
nine times, of which **five were legitimate work**:

1. **The paired edit to `0001_initial`** (twice: `5c44a03`, `c33a1a1`). This repo deliberately keeps
   the squashed initial migration in step with the fresh-install schema and ships a numbered migration
   alongside it for stamped installs. Both edits did exactly that (`0042`, `0065`). This is a routine,
   correct pattern here, and it will keep firing — roughly whenever a column is added to a table
   `0001` creates. It is the single largest false-positive source. Mitigation is the `--amend` line,
   not an exemption: exempting `0001` would exempt the largest and most consequential migration in the
   tree.

2. **A migration corrected within its own unpushed branch, after its first commit.** `0070`
   @`f8876b8` looks exactly like this — and was not. The check cannot tell them apart, and neither can
   the developer without asking "has the host build run this?" — which is the question worth forcing.

3. **A `downgrade()`-only correction** (`0063` @`ea779cc`). Genuinely safe: downgrade code is read at
   downgrade time, so an edit does take effect on stamped databases. Fires anyway. The reason text is
   one line.

4. **A repo-wide `ruff format` or `ruff check --fix` sweep.** Handled: formatting is invisible to the
   fingerprint and `_sort_import_runs` absorbs isort's `I` rule. Verified against `0060`'s real
   docstring-only edit, which goes quiet.

5. **A Python interpreter that changes the AST in a way the canonicaliser does not absorb.** Would
   invalidate all 62 lines at once — the worst possible failure. `test_the_fingerprint_does_not_depend_
on_the_python_minor` pins one literal so the failure lands on one test with an obvious cause rather
   than as 62 simultaneous errors. Verified stable 3.9.6 → 3.14.6.

6. **`--write` forgotten when adding a migration.** Fires with a one-command fix. Annoying, not
   dangerous, and it is the price of not having a "trust the tree" mode.

**Net:** roughly one fire every six weeks, about half of them real bugs, and every false fire cleared
by one command with one sentence of justification. The risk this design does _not_ remove is that
`--amend` becomes reflexive — a reason written to get past CI rather than to answer the question. That
is a human control, and the honest mitigation is the one already in `.claude/CLAUDE.md`: any diff that
changes an Alembic migration goes to Architecture Review, and an `amended:` line is the thing that
review should read first.

---

## 10. Open questions

- **Whether `0078`'s stamped databases actually matter.** The edit changed `sa.DateTime` to
  `sa.DateTime(timezone=True)` on two columns of a table the migration creates. SQLite ignores the
  flag, so the practical impact is nil — but I could not verify that no future backend (or
  `compare_metadata` run against a non-SQLite target) treats it differently. Classified as "the bug,
  benign" above on that basis.
- **Whether the maintainer's live database is stamped past every revision in §2's table.** The four
  bug-shaped edits only did damage if `/config/shortlist/shortlist.db` had already run the old file.
  For `0070` the commit message makes it certain; for `0078` and `0081` it is inferred from the
  fast-loop workflow, not confirmed. Confirming it is a read-only `SELECT version_num FROM
alembic_version` plus the dates — worth doing before deciding whether any of them still needs a
  fix-forward migration today.
- **Whether `scripts/deploy.sh` or the ad-hoc rsync loop is the current deploy path.** `deploy.sh`
  pulls `ghcr.io/stevezau/shortlist:dev` and disables watchtower on the container; `CLAUDE.local.md`
  describes watchtower recreating every container on a 4h poll. Both cannot be current. §4's
  `scripts/devbuild.sh` recommendation assumes the rsync loop is what actually runs migrations first;
  if it isn't, the pre-commit half of this design needs re-siting.
- **The literal in `test_the_fingerprint_does_not_depend_on_the_python_minor`** is written as `"0f1a…"`
  above. It must be generated on 3.12 when the file is written; I did not have a 3.12 interpreter
  available (this machine has 3.9.6 and 3.14.6, which is what the cross-version check used).
- **`0038`'s missing test.** The `UNTESTED_DML` allowance exists so the coverage rule can land today.
  Whether `0038`'s `delete from settings where key = 'plex.db_path'` deserves a test or the rule
  deserves a narrower DML detector is a judgement call I left to the implementer.

## Files read for this design (read-only, no edits)

`.github/workflows/ci.yml`, `.pre-commit-config.yaml`, `pyproject.toml`, `scripts/deploy.sh`,
`tests/conftest.py` (110-175), `tests/unit/test_migrations.py`, `tests/unit/test_migration_initial.py`,
`tests/integration/test_migration_recovery.py`, and migrations `0001`, `0032`, `0033`, `0034`, `0038`,
`0060`, `0063`, `0070`, `0078`, `0081`, `0082`, `0083`, `0088`, `0089`. Git history: `git log`
(exact-path and `--follow`) and `git show` over every migration, plus commits `efaf9f9`, `1bd41ce`,
`4352559`, `5c44a03`, `c33a1a1`, `ea779cc`, `f8876b8`, `29cb475`, `6ddc021`, `77c1283`, `8c5310f`,
`e54869d`/`2cbf403` (the duplicate-commit pair).
