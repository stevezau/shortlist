"""Put the FK ondelete policy in the schema instead of only in a docstring

`User`'s docstring has always split the five tables keyed to `users.id` on one question — *is this
row regenerable from Plex?* `watched_titles` and `watch_sync_state` are a cache, so they CASCADE and
have declared it since they were created. The other three are the only copy of what they hold:

* `picks` — the impact ledger behind every lifetime dashboard metric,
* `run_users` — the run history,
* `restriction_snapshots` — a person's share filters *as they were before Shortlist touched them*,
  which is what uninstall restores from (plex-safety rule 2). There is no second copy anywhere.

Those three declared no `ondelete` at all, so the half of the policy that protects the
irreplaceable half existed only as a comment. SQLite's default NO ACTION happens to behave the same
way, which is exactly what made it easy to lose: a future migration or a hand-written `ALTER` could
have turned any of them into a CASCADE without contradicting anything the schema said.

SQLite cannot alter a constraint in place, so each table is REBUILT in batch mode: the existing
table is reflected (columns, indexes, primary key and its other foreign keys all come from the real
schema, not from a copy of it written here), the `users` FK is redeclared with `ON DELETE RESTRICT`,
and the rows are copied across. Nothing is deleted and no value changes.

The FKs being dropped were created without names, and an unnamed constraint cannot be addressed in a
`DROP CONSTRAINT`. `naming_convention` is how alembic gives a reflected constraint a deterministic
name for the length of the batch — the documented recipe for exactly this case.
"""

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

#: How a reflected, unnamed foreign key is addressed inside the batch. It also names the OTHER FKs on
#: these tables (`picks.run_id`, `run_users.run_id`) as they are rebuilt, which is cosmetic: a
#: constraint's name is not part of what `compare_metadata` compares.
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}

#: The three tables whose rows cannot be rebuilt from Plex. See the module docstring.
_TABLES = ("picks", "run_users", "restriction_snapshots")


def _redeclare_user_fk(ondelete: str | None) -> None:
    """Rebuild each table with its `users.id` FK carrying (or losing) `ondelete`."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in _TABLES:
        if table not in tables:
            continue
        name = f"fk_{table}_user_id_users"
        with op.batch_alter_table(table, naming_convention=_NAMING) as batch_op:
            batch_op.drop_constraint(name, type_="foreignkey")
            batch_op.create_foreign_key(name, "users", ["user_id"], ["id"], ondelete=ondelete)


def upgrade() -> None:
    _redeclare_user_fk("RESTRICT")


def downgrade() -> None:
    # Back to no clause at all — NOT to a CASCADE. Downgrading must never be the thing that makes a
    # `DELETE FROM users` able to take the restriction snapshots with it.
    _redeclare_user_fk(None)
