"""Storage-manifest source of truth, independent of Qdrant."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Protocol

from pydantic import TypeAdapter

from knowledge.projections import IngestionReceipt, ManifestStatus, StorageManifest


class StorageManifestRepository(Protocol):
    def save(self, manifest: StorageManifest) -> None: ...

    def get(self, manifest_id: str) -> StorageManifest | None: ...

    def list(self, status: ManifestStatus | None = None) -> list[StorageManifest]: ...


class InMemoryStorageManifestRepository:
    def __init__(self) -> None:
        self._items: dict[str, StorageManifest] = {}

    def save(self, manifest: StorageManifest) -> None:
        self._items[manifest.id] = manifest.model_copy(deep=True)

    def get(self, manifest_id: str) -> StorageManifest | None:
        item = self._items.get(manifest_id)
        return item.model_copy(deep=True) if item else None

    def list(self, status: ManifestStatus | None = None) -> list[StorageManifest]:
        return [
            item.model_copy(deep=True)
            for item in sorted(self._items.values(), key=lambda value: value.created_at)
            if status is None or item.status == status
        ]


class JsonStorageManifestRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def save(self, manifest: StorageManifest) -> None:
        with self._lock:
            items = {current.id: current for current in self._read()}
            items[manifest.id] = manifest
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

    def get(self, manifest_id: str) -> StorageManifest | None:
        with self._lock:
            return next((item for item in self._read() if item.id == manifest_id), None)

    def list(self, status: ManifestStatus | None = None) -> list[StorageManifest]:
        with self._lock:
            return [
                item
                for item in sorted(self._read(), key=lambda value: value.created_at)
                if status is None or item.status == status
            ]

    def _read(self) -> list[StorageManifest]:
        if not self.path.exists():
            return []
        return TypeAdapter(list[StorageManifest]).validate_json(
            self.path.read_text(encoding="utf-8")
        )


class IngestionReceiptRepository(Protocol):
    def save(self, receipt: IngestionReceipt) -> None: ...

    def get(self, receipt_id: str) -> IngestionReceipt | None: ...

    def get_for_manifest(self, manifest_id: str) -> IngestionReceipt | None: ...


class InMemoryIngestionReceiptRepository:
    def __init__(self) -> None:
        self._items: dict[str, IngestionReceipt] = {}

    def save(self, receipt: IngestionReceipt) -> None:
        self._items[receipt.id] = receipt.model_copy(deep=True)

    def get(self, receipt_id: str) -> IngestionReceipt | None:
        item = self._items.get(receipt_id)
        return item.model_copy(deep=True) if item else None

    def get_for_manifest(self, manifest_id: str) -> IngestionReceipt | None:
        item = next(
            (
                receipt
                for receipt in self._items.values()
                if receipt.manifest_id == manifest_id
            ),
            None,
        )
        return item.model_copy(deep=True) if item else None


class JsonIngestionReceiptRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def save(self, receipt: IngestionReceipt) -> None:
        with self._lock:
            items = {current.id: current for current in self._read()}
            items[receipt.id] = receipt
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

    def get(self, receipt_id: str) -> IngestionReceipt | None:
        with self._lock:
            return next(
                (receipt for receipt in self._read() if receipt.id == receipt_id),
                None,
            )

    def get_for_manifest(self, manifest_id: str) -> IngestionReceipt | None:
        with self._lock:
            return next(
                (
                    receipt
                    for receipt in self._read()
                    if receipt.manifest_id == manifest_id
                ),
                None,
            )

    def _read(self) -> list[IngestionReceipt]:
        if not self.path.exists():
            return []
        return TypeAdapter(list[IngestionReceipt]).validate_json(
            self.path.read_text(encoding="utf-8")
        )
