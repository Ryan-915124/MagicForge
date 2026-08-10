from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings
from app.runtime_corpus import (
    ActiveCorpusConfigurationError,
    load_active_corpus,
)
from knowledge.governance import MagicForgeMode


RUN_ROOT = PROJECT_ROOT / "research/runs/bootstrap-002"
MANIFEST_ID = "f7b2da19-bfc2-55de-8850-2d58638b5031"
MANIFEST_HASH = "84b8bb01ba1ef7159268b1178059c415670af23e6c7b27d9dd34bba1d1faab9a"
RECEIPT_ID = "a720b4db-9a2d-564e-bb8f-fd9f9ea4f37e"


def _bootstrap_settings(root: Path = RUN_ROOT, **changes) -> Settings:
    settings = Settings(
        magicforge_mode=MagicForgeMode.BOOTSTRAP,
        active_corpus_root=str(root),
        active_corpus_id="bootstrap-002",
        active_corpus_manifest_path=str(
            root / "qdrant_manifest/bootstrap-manifest-v03.json"
        ),
        active_corpus_receipt_path=str(
            root / "qdrant_manifest/ingestion-receipt-v03.json"
        ),
        active_corpus_manifest_schema="bootstrap-manifest-0.2",
        active_qdrant_storage_kind="local",
        active_qdrant_local_path=str(root / "qdrant_storage_v03"),
        embedding_dimension=384,
    )
    return replace(settings, **changes)


def _copy_authoritative_metadata(destination: Path) -> Path:
    for relative in (
        "qdrant_manifest/bootstrap-manifest-v03.json",
        "qdrant_manifest/ingestion-receipt-v03.json",
        "reports/run-summary.json",
        "reports/retrieval-smoke-test.json",
        "knowledge_nodes/_canonical_registry.json",
    ):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(RUN_ROOT / relative, target)
    (destination / "evidence_cards").mkdir(parents=True)
    (destination / "sources").mkdir(parents=True)
    return destination


def _remote_settings(root: Path, **changes) -> Settings:
    return _bootstrap_settings(
        root,
        active_qdrant_storage_kind="remote",
        active_qdrant_local_path="",
        qdrant_url="http://qdrant.test:6333",
        **changes,
    )


def _mutate_json(path: Path, operation) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    operation(value)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_authoritative_v03_resolves_exact_immutable_identity() -> None:
    corpus = load_active_corpus(_bootstrap_settings())

    assert corpus.mode == MagicForgeMode.BOOTSTRAP
    assert corpus.corpus_id == "bootstrap-002"
    assert corpus.manifest_schema_version == "bootstrap-manifest-0.2"
    assert corpus.projection_schema_version == "bootstrap-qdrant-0.2"
    assert corpus.manifest_id == MANIFEST_ID
    assert corpus.manifest_hash == MANIFEST_HASH
    assert corpus.receipt_id == RECEIPT_ID
    assert corpus.collection_name == "magicforge_bootstrap_v03"
    assert corpus.expected_point_count == 797
    assert len(corpus.expected_point_ids) == 797
    assert len(set(corpus.expected_point_ids)) == 797
    assert corpus.storage_kind == "local"
    assert corpus.local_storage_path == RUN_ROOT / "qdrant_storage_v03"
    assert corpus.server_url is None
    assert corpus.authorized is False
    assert corpus.bootstrap_generated is True
    assert corpus.human_verified is False


def test_legacy_v02_collection_setting_does_not_select_runtime_corpus() -> None:
    corpus = load_active_corpus(
        _bootstrap_settings(
            qdrant_bootstrap_collection_name="magicforge_bootstrap_v02",
            qdrant_bootstrap_local_path=str(
                PROJECT_ROOT / "research/runs/bootstrap-001/qdrant_storage"
            ),
        )
    )

    assert corpus.collection_name == "magicforge_bootstrap_v03"
    assert corpus.local_storage_path == RUN_ROOT / "qdrant_storage_v03"


def test_production_rejects_bootstrap_identity_before_loading_artifacts() -> None:
    settings = _bootstrap_settings(magicforge_mode=MagicForgeMode.PRODUCTION)

    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(settings)

    assert caught.value.code == "active_corpus_mode_mismatch"
    assert "Bootstrap" in caught.value.public_message


