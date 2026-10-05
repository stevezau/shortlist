"""A row's AI instructions decide its pool and recipe only when it uses AI web search (#138)."""

from __future__ import annotations

from unittest.mock import MagicMock

from shortlist.engine.context import EngineContext
from shortlist.engine.models import EngineConfig, RowSpec
from shortlist.engine.rows import RowPolicy, _rating_key_resolver, row_recipe
from shortlist.engine.web_guidance import AiInstructions
from tests.conftest import MemorySnapshotStore, make_profile


def make_policy(*, sources: list[str], cfg_overrides: dict | None = None, curator=None) -> RowPolicy:
    """A RowPolicy for one person whose effective sources are ``sources``, built as
    `test_seasonal_rows.py::TestWhatDecidesARebuild` builds its own."""
    cfg = EngineConfig(
        row_size=5,
        min_history=3,
        candidates_pre_rank=10,
        max_seeds=10,
        candidate_sources=list(sources),
        **(cfg_overrides or {}),
    )
    ctx = EngineContext(
        config=cfg,
        plex=MagicMock(),
        plextv=MagicMock(),
        tmdb=MagicMock(),
        history_source=MagicMock(),
        curator=curator if curator is not None else MagicMock(),
        snapshots=MemorySnapshotStore(),
    )
    return RowPolicy(
        ctx=ctx,
        user=make_profile("sarah", account_id=100),
        cfg=ctx.config,
        specs=[],
        library_index={},
        report=MagicMock(),
        resolve=_rating_key_resolver({}),
    )


class TestNothingChangesWithoutInstructions:
    def test_a_row_with_no_instructions_keeps_its_recipe_and_pool_key_byte_for_byte(self):
        policy = make_policy(sources=["tmdb_similar", "llm_web"])
        plain = RowSpec(slug="plain", name_template="Plain", size=5)
        explicit_default = RowSpec(slug="plain", name_template="Plain", size=5, ai_instructions=None)
        assert row_recipe(policy, plain) == row_recipe(policy, explicit_default)
        assert "guide=" not in row_recipe(policy, plain)
        assert policy.pool_key(plain) == policy.pool_key(explicit_default)

    def test_the_recipe_is_the_one_stored_before_instructions_existed(self):
        """Captured from the code before #138. A stored recipe that no longer matches rebuilds the row, so a
        drift here would rebuild every AI web search row on every server the night this ships."""
        policy = make_policy(sources=["tmdb_similar", "llm_web"])
        plain = RowSpec(slug="plain", name_template="Plain", size=5)
        assert row_recipe(policy, plain) == "both||llm_web,tmdb_similar|0.0|0.0|False|False|10|1|popular"

    def test_instructions_on_a_row_without_ai_web_search_change_nothing(self):
        policy = make_policy(sources=["tmdb_similar"])
        plain = RowSpec(slug="r", name_template="R", size=5)
        told = RowSpec(slug="r", name_template="R", size=5, ai_instructions=AiInstructions("own", "Any decade."))
        assert row_recipe(policy, told) == row_recipe(policy, plain)
        assert policy.pool_key(told) == policy.pool_key(plain)


class TestInstructionsSplitTheirRow:
    def test_two_rows_that_differ_only_in_instructions_get_separate_pools(self):
        policy = make_policy(sources=["tmdb_similar", "llm_web"])
        a = RowSpec(slug="a", name_template="A", size=5)
        b = RowSpec(slug="b", name_template="B", size=5, ai_instructions=AiInstructions("add", "No kids films."))
        assert policy.pool_key(a) != policy.pool_key(b)

    def test_changing_a_rows_instructions_changes_its_recipe(self):
        policy = make_policy(sources=["llm_web"])
        one = RowSpec(slug="r", name_template="R", size=5, ai_instructions=AiInstructions("add", "One."))
        two = RowSpec(slug="r", name_template="R", size=5, ai_instructions=AiInstructions("add", "Two."))
        assert row_recipe(policy, one) != row_recipe(policy, two)
        assert row_recipe(policy, one).endswith("guide=" + policy.effective_guidance(one).fingerprint())

    def test_server_wide_text_changes_every_ai_web_search_rows_recipe(self):
        plain = RowSpec(slug="r", name_template="R", size=5)
        assert row_recipe(
            make_policy(sources=["llm_web"], cfg_overrides={"web_instructions": "Favour classics."}), plain
        ) != (row_recipe(make_policy(sources=["llm_web"]), plain))


class _NativeCurator:
    """A curator with its own web search, recording the guidance each call was handed."""

    name = "native"
    supports_native_web_search = True
    last_tokens = 0
    last_output_tokens = 0

    def __init__(self):
        self.guidance_seen: list = []

    def recommend_web(self, profile, seeds, k, *, guidance=None):
        self.guidance_seen.append(guidance)
        return []

    def complete(self, system, user):
        raise AssertionError("native search never asks for a plain completion")


class TestTheRowsGuidanceReachesItsGather:
    def test_a_rows_instructions_are_what_its_web_search_is_handed(self):
        """The wiring from a row to its gather: `pools_for` -> `_candidate_pool` -> `gather_candidates`. Drop
        any link and the row's search silently runs on the built-in prompt."""
        curator = _NativeCurator()
        policy = make_policy(sources=["llm_web"], cfg_overrides={"web_search_provider": "native"}, curator=curator)
        spec = RowSpec(slug="r", name_template="R", size=5, ai_instructions=AiInstructions("add", "No kids films."))
        policy.specs = [spec]

        policy.pools_for(spec)

        assert not policy.effective_guidance(spec).is_builtin
        assert curator.guidance_seen == [policy.effective_guidance(spec)]
