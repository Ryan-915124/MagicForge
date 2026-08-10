"""Official Z.AI SDK adapter for GLM chat completion."""

from __future__ import annotations

import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError


StructuredModel = TypeVar("StructuredModel", bound=BaseModel)

DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_RETRIES = 0


class GLMConfigurationError(RuntimeError):
    pass


class GLMAPIError(RuntimeError):
    pass


class GLMClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: Any | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._client = client

    def _get_client(self):
        if not self.api_key:
            raise GLMConfigurationError(
                "GLM_API_KEY is not configured; add it to .env before generation"
            )
        if self._client is None:
            try:
                from zai import ZhipuAiClient
            except ImportError as exc:  # pragma: no cover - installation issue
                raise GLMConfigurationError(
                    "zai-sdk is not installed; run `pip install -r requirements.txt`"
                ) from exc
            self._client = ZhipuAiClient(
                api_key=self.api_key,
                timeout=self.timeout_seconds,
                max_retries=self.max_retries,
            )
        return self._client

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.5,
        max_tokens: int = 2_000,
        json_mode: bool = False,
        thinking_enabled: bool | None = None,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if json_mode:
                request["response_format"] = {"type": "json_object"}
            if thinking_enabled is not None:
                request["thinking"] = {
                    "type": "enabled" if thinking_enabled else "disabled"
                }
            response = self._get_client().chat.completions.create(
                **request,
            )
            content = response.choices[0].message.content
        except GLMConfigurationError:
            raise
        except Exception as exc:
            raise GLMAPIError(f"GLM request failed: {exc}") from exc

        if not content:
            raise GLMAPIError("GLM returned an empty response")
        return str(content)

    def generate_structured(
        self,
        prompt: str,
        response_model: type[StructuredModel],
        *,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2_000,
        thinking_enabled: bool | None = None,
    ) -> StructuredModel:
        """Generate JSON and validate it against a Pydantic model."""

        content = self.generate(
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            thinking_enabled=thinking_enabled,
        )
        cleaned = _strip_json_fence(content)
        try:
            return response_model.model_validate_json(cleaned)
        except ValidationError as exc:
            raise GLMAPIError(f"GLM returned invalid structured data: {exc}") from exc


def _strip_json_fence(content: str) -> str:
    match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", content, re.DOTALL)
    return match.group(1) if match else content.strip()
