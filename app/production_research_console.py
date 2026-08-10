"""Stable Production Research Console adapter for the v0.1 backend milestone."""

from __future__ import annotations

from app.research_console_read_model import ResearchConsoleReadModelError
from app.runtime_corpus import ActiveCorpus
from knowledge.governance import MagicForgeMode


class ProductionResearchConsoleReadModel:
    """Keep the product runtime ready while the console contract stays Bootstrap-only."""

    def __init__(self, active_corpus: ActiveCorpus) -> None:
        if active_corpus.mode != MagicForgeMode.PRODUCTION:
            raise ResearchConsoleReadModelError(
                "the Production console adapter requires a Production corpus"
            )
        self.active_corpus = active_corpus

    def snapshot(self, _settings):
        raise ResearchConsoleReadModelError(
            "The Production Research Console is not available in this Alpha.",
            code="alpha_feature_unavailable",
            status_code=501,
        )


__all__ = ["ProductionResearchConsoleReadModel"]
