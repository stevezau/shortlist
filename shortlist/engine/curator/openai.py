"""OpenAI curator — JSON-schema response_format on chat.completions."""

from __future__ import annotations

import json

from shortlist.engine.curator.base import CuratorError, build_prompts, picks_schema, validate_picks
from shortlist.engine.models import Candidate, Pick, UserProfile

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAICurator:
    name = "openai"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60.0):
        try:
            import openai
        except ImportError as e:
            raise ImportError("OpenAI provider needs `pip install shortlist[openai]`") from e
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=2)
        self._model = model
        self.last_tokens = 0

    def ping(self) -> str:
        r = self._client.chat.completions.create(
            model=self._model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        )
        return r.choices[0].message.content or ""

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        import openai

        system, user = build_prompts(profile, candidates, k)
        try:
            r = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "picks", "strict": True, "schema": picks_schema()},
                },
            )
        except openai.OpenAIError as e:
            raise CuratorError(f"OpenAI error: {e}") from e
        usage = getattr(r, "usage", None)
        self.last_tokens = (usage.total_tokens or 0) if usage else 0
        try:
            data = json.loads(r.choices[0].message.content or "")
        except json.JSONDecodeError as e:
            raise CuratorError("OpenAI returned unparseable JSON") from e
        return validate_picks(data.get("picks", []), candidates, k, self.name)
