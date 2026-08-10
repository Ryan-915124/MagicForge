"""Application use cases for assistant, analysis, and creation."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING

from analysis.analyzer import MagicTheoryAnalyzer
from analysis.models import MagicTheoryAnalysis
from llm.glm_client import GLMClient
from retrieval.interfaces import (
    KnowledgeRetriever,
    RetrievalAuthorization,
    SearchResult,
    require_retrieval_authorization,
)
from retrieval.routing import MagicKnowledgeRouter

if TYPE_CHECKING:
    from app.runtime_corpus import ActiveCorpus


PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
GLM_47_ASSISTANT_MAX_TOKENS = 1_600


class PromptNotFoundError(RuntimeError):
    pass


class MagicForgeService:
    def __init__(
        self,
        llm: GLMClient,
        retriever: KnowledgeRetriever,
        retrieval_limit: int = 5,
        analyzer: MagicTheoryAnalyzer | None = None,
        router: MagicKnowledgeRouter | None = None,
        active_corpus: ActiveCorpus | None = None,
        runtime_preflight: Callable[[], None] | None = None,
    ) -> None:
        self.llm = llm
        self.retriever = retriever
        self.retrieval_limit = retrieval_limit
        self.analyzer = analyzer or MagicTheoryAnalyzer(llm)
        self.router = router or MagicKnowledgeRouter()
        self.active_corpus = active_corpus
        self.runtime_preflight = runtime_preflight

    def assistant(
        self,
        question: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> tuple[str, list[SearchResult]]:
        generation_options = {}
        if self.llm.model.strip().casefold() == "glm-4.7":
            generation_options = {
                "thinking_enabled": False,
                "max_tokens": GLM_47_ASSISTANT_MAX_TOKENS,
            }
        return self._generate(
            "assistant_prompt.txt",
            question,
            generation_options=generation_options,
            authorization=authorization,
        )

    def analyze(
        self,
        magic_description: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> tuple[MagicTheoryAnalysis, list[SearchResult]]:
        results = self._retrieve(magic_description, authorization=authorization)
        return self.analyzer.analyze(magic_description, results), results

    def create(
        self,
        requirements: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> tuple[str, list[SearchResult]]:
        return self._generate(
            "creator_prompt.txt",
            requirements,
            authorization=authorization,
        )

    def _generate(
        self,
        prompt_filename: str,
        user_input: str,
        *,
        generation_options: dict[str, object] | None = None,
        authorization: RetrievalAuthorization | None = None,
    ) -> tuple[str, list[SearchResult]]:
        results = self._retrieve(user_input, authorization=authorization)
        context = self._format_context(results)
        system_prompt = self._load_prompt(prompt_filename)
        prompt = (
            "Retrieved knowledge:\n"
            f"{context}\n\n"
            "User request:\n"
            f"{user_input}"
        )
        answer = self.llm.generate(
            prompt,
            system_prompt=system_prompt,
            **(generation_options or {}),
        )
        return answer, results

    def _retrieve(
        self,
        query: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> list[SearchResult]:
        if self.runtime_preflight is not None:
            self.runtime_preflight()
        auth = require_retrieval_authorization(authorization)
        ranked: dict[str, tuple[int, float, SearchResult]] = {}
        for channel in self.router.plan(query):
            results = self.retriever.search_documents(
                query,
                limit=self.retrieval_limit,
                filters=channel.filters,
                authorization=auth,
            )
            for result in results:
                identity = str(
                    result.payload.get("knowledge_unit_id")
                    or result.payload.get("artifact_id")
                    or result.text
                )
                candidate = (channel.priority, -result.score, result)
                existing = ranked.get(identity)
                if existing is None or candidate[:2] < existing[:2]:
                    ranked[identity] = candidate
        return [
            item[2]
            for item in sorted(ranked.values(), key=lambda value: value[:2])[
                : self.retrieval_limit
            ]
        ]

    @staticmethod
    def _format_context(results: list[SearchResult]) -> str:
        if not results:
            return "No relevant knowledge was retrieved. State uncertainty explicitly."
        sections = []
        for index, result in enumerate(results, start=1):
            title = result.payload.get("title") or "Untitled source"
            author = result.payload.get("author") or "Unknown author"
            evidence = _format_evidence_metadata(result)
            sections.append(
                f"[Source {index}: {title} — {author}; {evidence}]\n{result.text}"
            )
        return "\n\n".join(sections)

    @staticmethod
    def _load_prompt(filename: str) -> str:
        path = PROMPT_DIR / filename
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptNotFoundError(f"could not load prompt {filename}: {exc}") from exc


def _format_evidence_metadata(result: SearchResult) -> str:
    payload = result.payload
    metadata = [
        f"knowledge_type={payload.get('knowledge_type') or 'unknown'}",
        f"origin={payload.get('knowledge_origin') or 'unknown'}",
        f"evidence_level={payload.get('evidence_level') or 'unknown'}",
        f"confidence={payload.get('confidence_label') or 'unknown'}",
        f"contradiction={payload.get('contradiction_status') or 'unknown'}",
    ]
    limitations = payload.get("limitations") or []
    if isinstance(limitations, list) and limitations:
        metadata.append(
            "limitations=" + "; ".join(str(item) for item in limitations)
        )
    return "; ".join(metadata)