def test_production_without_explicit_authorized_corpus_fails_closed() -> None:
    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(Settings(magicforge_mode=MagicForgeMode.PRODUCTION))

    assert caught.value.code == "active_corpus_not_configured"


def test_production_rejects_bootstrap_collection_configuration() -> None:
    settings = Settings(
        magicforge_mode=MagicForgeMode.PRODUCTION,
        qdrant_collection_name="magicforge_bootstrap_v03",
    )

    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(settings)

    assert caught.value.code == "active_corpus_mode_mismatch"
    assert "Bootstrap collection" in caught.value.public_message


def test_production_rejects_bootstrap_manifest_markers_outside_bootstrap_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "production-corpus"
    root.mkdir()
    manifest_path = root / "manifest.json"
    receipt_path = root / "receipt.json"
    shutil.copyfile(
        RUN_ROOT / "qdrant_manifest/bootstrap-manifest-v03.json",
        manifest_path,
    )
    shutil.copyfile(
        RUN_ROOT / "qdrant_manifest/ingestion-receipt-v03.json",
        receipt_path,
    )
    settings = Settings(
        magicforge_mode=MagicForgeMode.PRODUCTION,
        active_corpus_root=str(root),
        active_corpus_id="production-corpus-001",
        active_corpus_manifest_path=str(manifest_path),
        active_corpus_receipt_path=str(receipt_path),
        active_corpus_manifest_schema="manifest-0.2",
        active_qdrant_storage_kind="remote",
        qdrant_url="http://qdrant.test:6333",
    )

    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(settings)

    assert caught.value.code == "active_corpus_mode_mismatch"
    assert "Bootstrap governance markers" in caught.value.public_message


def test_missing_final_manifest_never_falls_back_to_generic_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bootstrap-002"
    (root / "qdrant_manifest").mkdir(parents=True)
    shutil.copyfile(
        RUN_ROOT / "qdrant_manifest/bootstrap-manifest.json",
        root / "qdrant_manifest/bootstrap-manifest.json",
    )

    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(_remote_settings(root))

    assert caught.value.code == "active_corpus_missing_artifact"
    assert "authoritative manifest" in caught.value.public_message


def test_configured_manifest_schema_must_match_supported_bootstrap_schema() -> None:
    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(
            _bootstrap_settings(active_corpus_manifest_schema="bootstrap-manifest-0.1")
        )

    assert caught.value.code == "active_corpus_schema_mismatch"


def test_receipt_manifest_mismatch_fails_closed(tmp_path: Path) -> None:
    root = _copy_authoritative_metadata(tmp_path / "bootstrap-002")
    receipt_path = root / "qdrant_manifest/ingestion-receipt-v03.json"
    _mutate_json(receipt_path, lambda value: value.__setitem__("manifest_id", str(RECEIPT_ID)))

    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(_remote_settings(root))

    assert caught.value.code == "active_corpus_receipt_mismatch"


def test_manifest_collection_mismatch_fails_before_remote_access(
    tmp_path: Path,
) -> None:
    root = _copy_authoritative_metadata(tmp_path / "bootstrap-002")
    manifest_path = root / "qdrant_manifest/bootstrap-manifest-v03.json"
    _mutate_json(
        manifest_path,
        lambda value: value.__setitem__("collection_name", "magicforge_bootstrap_v02"),
    )

    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(_remote_settings(root))

    assert caught.value.code == "active_corpus_collection_mismatch"


def test_summary_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    root = _copy_authoritative_metadata(tmp_path / "bootstrap-002")
    summary_path = root / "reports/run-summary.json"
    _mutate_json(summary_path, lambda value: value.__setitem__("run_id", "bootstrap-001"))

    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(_remote_settings(root))

    assert caught.value.code == "active_corpus_summary_mismatch"


def test_public_configuration_error_does_not_expose_local_path(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private-corpus-location"

    with pytest.raises(ActiveCorpusConfigurationError) as caught:
        load_active_corpus(_remote_settings(private_root))

    serialized = str(caught.value)
    assert str(private_root) not in serialized
    assert caught.value.public_message == caught.value.message
