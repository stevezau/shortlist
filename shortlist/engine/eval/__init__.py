"""Measuring whether a ranking change actually helps.

Pure, like the rest of `shortlist/engine/` — no imports from `shortlist/server/`, and nothing in here
ever WRITES to Plex or plex.tv. See `replay.py`.
"""

from shortlist.engine.eval.replay import HoldoutCase, ReplayOutcome, holdout_cases, replay_case

__all__ = ["HoldoutCase", "ReplayOutcome", "holdout_cases", "replay_case"]
