"""The migration freeze — and the coverage rule for data migrations.

`test_migrations.py` proves the migrated schema matches the ORM on a FRESH database. That can never
see this bug: a database already stamped at a revision does not replay it, so an edited migration is
correct on every fresh install and wrong only where it already ran. These tests guard the one
mechanism that can see it. See `.claude/docs/migration-ci-design.md`.

Nothing here writes to the real `versions/` directory. The checker resolves its paths from
`__file__`, so every mutation test copies the script and the real migrations into a sandbox and runs
it there — the suite runs under xdist, where editing a file the other workers are importing is a data
race dressed up as a test.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_migration_freeze.py"
VERSIONS_DIR = REPO_ROOT / "shortlist" / "server" / "db" / "alembic" / "versions"
MANIFEST = VERSIONS_DIR.parent / "frozen_migrations.txt"

#: The migration the mutation tests deform. Any would do; a recent, short one keeps the diff obvious.
VICTIM = "0089_drop_row_shown_state.py"


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
            pytest.param(
                BASE.replace('"""A migration."""', '"""A migration.\n\nRewritten prose.\n"""'), id="docstring"
            ),
            pytest.param(BASE.replace("def upgrade", "# why this exists\ndef upgrade"), id="comment"),
            pytest.param(BASE.replace("import sqlalchemy as sa\n", "import sqlalchemy as sa\n\n\n"), id="blank-lines"),
            pytest.param(BASE.replace("\n", "  \n"), id="trailing-whitespace"),
            pytest.param(
                BASE.replace(
                    'op.add_column("rows", sa.Column("flag", sa.Boolean(), nullable=True))',
                    'op.add_column(\n        "rows",\n        sa.Column("flag", sa.Boolean(), nullable=True),\n    )',
                ),
                id="reformatted",
            ),
            pytest.param(
                BASE.replace(
                    "import sqlalchemy as sa\nfrom alembic import op", "from alembic import op\nimport sqlalchemy as sa"
                ),
                id="import-order",
            ),
        ],
    )
    def test_a_change_that_cannot_alter_behaviour_keeps_the_fingerprint(self, edited: str):
        assert edited != BASE, "the parametrised edit must actually change the source"
        assert freeze.fingerprint(edited) == freeze.fingerprint(BASE)

    @pytest.mark.parametrize(
        "edited,why",
        [
            pytest.param(BASE.replace("nullable=True", "nullable=False"), "a changed keyword", id="keyword"),
            pytest.param(BASE.replace("sa.Boolean()", "sa.String(length=16)"), "a changed type", id="column-type"),
            pytest.param(
                BASE.replace('    op.add_column("rows", sa.Column("flag", sa.Boolean(), nullable=True))', "    pass"),
                "a removed body",
                id="removed-body",
            ),
            pytest.param(
                BASE + '\n\ndef downgrade() -> None:\n    op.drop_column("rows", "flag")\n',
                "an added function",
                id="added-function",
            ),
            pytest.param(BASE.replace('"rows"', '"collections"'), "a changed table name", id="table-name"),
            pytest.param(BASE.replace('down_revision = "0098"', 'down_revision = "0097"'), "a re-parented revision"),
        ],
    )
    def test_a_change_that_can_alter_behaviour_changes_the_fingerprint(self, edited: str, why: str):
        assert freeze.fingerprint(edited) != freeze.fingerprint(BASE), why

    def test_re_rendering_every_real_migration_from_its_own_ast_changes_nothing(self):
        """The false-positive proof, run against all 62 real migrations rather than a toy.

        `ast.unparse` throws away every comment, every blank line, every quote style and all
        line-wrapping, and re-emits the file from its syntax tree — a more violent reformat than
        `ruff format` will ever perform. If a single fingerprint moved, the check would fire on the
        next repo-wide format sweep, and a check that fires on formatting gets switched off.
        """
        moved = []
        for path in sorted(VERSIONS_DIR.glob("[0-9]*.py")):
            source = path.read_text(encoding="utf-8")
            if freeze.fingerprint(source) != freeze.fingerprint(ast.unparse(ast.parse(source))):
                moved.append(path.name)

        assert moved == [], "reformatting alone moved these fingerprints"


class TestTheFingerprintSurvivesAPythonUpgrade:
    """The worst failure this design has is all 62 lines breaking at once on a new interpreter.

    The manifest in this repo was generated on 3.14.6 and verified byte-identical on 3.9.6 — a span
    that brackets CI's pinned 3.12 — but no 3.12 interpreter was available to generate it on, so
    there is no golden literal here to pin. These tests assert the PROPERTY that made the two agree
    instead, which is stronger than a literal: it holds on whatever interpreter runs it.
    """

    FUTURE_FIELD = "type_params_of_some_later_python"

    def test_a_new_empty_ast_field_is_invisible(self, monkeypatch: pytest.MonkeyPatch):
        """Every cross-version break measured on this repo had this one cause: a later Python adds a
        `_fields` entry (`type_params` on `FunctionDef` in 3.12) that these files leave empty."""
        before = freeze.fingerprint(BASE)
        monkeypatch.setattr(ast.FunctionDef, "_fields", (*ast.FunctionDef._fields, self.FUTURE_FIELD))
        monkeypatch.setattr(ast.FunctionDef, self.FUTURE_FIELD, [], raising=False)

        assert freeze.fingerprint(BASE) == before

    def test_a_new_populated_ast_field_is_not_invisible(self, monkeypatch: pytest.MonkeyPatch):
        """The control. Without it the test above passes just as happily if `_canonical` skipped
        every unknown field — which would make the fingerprint blind to real syntax."""
        before = freeze.fingerprint(BASE)
        monkeypatch.setattr(ast.FunctionDef, "_fields", (*ast.FunctionDef._fields, self.FUTURE_FIELD))
        monkeypatch.setattr(ast.FunctionDef, self.FUTURE_FIELD, ["something"], raising=False)

        assert freeze.fingerprint(BASE) != before

    def test_the_fingerprint_does_not_depend_on_line_numbers(self):
        """`ast.dump(include_attributes=True)` would fold `lineno`/`col_offset` in, and then adding a
        comment at the top of a file would change every node under it."""
        assert freeze.fingerprint("\n\n\n" + BASE) == freeze.fingerprint(BASE)


class TestTheCheckerCatchesTheRealThing:
    @pytest.fixture
    def sandbox(self, tmp_path: Path) -> Path:
        """A throwaway repo carrying the REAL migrations, the REAL manifest and the REAL script.

        The script resolves `REPO_ROOT` from its own `__file__`, so copying it into `<tmp>/scripts/`
        is all it takes to point it at the copy. Mutations therefore never touch the working tree.
        """
        root = tmp_path / "repo"
        (root / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
        alembic = root / "shortlist" / "server" / "db" / "alembic"
        shutil.copytree(VERSIONS_DIR, alembic / "versions")
        shutil.copy(MANIFEST, alembic / MANIFEST.name)
        return root

    @staticmethod
    def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(root / "scripts" / SCRIPT.name), *args], capture_output=True, text=True
        )

    @staticmethod
    def _versions(root: Path) -> Path:
        return root / "shortlist" / "server" / "db" / "alembic" / "versions"

    def test_the_repo_as_committed_passes(self):
        """Run against the real tree, read-only. This is also what proves the manifest — generated on
        3.14.6 — still validates on CI's 3.12, because there this runs on 3.12."""
        result = self._run(REPO_ROOT)
        assert result.returncode == 0, result.stderr
        assert "migrations match" in result.stdout

    def test_an_edited_migration_fails_and_names_it(self, sandbox: Path):
        victim = self._versions(sandbox) / VICTIM
        original = victim.read_text()
        victim.write_text(original.replace("def upgrade() -> None:", "def upgrade() -> None:\n    return"))

        result = self._run(sandbox)

        assert result.returncode == 1
        assert "executable content of migration 0089 changed" in result.stderr
        assert "NEW migration" in result.stderr, "the message must lead with fix-forward, not with --amend"
        assert "--amend 0089" in result.stderr, "and must still say how, once fix-forward is ruled out"

    @pytest.mark.parametrize(
        "rewrite,id_",
        [
            (lambda s: s.replace("def upgrade", "# an explanatory comment\ndef upgrade"), "comment"),
            (lambda s: s.replace('"""', '"""Rewritten prose. ', 1), "docstring"),
            (lambda s: s.replace("\n\n", "\n\n\n"), "blank-lines"),
            (lambda s: ast.unparse(ast.parse(s)), "fully-reformatted"),
        ],
    )
    def test_a_change_with_no_executable_effect_does_not_fire(
        self, sandbox: Path, rewrite: Callable[[str], str], id_: str
    ):
        """The false-positive proof end to end: the whole checker, not just `fingerprint()`."""
        victim = self._versions(sandbox) / VICTIM
        rewritten = rewrite(victim.read_text())
        assert rewritten != victim.read_text(), f"{id_}: the rewrite must actually change the file"
        victim.write_text(rewritten)

        result = self._run(sandbox)

        assert result.returncode == 0, result.stderr

    def test_a_repo_wide_reformat_of_every_migration_does_not_fire(self, sandbox: Path):
        """The scenario that would get this check switched off: someone runs `ruff format` (or bumps
        ruff and it re-wraps differently) across the tree. Simulated harder than ruff would — every
        file re-emitted from its own syntax tree, comments and layout gone — over all 62 at once."""
        for path in sorted(self._versions(sandbox).glob("[0-9]*.py")):
            path.write_text(ast.unparse(ast.parse(path.read_text())))

        result = self._run(sandbox)

        assert result.returncode == 0, result.stderr

    def test_a_new_migration_with_no_manifest_line_fails(self, sandbox: Path):
        (self._versions(sandbox) / "0999_unstamped.py").write_text(BASE.replace('"0099"', '"0999"'))

        result = self._run(sandbox)

        assert result.returncode == 1
        assert "not in frozen_migrations.txt" in result.stderr
        assert "--write" in result.stderr

    def test_a_deleted_migration_fails(self, sandbox: Path):
        (self._versions(sandbox) / VICTIM).unlink()

        result = self._run(sandbox)

        assert result.returncode == 1
        assert "no longer exists on disk" in result.stderr
        assert "strands every database stamped at it" in result.stderr

    def test_write_stamps_only_the_new_migration(self, sandbox: Path):
        (self._versions(sandbox) / "0999_unstamped.py").write_text(BASE.replace('"0099"', '"0999"'))
        before = (sandbox / "shortlist/server/db/alembic/frozen_migrations.txt").read_text()

        result = self._run(sandbox, "--write")

        after = (sandbox / "shortlist/server/db/alembic/frozen_migrations.txt").read_text()
        assert result.returncode == 0, result.stderr
        assert "froze 1 new migration(s): 0999" in result.stdout
        added = [line for line in after.splitlines() if line not in before.splitlines()]
        assert len(added) == 1 and added[0].startswith("0999  "), f"--write touched more than the new line: {added}"

    def test_write_never_launders_an_edit(self, sandbox: Path):
        """`--write` is the routine command, so it must be incapable of hiding a changed fingerprint.

        Teeth proven by widening `--write` to `sorted(set(found))` — the naive "just regenerate the
        manifest" implementation. This then exits 0 with the edit blessed and the line rewritten.
        """
        victim = self._versions(sandbox) / VICTIM
        manifest = sandbox / "shortlist/server/db/alembic/frozen_migrations.txt"
        before = manifest.read_text()
        victim.write_text(victim.read_text().replace("def upgrade() -> None:", "def upgrade() -> None:\n    return"))

        result = self._run(sandbox, "--write")

        assert result.returncode == 1, "--write must still fail on an edited migration"
        assert manifest.read_text() == before, "--write must not rewrite the line it failed on"

    @pytest.mark.parametrize(
        "reason", ["", "fix", "WIP", "n/a", "too short to be a reason", "..............................."]
    )
    def test_amend_refuses_a_reason_that_explains_nothing(self, sandbox: Path, reason: str):
        victim = self._versions(sandbox) / VICTIM
        manifest = sandbox / "shortlist/server/db/alembic/frozen_migrations.txt"
        before = manifest.read_text()
        victim.write_text(victim.read_text().replace("def upgrade() -> None:", "def upgrade() -> None:\n    return"))

        result = self._run(sandbox, "--amend", "0089", "--reason", reason)

        assert result.returncode == 2
        assert "at least 30 letters" in result.stderr
        assert manifest.read_text() == before, "a rejected amendment must not write anything"

    def test_amend_refuses_a_migration_that_was_never_frozen(self, sandbox: Path):
        """A brand-new migration has no stamped installs to reason about, so an `amended:` line
        would record a decision nobody made. That is a `--write`."""
        (self._versions(sandbox) / "0999_unstamped.py").write_text(BASE.replace('"0099"', '"0999"'))

        result = self._run(sandbox, "--amend", "0999", "--reason", "a perfectly well-formed and lengthy reason")

        assert result.returncode == 2
        assert "was never frozen — stamp it with --write" in result.stderr

    def test_amend_refuses_a_migration_that_did_not_change(self, sandbox: Path):
        """Otherwise `--amend` becomes a way to stamp an `amended:` line onto untouched history."""
        result = self._run(sandbox, "--amend", "0089", "--reason", "a perfectly well-formed and lengthy reason")

        assert result.returncode == 2
        assert "nothing to amend" in result.stderr

    def test_amend_records_the_reason_and_then_the_check_passes(self, sandbox: Path):
        victim = self._versions(sandbox) / VICTIM
        manifest = sandbox / "shortlist/server/db/alembic/frozen_migrations.txt"
        victim.write_text(victim.read_text().replace("def upgrade() -> None:", "def upgrade() -> None:\n    return"))
        reason = "only downgrade() changed and no install has ever been downgraded past 0089"

        amended = self._run(sandbox, "--amend", "0089", "--reason", reason)

        assert amended.returncode == 0, amended.stderr
        line = next(ln for ln in manifest.read_text().splitlines() if ln.startswith("0089 "))
        assert re.match(rf"^0089  [0-9a-f]{{64}}  amended:\d{{4}}-\d{{2}}-\d{{2}}  {re.escape(reason)}$", line), line
        assert self._run(sandbox).returncode == 0, "an amended migration is frozen again at its new content"

    def test_a_second_edit_to_an_amended_migration_fails_again(self, sandbox: Path):
        """Each amendment is its own decision — an `amended:` line is not a permanent exemption."""
        victim = self._versions(sandbox) / VICTIM
        victim.write_text(victim.read_text().replace("def upgrade() -> None:", "def upgrade() -> None:\n    return"))
        self._run(sandbox, "--amend", "0089", "--reason", "the first edit, explained at sufficient length")

        victim.write_text(victim.read_text().replace("    return", "    return None"))

        assert self._run(sandbox).returncode == 1

    def test_every_fingerprint_moving_at_once_is_reported_as_one_interpreter_problem(self, sandbox: Path):
        """A systemic mismatch agrees with itself on every line, so the per-migration check reads it
        as 62 independent edits and tells the developer to regenerate — which would re-freeze
        whatever the tree says and bless any real edit hiding among them. Same aggregate guard as
        `.claude/rules/plex-safety.md` §4's "not one row reads as labelled"."""
        manifest = sandbox / "shortlist/server/db/alembic/frozen_migrations.txt"
        manifest.write_text(
            "\n".join(
                re.sub(r"  [0-9a-f]{64}  ", "  " + "0" * 64 + "  ", line) if not line.startswith("#") else line
                for line in manifest.read_text().splitlines()
            )
        )

        result = self._run(sandbox)

        assert result.returncode == 1
        assert "That is not 62 edits" in result.stderr
        assert "Do NOT regenerate the manifest" in result.stderr
        assert "executable content of migration" not in result.stderr, "62 separate reports is the failure mode"

    @pytest.mark.parametrize(
        "bad_line,expected",
        [
            ("0089  deadbeef", "expected '<revision>"),
            ("0089  deadbeef  maybe", "status must be 'frozen'"),
            # A merge resolved by keeping both sides. Left to `dict` assignment the second line just
            # wins, so a real frozen fingerprint could be displaced without a word.
            ("0089  " + "0" * 64 + "  frozen", "listed twice"),
        ],
    )
    def test_a_malformed_manifest_line_is_a_hard_stop(self, sandbox: Path, bad_line: str, expected: str):
        manifest = sandbox / "shortlist/server/db/alembic/frozen_migrations.txt"
        manifest.write_text(manifest.read_text() + bad_line + "\n")

        result = self._run(sandbox)

        assert result.returncode == 1
        assert expected in result.stderr


