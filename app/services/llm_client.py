"""Thin wrapper around the official OpenAI or Anthropic SDK. Raises LLMUnavailable with a safe, client-facing code."""

from __future__ import annotations

import logging
from typing import Any, TypeVar

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from app.config import Settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

DEFAULT_MODELS = {"openai": "gpt-4.1-mini", "anthropic": "claude-haiku-5-5"}
KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


class LLMUnavailable(Exception):
    """code: not_configured | timeout | rate_limited | provider_error | refused | invalid_output"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail  # logged only, never returned to clients


def _provider_error(exc: Exception) -> LLMUnavailable:
    # The provider's own message (e.g. low credit balance) is server-log only; clients get the code.
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]  # Anthropic nests the error; OpenAI's body is the error itself
    detail = body.get("message") if isinstance(body, dict) else None
    log.warning("LLM provider error: %s %s", type(exc).__name__, (detail or "")[:200])
    return LLMUnavailable("provider_error", type(exc).__name__)


class LLMClient:
    def __init__(self, settings: Settings, client: Any | None = None):
        self.provider = settings.llm_provider
        self.model = settings.llm_model or DEFAULT_MODELS[self.provider]
        self.max_tokens = settings.llm_max_tokens
        self._client = client
        if client is None and settings.llm_configured:
            common = {"api_key": settings.llm_api_key, "timeout": settings.llm_timeout_seconds, "max_retries": settings.llm_max_retries}
            if self.provider == "openai":
                self._client = openai.OpenAI(**common)
            else:
                ws = settings.anthropic_workspace_id
                self._client = anthropic.Anthropic(**common, default_headers={"anthropic-workspace-id": ws} if ws else None)

    @property
    def configured(self) -> bool:
        return self._client is not None

    def generate(self, system: str, user: str, schema: type[T]) -> T:
        """One structured-output call: the response is constrained to `schema` and validated by the SDK."""
        if self._client is None:
            raise LLMUnavailable("not_configured", f"LLM disabled or {KEY_NAMES[self.provider]} is not set")
        if self.provider == "openai":
            return self._openai(system, user, schema)
        return self._anthropic(system, user, schema)

    def _openai(self, system: str, user: str, schema: type[T]) -> T:
        try:
            resp = self._client.chat.completions.parse(
                model=self.model,
                max_completion_tokens=self.max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
            )
        except openai.APITimeoutError as exc:  # subclass of APIConnectionError: catch first
            raise LLMUnavailable("timeout", str(exc)) from exc
        except openai.RateLimitError as exc:
            raise LLMUnavailable("rate_limited", str(exc)) from exc
        except openai.LengthFinishReasonError as exc:
            raise LLMUnavailable("invalid_output", "response truncated at max tokens") from exc
        except openai.ContentFilterFinishReasonError as exc:
            raise LLMUnavailable("refused", "content filter") from exc
        except openai.APIError as exc:  # auth, quota, bad request, 5xx, connection
            raise _provider_error(exc) from exc
        except ValidationError as exc:  # output did not satisfy the schema's own constraints
            raise LLMUnavailable("invalid_output", f"schema validation failed: {exc.error_count()} errors") from exc
        msg = resp.choices[0].message
        if msg.refusal:
            raise LLMUnavailable("refused", "model declined the request")
        if msg.parsed is None:
            raise LLMUnavailable("invalid_output", "no parsed output")
        return msg.parsed

    def _anthropic(self, system: str, user: str, schema: type[T]) -> T:
        try:
            resp = self._client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except anthropic.APITimeoutError as exc:  # subclass of APIConnectionError: catch first
            raise LLMUnavailable("timeout", str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable("rate_limited", str(exc)) from exc
        except anthropic.APIError as exc:  # auth, bad request, 5xx, connection
            raise _provider_error(exc) from exc
        except ValidationError as exc:
            raise LLMUnavailable("invalid_output", f"schema validation failed: {exc.error_count()} errors") from exc
        if resp.stop_reason == "refusal":
            raise LLMUnavailable("refused", "model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMUnavailable("invalid_output", "response truncated at max_tokens")
        if resp.parsed_output is None:
            raise LLMUnavailable("invalid_output", "no parsed output")
        return resp.parsed_output
