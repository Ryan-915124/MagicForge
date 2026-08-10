"""Deterministic domain-channel routing for magic intelligence retrieval."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from retrieval.interfaces import KnowledgeSearchFilter


class RetrievalChannel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    priority: int = Field(ge=1)
    filters: KnowledgeSearchFilter


class MagicKnowledgeRouter:
    """Route common method-concealment questions through explicit evidence channels."""

    _ATTENTION_TERMS = {
        "notice",
        "noticed",
        "attention",
        "misdirection",
        "secret move",
        "didn't see",
        "did not see",
        "注意",
        "没看到",
        "没有看到",
        "误导",
        "秘密动作",
    }

    def plan(self, query: str) -> list[RetrievalChannel]:
        normalized = query.casefold()
        domains = _domains_for_query(normalized)
        if not any(term in normalized for term in self._ATTENTION_TERMS):
            return [
                RetrievalChannel(
                    name="general-reviewed-knowledge",
                    priority=1,
                    filters=KnowledgeSearchFilter(domains=domains),
                )
            ]
        return [
            RetrievalChannel(
                name="psychology-evidence",
                priority=1,
                filters=KnowledgeSearchFilter(
                    knowledge_types=["evidence", "psychology"],
                    domains=domains,
                    knowledge_origins=["scientific_evidence"],
                    evidence_levels=["review", "empirical"],
                ),
            ),
            RetrievalChannel(
                name="misdirection-principles",
                priority=2,
                filters=KnowledgeSearchFilter(
                    knowledge_types=["psychology", "performance"],
                    domains=domains,
                    ontology_paths=[
                        "misdirection.spatial",
                        "misdirection.temporal",
                        "misdirection.social",
                        "misdirection.cognitive",
                        "misdirection.emotional",
                    ],
                ),
            ),
            RetrievalChannel(
                name="practitioner-applications",
                priority=3,
                filters=KnowledgeSearchFilter(
                    knowledge_types=["performance"],
                    domains=domains,
                    knowledge_origins=["expert_practice"],
                    evidence_levels=["practitioner"],
                ),
            ),
            RetrievalChannel(
                name="technique-examples",
                priority=4,
                filters=KnowledgeSearchFilter(
                    knowledge_types=["technique"],
                    domains=domains,
                ),
            ),
        ]


def _domains_for_query(query: str) -> list[str]:
    mappings = {
        "card": ("card", "cards", "playing card", "纸牌", "扑克牌"),
        "close-up": ("close-up", "close up", "近景"),
        "stage": ("stage", "舞台"),
        "mentalism": ("mentalism", "mind reading", "心灵", "读心"),
    }
    return [
        domain
        for domain, terms in mappings.items()
        if any(term in query for term in terms)
    ]