#: Data migrations with no test. Empty on purpose — 0038, the last one, is covered by
#: `test_migrations.py::TestMigration0038DropsTheWatchHistoryMirror`. Never grow this.
UNTESTED_DML: set[str] = set()

_DML_CALLS = {"execute", "bulk_insert"}


def test_every_data_migration_is_named_in_a_test():
    """A migration that rewrites ROWS is invisible to every schema test in this repo.

    `0032` was a no-op on every real database on the day it was written — it compared the whole
    `{"v": …}` settings envelope to a bare string — and no test seeded a settings row and asserted it
    changed. `compare_metadata` cannot see that; only a seeded before/after can.

    This file is excluded from the corpus it searches: it names revisions as scaffolding (the
    mutation victim, the sample source), and scaffolding must not be able to satisfy the rule.
    """
    suite = "\n".join(
        path.read_text(errors="ignore")
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if path != Path(__file__)
    )
    missing = set()
    for path in sorted(VERSIONS_DIR.glob("[0-9]*.py")):
        source = path.read_text()
        writes_rows = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _DML_CALLS
            for node in ast.walk(ast.parse(source))
        )
        revision = freeze.revision_of(source, path.stem[:4])
        # Bounded so `0038` cannot be satisfied by an unrelated `10038` or `00381`.
        if writes_rows and not re.search(rf"(?<![0-9]){re.escape(revision)}(?![0-9])", suite):
            missing.add(revision)

    assert missing <= UNTESTED_DML, (
        f"migration(s) {sorted(missing - UNTESTED_DML)} rewrite rows but are named in no test. "
        "Seed the pre-state and assert the post-state — use test_migrations.py::_write_setting for "
        "settings, so the fixture cannot be an easier shape than the product writes."
    )
