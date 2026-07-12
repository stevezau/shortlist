"""Privacy Check tiers, exactly as validated live in Phase 0 (2026-07-12).

T1: read every restricted user's filters back from plex.tv and assert the expected
    rowarr excludes are present.
T2: mint a canary Home user's server token (switch + resources exchange) and assert that no
    OTHER user's rowarr collection appears among the canary's Home hubs. Detection is by
    collection id parsed from each hub's key (``/library/collections/<id>/children``) — hub
    payloads do not carry labels, and row titles are shared templates, so ids are the only
    reliable discriminator. See tests/fixtures/pms_hubs_home.json.
"""

from __future__ import annotations

import re

from loguru import logger

from rowarr.engine.clients.plex import PlexClient, PlexTvClient
from rowarr.engine.models import PrivacyCheckResult, UserProfile, UserType
from rowarr.engine.privacy import desired_excludes, rowarr_labels_in

_COLLECTION_KEY = re.compile(r"/library/collections/(\d+)")


def collection_id_from_hub(hub: dict) -> int | None:
    """Collection id behind a Home hub, or None for non-collection hubs."""
    match = _COLLECTION_KEY.search(str(hub.get("key") or hub.get("hubKey") or ""))
    return int(match.group(1)) if match else None


def check_t1(
    plextv: PlexTvClient,
    enabled_users: list[UserProfile],
    stored_labels: dict[str, str],
    *,
    label_prefix: str = "rowarr",
) -> PrivacyCheckResult:
    """Assert every non-owner user's share filters exclude every other user's existing label."""
    failures = {}
    remote = {u.id: u for u in plextv.list_users()}
    for user in enabled_users:
        if user.user_type is UserType.OWNER:
            continue
        wanted = desired_excludes(user, enabled_users, stored_labels)
        if not wanted:
            continue
        got = remote.get(user.plex_account_id)
        if got is None:
            failures[user.username] = "not found on plex.tv"
            continue
        for fieldname in ("filterMovies", "filterTelevision"):
            present = rowarr_labels_in(got.filters.get(fieldname, ""), label_prefix)
            missing = {w for w in wanted if w.lower() not in {p.lower() for p in present}}
            if missing:
                failures[user.username] = f"{fieldname} missing excludes: {sorted(missing)}"
    passed = not failures
    logger.info("Privacy Check T1: {}", "PASS" if passed else f"FAIL {failures}")
    return PrivacyCheckResult(tier="T1", passed=passed, detail=failures)


def check_t2(
    plex: PlexClient,
    plextv: PlexTvClient,
    canary: UserProfile,
    collections: dict[str, tuple[str, int]],
) -> PrivacyCheckResult:
    """Fetch Home hubs AS the canary; assert no other user's collection id appears."""
    token = plextv.canary_server_token(canary.plex_account_id)
    hubs = plex.user_hubs(token)
    foreign_ids = {rating_key: slug for slug, (_, rating_key) in collections.items() if slug != canary.slug}
    own = collections.get(canary.slug)

    leaked = []
    own_visible = False
    for hub in hubs:
        cid = collection_id_from_hub(hub)
        if cid is None:
            continue
        if cid in foreign_ids:
            leaked.append({"title": hub.get("title"), "collection_id": cid, "slug": foreign_ids[cid]})
        if own and cid == own[1]:
            own_visible = True

    detail = {
        "hub_count": len(hubs),
        "leaked": leaked,
        "own_row_visible": own_visible,
        "foreign_collections_checked": len(foreign_ids),
    }
    passed = not leaked
    logger.info("Privacy Check T2 ({}): {}", canary.username, "PASS" if passed else f"FAIL {leaked}")
    return PrivacyCheckResult(tier="T2", passed=passed, detail=detail)
