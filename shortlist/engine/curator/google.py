"""Google curator — response_schema on generate_content (google-genai SDK)."""

from __future__ import annotations

import json
import time

from shortlist.engine.curator.base import (
    CuratorError,
    build_prompts,
    log_curate_request,
    log_curate_response,
    picks_schema,
    validate_picks,
)
from shortlist.engine.models import Candidate, Pick, UserProfile

DEFAULT_MODEL = "gemini-2.5-flash"


class GoogleCurator:
    name = "google"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60.0):
        try:
            from google import genai
        except ImportError as e:
            raise ImportError("Google provider needs `pip install shortlist[google]`") from e
        # google-genai's HttpOptions.timeout is in MILLISECONDS; without this the constructor's
        # timeout was silently dropped, so a stalled Gemini call was bounded only by the SDK default.
        self._client = genai.Client(api_key=api_key, http_options={"timeout": int(timeout * 1000)})
        self._model = model
        self.last_tokens = 0

    def ping(self) -> str:
        r = self._client.models.generate_content(model=self._model, contents="Reply with the single word: ready")
        return r.text or ""

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        system, user = build_prompts(profile, candidates, k)
        log_curate_request(self.name, self._model, system, user, len(candidates), k)
        started = time.monotonic()
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
        text = r.text or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CuratorError("Google returned unparseable JSON") from e
        picks = validate_picks(data.get("picks", []), candidates, k, self.name)
        log_curate_response(self.name, self._model, len(picks), self.last_tokens, time.monotonic() - started, text)
        return picks
