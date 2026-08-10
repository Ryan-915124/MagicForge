import pytest

from retrieval.interfaces import RetrievalAuthorizationRequiredError, SearchResult
from retrieval.routing import MagicKnowledgeRouter
from app.service import MagicForgeService
from security.policy import bootstrap_anonymous_retrieval_authorization


AUTHORIZATION = bootstrap_anonymous_retrieval_authorization()


class FakeLLM:
    model = "glm-test"

    def generate(self, *args, **kwargs):
        return "answer"


class RecordingRetriever:
    def __init__(self) -> None:
        self.filters = []
        self.authorizations = []

    def search_documents(self, query, limit=5, filters=None, authorization=None):
        self.filters.append(filters)
        self.authorizations.append(authorization)
        channel = filters.knowledge_origins[0] if filters.knowledge_origins else "domain"
        return [
            SearchResult(
                text=f"Knowledge from {channel}",
                score=0.8,
                payload={"knowledge_unit_id": channel},
            )
        ]


class RecordingLLM:
    def __init__(self, model: str = "glm-4.7") -> None:
        self.model = model
        self.calls = []

    def generate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "answer"


def test_attention_question_uses_required_channel_order() -> None:
    channels = MagicKnowledgeRouter().plan(
        "Why didn't spectators notice my secret move in a card trick?"
    )

    assert [channel.name for channel in channels] == [
        "psychology-evidence",
        "misdirection-principles",
        "practitioner-applications",
        "technique-examples",
    ]
    assert channels[0].filters.knowledge_origins == ["scientific_evidence"]
    assert channels[0].filters.domains == ["card"]


def test_service_merges_channels_by_priority_not_raw_cosine_sum() -> None:
    retriever = RecordingRetriever()
    service = MagicForgeService(FakeLLM(), retriever, retrieval_limit=4)

    results = service._retrieve(
        "Why did nobody notice the secret move?",
        authorization=AUTHORIZATION,
    )

    assert results[0].payload["knowledge_unit_id"] == "scientific_evidence"
    assert len(retriever.filters) == 4
    assert all(item is AUTHORIZATION for item in retriever.authorizations)


def test_service_refuses_to_retrieve_without_explicit_authorization() -> None:
    retriever = RecordingRetriever()
    service = MagicForgeService(FakeLLM(), retriever)

    with pytest.raises(RetrievalAuthorizationRequiredError):
        service._retrieve("attention")

    assert retriever.filters == []


def test_generation_context_exposes_epistemic_metadata_to_glm() -> None:
    result = SearchResult(
        text="Knowledge type: evidence\nClaim: Attention is limited.",
        score=0.9,
        payload={
            "knowledge_type": "evidence",
            "knowledge_origin": "scientific_evidence",
            "evidence_level": "empirical",
            "confidence_label": "high",
            "contradiction_status": "none_found",
            "limitations": ["Task dependent."],
        },
    )

    context = MagicForgeService._format_context([result])

    assert "origin=scientific_evidence" in context
    assert "limitations=Task dependent." in context


def test_glm_47_assistant_disables_thinking_and_uses_smaller_response_budget() -> None:
    llm = RecordingLLM()
    service = MagicForgeService(llm, RecordingRetriever())

    service.assistant(
        "Explain theatrical framing",
        authorization=AUTHORIZATION,
    )

    _, options = llm.calls[0]
    assert options["thinking_enabled"] is False
    assert options["max_tokens"] == 1_600


def test_create_keeps_default_generation_behavior() -> None:
    llm = RecordingLLM()
    service = MagicForgeService(llm, RecordingRetriever())

    service.create(
        "Create a short parlor effect",
        authorization=AUTHORIZATION,
    )

    _, options = llm.calls[0]
    assert "thinking_enabled" not in options
    assert "max_tokens" not in options
