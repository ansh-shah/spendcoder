"""OpenRouter backend: strict-schema chat completions plus the Decisions API."""
from __future__ import annotations

import http.client
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from typing import Any

from ..config import LLMConfig
from .base import BackendError, InvalidAnswer, Ledger, validate_chat, validate_decision

BASE_URL = "https://openrouter.ai/api"
CHAT_URL = f"{BASE_URL}/v1/chat/completions"
DECISIONS_URL = f"{BASE_URL}/alpha/decisions"
MODELS_URL = f"{BASE_URL}/v1/models?output_modalities=decisions"
INVALID_ATTEMPTS = 3  # first try + 2 more when the model returns an invalid answer

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def parse_json_content(content: Any) -> Any:
    """Parse model text as JSON, tolerating a ```json fence."""
    if not isinstance(content, str):
        raise InvalidAnswer("model returned no text content")
    text = content.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidAnswer(f"model output is not valid JSON: {e}") from e


class OpenRouterBackend:
    name = "openrouter"

    def __init__(self, cfg: LLMConfig, ledger: Ledger, api_key: str | None = None) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise BackendError(
                "OPENROUTER_API_KEY is not set. Put it in the project's .env file or the environment "
                "(get a key at https://openrouter.ai/keys), or run with --backend mock / agent-files."
            )

    # ----------------------------------------------------------------- HTTP
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/spendcoder/spendcoder",
            "X-Title": "spendcoder",
        }

    def _request(self, url: str, body: dict | None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        method = "POST" if body is not None else "GET"
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
            delay = min(30.0, 2.0 ** attempt) * (0.5 + random.random())
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
                if e.code in (408, 429) or e.code >= 500:
                    last = BackendError(f"HTTP {e.code} from {url}: {text[:300]}")
                    retry_after = (e.headers or {}).get("Retry-After") if hasattr(e, "headers") else None
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, min(60.0, float(retry_after)))
                else:
                    raise BackendError(f"HTTP {e.code} from {url}: {text[:300]}") from e
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
                last = BackendError(f"network error calling {url}: {e}")
            except json.JSONDecodeError as e:
                last = BackendError(f"non-JSON response from {url}: {e}")
            if attempt < self.cfg.max_retries:
                time.sleep(delay)
        raise BackendError(f"giving up on {url} after {self.cfg.max_retries + 1} attempts: {last}")

    def _post_json(self, url: str, body: dict) -> dict:
        return self._request(url, body)

    def _get_json(self, url: str) -> dict:
        return self._request(url, None)

    # ------------------------------------------------------------ primitives
    def _chat_call(self, body: dict, purpose: str) -> str:
        resp = self._post_json(CHAT_URL, body)
        usage = resp.get("usage") or {}
        self.ledger.record(
            purpose,
            cost=usage.get("cost") or 0.0,
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
        )
        try:
            return resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            err = resp.get("error") if isinstance(resp, dict) else None
            raise InvalidAnswer(f"unexpected chat response shape ({err or 'no choices'})") from e

    def chat_json(self, *, system: str, user: str, schema: dict, schema_name: str, purpose: str) -> dict:
        body = {
            "model": self.cfg.chat_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            "reasoning": {"effort": self.cfg.reasoning_effort, "exclude": True},
            "usage": {"include": True},
        }
        last: InvalidAnswer | None = None
        for _ in range(INVALID_ATTEMPTS):
            try:
                return validate_chat(parse_json_content(self._chat_call(body, purpose)), schema)
            except InvalidAnswer as e:
                last = e
        raise InvalidAnswer(f"{purpose}: no valid answer after {INVALID_ATTEMPTS} attempts: {last}")

    def decide(self, *, state: dict, questions: dict, purpose: str) -> dict:
        body = {"model": self.cfg.decision_model, "state": state, "questions": questions}
        last: InvalidAnswer | None = None
        for _ in range(INVALID_ATTEMPTS):
            resp = self._post_json(DECISIONS_URL, body)
            usage = resp.get("usage") or {}
            self.ledger.record(
                purpose,
                cost=usage.get("cost") or 0.0,
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
            )
            try:
                return validate_decision(resp.get("answers"), questions)
            except InvalidAnswer as e:
                last = e
        raise InvalidAnswer(f"{purpose}: no valid decision after {INVALID_ATTEMPTS} attempts: {last}")

    def chat_text(self, *, prompt: str, purpose: str) -> str:
        body = {
            "model": self.cfg.reflection_model,
            "messages": [{"role": "user", "content": prompt}],
            "reasoning": {"effort": self.cfg.reasoning_effort, "exclude": True},
            "usage": {"include": True},
        }
        content = self._chat_call(body, purpose)
        if not isinstance(content, str):
            raise BackendError("model returned no text content")
        return content

    def list_decision_models(self) -> list[dict]:
        return list(self._get_json(MODELS_URL).get("data", []))
