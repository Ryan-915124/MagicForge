"""Persistence ports for source approval records."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Protocol

from pydantic import TypeAdapter

from knowledge.governance import ReviewStatus
from research.review.source_models import SourceApprovalRecord


class SourceApprovalRepository(Protocol):
    def save(self, item: SourceApprovalRecord) -> None: ...

    def get(self, item_id: str) -> SourceApprovalRecord | None: ...

    def list(self, status: ReviewStatus | None = None) -> list[SourceApprovalRecord]: ...


class InMemorySourceApprovalRepository:
    def __init__(self) -> None:
        self._items: dict[str, SourceApprovalRecord] = {}

    def save(self, item: SourceApprovalRecord) -> None:
        self._items[item.id] = item.model_copy(deep=True)

    def get(self, item_id: str) -> SourceApprovalRecord | None:
        item = self._items.get(item_id)
        return item.model_copy(deep=True) if item else None

    def list(self, status: ReviewStatus | None = None) -> list[SourceApprovalRecord]:
        return [
            item.model_copy(deep=True)
            for item in sorted(self._items.values(), key=lambda value: value.submitted_at)
            if status is None or item.status == status
        ]


class JsonSourceApprovalRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def save(self, item: SourceApprovalRecord) -> None:
        with self._lock:
            items = {current.id: current for current in self._read()}
            items[item.id] = item
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    [value.model_dump(mode="json") for value in items.values()],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.path)

    def get(self, item_id: str) -> SourceApprovalRecord | None:
        with self._lock:
            return next((item for item in self._read() if item.id == item_id), None)

    def list(self, status: ReviewStatus | None = None) -> list[SourceApprovalRecord]:
        with self._lock:
            return [
                item
                for item in sorted(self._read(), key=lambda value: value.submitted_at)
                if status is None or item.status == status
            ]

    def _read(self) -> list[SourceApprovalRecord]:
        if not self.path.exists():
            return []
        return TypeAdapter(list[SourceApprovalRecord]).validate_json(
            self.path.read_text(encoding="utf-8")
        )
