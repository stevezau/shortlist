"""Resolve one row's effective request config from the global config plus that row's overrides.

The rule the whole per-row feature rests on: **ceilings that protect a shared resource stay global,
settings that express taste go per-row.** A row may make itself more restrictive and never less, so
whatever a row asks for, the run still cannot exceed ``max_per_run`` requests or the rating-lookup
budget derived from it.
"""

from __future__ import annotations

from dataclasses import replace

from shortlist.engine.models import ArrTarget, RequestConfig, RequestOverrides


def resolve_request_config(base: RequestConfig, overrides: RequestOverrides | None) -> RequestConfig:
    """This row's effective config: ``base`` with every set field of ``overrides`` applied.

    Args:
        base: The owner's global request config, as built from settings. Never mutated — every row
            resolves from the same instance, so mutating it would leak one row's floors into the next.
        overrides: The row's own settings, or None to inherit everything.

    Returns:
        A new ``RequestConfig``. ``max_per_run`` is always the global value; the row's own limit
        travels separately as ``max_per_row`` for the allocator to apply.
    """
    if overrides is None:
        return replace(base)

    def pick(value, fallback):
        """None means inherit. Compared against None, never falsiness: 0 and False are real
        choices here — `min_year=0` means 'no lower bound' and `auto_send=False` means 'queue it'."""
        return fallback if value is None else value

    row_cap = pick(overrides.max_per_row, base.max_per_run)
    return replace(
        base,
        radarr=_retarget(base.radarr, overrides.radarr_quality_profile_id, overrides.radarr_root_folder),
        sonarr=_retarget(base.sonarr, overrides.sonarr_quality_profile_id, overrides.sonarr_root_folder),
        sonarr_monitor=pick(overrides.sonarr_monitor, base.sonarr_monitor),
        min_rating=pick(overrides.min_rating, base.min_rating),
        min_votes=pick(overrides.min_votes, base.min_votes),
        min_demand=pick(overrides.min_demand, base.min_demand),
        min_year=pick(overrides.min_year, base.min_year),
        max_year=pick(overrides.max_year, base.max_year),
        auto_send=pick(overrides.auto_send, base.auto_send),
        auto_min_demand=pick(overrides.auto_min_demand, base.auto_min_demand),
        auto_min_rating=pick(overrides.auto_min_rating, base.auto_min_rating),
        # Clamped, not trusted: a row asking for more than the run allows gets the run's number. The
        # UI should not offer it, but the ceiling cannot depend on the UI for its enforcement.
        max_per_row=min(max(0, row_cap), base.max_per_run),
    )


def _retarget(target: ArrTarget | None, quality_profile_id: int | None, root_folder: str | None) -> ArrTarget | None:
    """``target`` with this row's profile/folder applied, keeping its URL, key and tag.

    None in, None out — and that is load-bearing. An unconfigured Arr has no URL and no API key, so
    building a target from a row override alone would produce one pointing nowhere that
    ``_request_one`` would then dutifully try to send to.
    """
    if target is None:
        return None
    if quality_profile_id is None and not root_folder:
        return target
    return replace(
        target,
        quality_profile_id=target.quality_profile_id if quality_profile_id is None else quality_profile_id,
        root_folder=root_folder or target.root_folder,
    )
