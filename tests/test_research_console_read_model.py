from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings
from app.production_research_console import ProductionResearchConsoleReadModel
from app.research_console_read_model import (
    ResearchConsoleReadModel,
    ResearchConsoleReadModelError,
)
from app.runtime_corpus import load_active_corpus
from knowledge.governance import MagicForgeMode


RUN_PATH = Path("research/runs/bootstrap-002")


def _active_corpus():
    return load_active_corpus(
        Settings(
            magicforge_mode=MagicForgeMode.BOOTSTRAP,
            active_corpus_root=str(RUN_PATH),
            active_corpus_id="bootstrap-002",
            active_corpus_manifest_path=str(
                RUN_PATH / "qdrant_manifest/bootstrap-manifest-v03.json"
            ),
            active_corpus_receipt_path=str(
                RUN_PATH / "qdrant_manifest/ingestion-receipt-v03.json"
            ),
            active_corpus_manifest_schema="bootstrap-manifest-0.2",
            active_qdrant_storage_kind="local",
            active_qdrant_local_path=str(RUN_PATH / "qdrant_storage_v03"),
        )
    )


def test_console_uses_active_corpus_identity_without_probing() -> None:
    active_corpus = _active_corpus()
    snapshot = ResearchConsoleReadModel(active_corpus, PROJECT_ROOT).snapshot(
        Settings(
            glm_api_key="configured-key",
            glm_model="glm-test",
            magicforge_mode=MagicForgeMode.PRODUCTION,
            qdrant_bootstrap_collection_name="magicforge_bootstrap_v02",
        )
    )

    assert snapshot.runtime["retrieval"]["connectivity"] == "not_probed"
    assert snapshot.runtime["retrieval"]["collection"] == active_corpus.collection_name
    assert snapshot.runtime["retrieval"]["storage_kind"] == "local"
    assert snapshot.memory_vault["runtime_collection"] == active_corpus.collection_name
    assert snapshot.memory_vault["audited_collection"] == active_corpus.collection_name
    assert snapshot.memory_vault["alignment_status"] == "aligned"
    assert snapshot.current_run["run_id"] == active_corpus.corpus_id
    assert snapshot.runtime["intelligence_instrument"]["model"] == "glm-test"
    assert snapshot.runtime["intelligence_instrument"]["configured"] is True
    assert [item.metric_basis for item in snapshot.run_history] == [
        "reported_generated_outputs",
        "reported_generated_outputs",
        "receipt_verified_projections",
    ]


def test_console_rejects_receipt_manifest_mismatch() -> None:
    model = ResearchConsoleReadModel(_active_corpus(), PROJECT_ROOT)
    reports = deepcopy(model._load())
    receipt = reports["bootstrap_002_receipt"]
    assert isinstance(receipt, dict)
    receipt["manifest_id"] = "tampered-manifest"
    model._reports = reports

    with pytest.raises(
        ResearchConsoleReadModelError,
        match="audited projection mismatch: receipt manifest id",
    ):
        model.snapshot(Settings())


def test_console_missing_artifact_error_does_not_expose_absolute_path(
    tmp_path: Path,
) -> None:
    model = ResearchConsoleReadModel(_active_corpus(), tmp_path)

    with pytest.raises(ResearchConsoleReadModelError) as caught:
        model.snapshot(Settings())

    assert str(tmp_path) not in str(caught.value)
    assert "bootstrap_001" in str(caught.value)


def test_console_rejects_a_production_corpus() -> None:
    production = replace(
        _active_corpus(),
        mode=MagicForgeMode.PRODUCTION,
        bootstrap_generated=False,
        human_verified=True,
    )

    with pytest.raises(ResearchConsoleReadModelError, match="requires a Bootstrap"):
        ResearchConsoleReadModel(production, PROJECT_ROOT)


def test_console_rejects_descriptor_identity_drift() -> None:
    mismatched = replace(
        _active_corpus(),
        collection_name="magicforge_bootstrap_wrong",
    )
    model = ResearchConsoleReadModel(mismatched, PROJECT_ROOT)

    with pytest.raises(
        ResearchConsoleReadModelError,
        match="active corpus mismatch: run collection",
    ):
        model.snapshot(Settings(glm_api_key="unused"))


def test_production_console_reports_an_explicit_alpha_boundary() -> None:
    production = replace(
        _active_corpus(),
        mode=MagicForgeMode.PRODUCTION,
        bootstrap_generated=False,
        human_verified=True,
    )
    model = ProductionResearchConsoleReadModel(production)

    with pytest.raises(ResearchConsoleReadModelError) as caught:
        model.snapshot(Settings())

    assert caught.value.code == "alpha_feature_unavailable"
    assert caught.value.status_code == 501
    assert str(caught.value) == "The Production Research Console is not available in this Alpha."
