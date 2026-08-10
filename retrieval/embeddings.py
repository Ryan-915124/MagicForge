"""Replaceable embedding provider boundary and FastEmbed implementation."""

from __future__ import annotations

from functools import cached_property
from typing import Protocol, Sequence


class EmbeddingError(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedProvider:
    """Local CPU embeddings; replace by implementing ``EmbeddingProvider``."""

    def __init__(self, model_name: str, dimension: int) -> None:
        self.model_name = model_name
        self._dimension = dimension

    @cached_property
    def _model(self):  # type annotation would force an eager optional import
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - installation issue
            raise EmbeddingError(
                "fastembed is not installed; run `pip install -r requirements.txt`"
            ) from exc
        return TextEmbedding(model_name=self.model_name)

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vectors = [vector.tolist() for vector in self._model.embed(list(texts))]
        except Exception as exc:  # SDK/model errors vary by version
            raise EmbeddingError(f"embedding generation failed: {exc}") from exc
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbeddingError(
                    f"embedding dimension {len(vector)} does not match configured "
                    f"dimension {self.dimension}"
                )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0]
