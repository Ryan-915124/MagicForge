import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from llm.glm_client import GLMAPIError, GLMClient, GLMConfigurationError


class FakeCompletions:
    def __init__(self, content: str | None = "Generated answer") -> None:
        self.content = content
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def test_generate_uses_configured_model_and_system_prompt() -> None:
    completions = FakeCompletions()
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = GLMClient("test-key", "glm-test", client=sdk)

    result = client.generate("Question", system_prompt="System")

    assert result == "Generated answer"
    assert completions.kwargs["model"] == "glm-test"
    assert completions.kwargs["messages"] == [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Question"},
    ]


def test_sdk_client_uses_explicit_timeout_and_retry_budget(monkeypatch) -> None:
    constructed = {}

    class RecordingSDK:
        def __init__(self, **kwargs) -> None:
            constructed.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "zai",
        SimpleNamespace(ZhipuAiClient=RecordingSDK),
    )

    client = GLMClient(
        "test-key",
        "glm-test",
        timeout_seconds=45.0,
        max_retries=1,
    )

    assert isinstance(client._get_client(), RecordingSDK)
    assert constructed == {
        "api_key": "test-key",
        "timeout": 45.0,
        "max_retries": 1,
    }


def test_missing_key_has_clear_configuration_error() -> None:
    with pytest.raises(GLMConfigurationError, match="GLM_API_KEY"):
        GLMClient("", "glm-test").generate("Question")


def test_empty_response_is_rejected() -> None:
    completions = FakeCompletions(content=None)
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    with pytest.raises(GLMAPIError, match="empty response"):
        GLMClient("test-key", "glm-test", client=sdk).generate("Question")


class StructuredAnswer(BaseModel):
    score: int


def test_generate_structured_strips_fence_and_validates_json() -> None:
    completions = FakeCompletions(content='```json\n{"score": 4}\n```')
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = GLMClient("test-key", "glm-test", client=sdk).generate_structured(
        "Question", StructuredAnswer
    )

    assert result.score == 4
    assert completions.kwargs["response_format"] == {"type": "json_object"}
    assert completions.kwargs["max_tokens"] == 2_000
    assert "thinking" not in completions.kwargs


def test_generate_structured_rejects_invalid_json() -> None:
    completions = FakeCompletions(content="not json")
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(GLMAPIError, match="invalid structured data"):
        GLMClient("test-key", "glm-test", client=sdk).generate_structured(
            "Question", StructuredAnswer
        )
