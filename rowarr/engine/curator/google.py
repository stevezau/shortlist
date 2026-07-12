"""Google curator — response_schema on generate_content (google-genai SDK)."""

from __future__ import annotations

import json

from rowarr.engine.curator.base import CuratorError, build_prompts, picks_schema, validate_picks
from rowarr.engine.models import Candidate, Pick, UserProfile

DEFAULT_MODEL = "gemini-2.5-flash"


class GoogleCurator:
    name = "google"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60.0):
        try:
            from google import genai
        except ImportError as e:
            raise ImportError("Google provider needs `pip install rowarr[google]`") from e
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self.last_tokens = 0

    def ping(self) -> str:
        r = self._client.models.generate_content(model=self._model, contents="Reply with the single word: ready")
        return r.text or ""

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        system, user = build_prompts(profile, candidates, k)
        try:
            r = self._client.models.generate_content(
                model=self._model,
                contents=user,
                config={
                    "system_instruction": system,
                    "response_mime_type": "application/json",
                    "response_json_schema": picks_schema(),
                },
            )
        except Exception as e:  # google-genai raises provider-specific exceptions
            raise CuratorError(f"Google error: {e}") from e
        usage = getattr(r, "usage_metadata", None)
        self.last_tokens = getattr(usage, "total_token_count", 0) or 0
        try:
            data = json.loads(r.text or "")
        except json.JSONDecodeError as e:
            raise CuratorError("Google returned unparseable JSON") from e
        return validate_picks(data.get("picks", []), candidates, k, self.name)
