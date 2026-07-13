"""The automated Privacy Check probe — wizard step 5, productized from the live Phase 0 test.

Creates a throwaway labeled collection, promotes it, excludes its label on a canary Home
user, and verifies with the canary's own eyes (their /hubs) that the row disappears.
Everything is cleaned up in ``finally`` — probe artifacts never outlive the check
(plex-safety rule 7), and the canary's filters are restored byte-identical.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from loguru import logger

from rowarr.engine.clients.plex_pms import PlexClient
from rowarr.engine.clients.plextv import PlexTvClient
from rowarr.engine.models import FilterSnapshot, PrivacyCheckResult, UserProfile
from rowarr.engine.privacy import SnapshotStore, merge_label_excludes, rowarr_labels_in
from rowarr.engine.verify import collection_id_from_hub

PROBE_TITLE = "Rowarr Privacy Probe"
PROBE_LABEL = "rowarr_probe"


def _canary_sees_collection(plex: PlexClient, token: str, collection_id: int) -> bool:
    return any(collection_id_from_hub(h) == collection_id for h in plex.user_hubs(token))


def run_privacy_probe(
    plex: PlexClient,
    plextv: PlexTvClient,
    canary: UserProfile,
    snapshots: SnapshotStore,
    *,
    visible_timeout_s: float = 60,
    hidden_timeout_s: float = 90,
    poll_interval_s: float = 10,
    on_step: Callable[[str], None] | None = None,
    sleep=time.sleep,
) -> PrivacyCheckResult:
    """Run the end-to-end probe against a live server. ~60-90 seconds, fully reversible.

    Steps (each reported via on_step for the wizard's live log):
      1. create probe collection (2 oldest movies), label, promote to shared Home
      2. baseline: canary CAN see the probe row
      3. SNAPSHOT the canary's filters, then merge label!=<probe> (T1 read-back inside)
      4. canary can NO LONGER see the probe row (the actual privacy proof)
      5. finally: restore filters byte-identical, delete probe

    The snapshot in step 3 is not belt-and-braces: `finally` covers exceptions but not process
    death, and merges never prune. Without a persisted snapshot, a crash inside the ~90s window
    would leave `label!=Rowarr_probe` on a real user's share forever — and the first real run
    would then capture that contaminated state as the "pre-Rowarr" snapshot, so even uninstall
    could not undo it (plex-safety rule 2).
    """

    def step(message: str) -> None:
        logger.info("privacy probe: {}", message)
        if on_step:
            on_step(message)

    detail: dict = {"steps": []}
    collection = None
    before: dict[str, str] | None = None
    section = next((s for s in plex.sections() if s.type == "movie"), None)
    if section is None:
        return PrivacyCheckResult(tier="PROBE", passed=False, detail={"error": "no movie library found"})

    try:
        # Related hubs only render for collections with >=2 items (Phase 0 finding).
        items = section.search(sort="addedAt:asc", limit=2)
        if len(items) < 2:
            return PrivacyCheckResult(tier="PROBE", passed=False, detail={"error": "need at least 2 movies"})
        step("creating probe collection")
        collection = plex.create_collection(section, PROBE_TITLE, items)
        stored_label = plex.stored_label(collection, PROBE_LABEL)
        plex.promote(collection, shared=True)
        probe_id = collection.ratingKey
        detail["probe_collection_id"] = probe_id

        step("checking the canary can see the probe row (baseline)")
        token = plextv.canary_server_token(canary.plex_account_id)
        deadline = time.monotonic() + visible_timeout_s
        baseline_visible = False
        while time.monotonic() < deadline:
            if _canary_sees_collection(plex, token, probe_id):
                baseline_visible = True
                break
            sleep(poll_interval_s)
        detail["baseline_visible"] = baseline_visible
        if not baseline_visible:
            return PrivacyCheckResult(
                tier="PROBE",
                passed=False,
                detail={**detail, "error": "probe row never appeared on the canary's Home — promotion problem"},
            )

        step("excluding the probe label on the canary's share")
        remote = plextv.get_user(canary.plex_account_id)
        before = dict(remote.filters)
        if snapshots.get(canary.plex_account_id) is None:
            snapshots.save(
                FilterSnapshot(
                    plex_account_id=canary.plex_account_id,
                    username=canary.username,
                    taken_at=datetime.now(UTC),
                    filters=before,
                )
            )
            logger.info("privacy probe: snapshot persisted before touching {}'s share", canary.username)
        changed = {
            field: merge_label_excludes(remote.filters[field], {stored_label})
            for field in ("filterMovies", "filterTelevision")
        }
        plextv.update_user_filters(canary.plex_account_id, changed)
        readback = plextv.get_user(canary.plex_account_id)
        t1_ok = all(
            stored_label.lower() in {v.lower() for v in rowarr_labels_in(readback.filters[f], "rowarr")}
            for f in ("filterMovies", "filterTelevision")
        )
        detail["t1_filter_persisted"] = t1_ok
        if not t1_ok:
            return PrivacyCheckResult(
                tier="PROBE", passed=False, detail={**detail, "error": "exclusion did not persist on plex.tv"}
            )

        step("waiting for the probe row to disappear for the canary")
        token = plextv.canary_server_token(canary.plex_account_id)  # fresh token post-filter
        deadline = time.monotonic() + hidden_timeout_s
        hidden = False
        while time.monotonic() < deadline:
            if not _canary_sees_collection(plex, token, probe_id):
                hidden = True
                break
            sleep(poll_interval_s)
        detail["hidden_after_exclusion"] = hidden
        passed = hidden
        message = (
            "Your server keeps rows private ✅"
            if passed
            else "The probe row stayed visible to the canary — label restrictions are not working here"
        )
        detail["message"] = message
        step(message)
        return PrivacyCheckResult(tier="PROBE", passed=passed, detail=detail)
    finally:
        try:
            if before is not None:
                step("restoring the canary's filters")
                current = plextv.get_user(canary.plex_account_id).filters
                diverged = {k: before[k] for k in before if current.get(k, "") != before[k]}
                if diverged:
                    plextv.update_user_filters(canary.plex_account_id, diverged)
        except Exception:
            logger.exception("privacy probe: filter restore FAILED — restore manually from plex.tv sharing settings")
        try:
            if collection is not None:
                step("deleting the probe collection")
                plex.delete_owned_collection(collection, "rowarr")
        except Exception:
            logger.exception("privacy probe: probe collection cleanup FAILED — delete {!r} manually", PROBE_TITLE)
