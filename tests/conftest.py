from __future__ import annotations

from pathlib import Path

import pytest

import research.acquisition.staging as staging
import research.acquisition.tavily_results as tavily_results
import security.governance_source_cli as governance_source_cli

from tests.support.synthetic_bootstrap import (
    SyntheticBootstrapCorpus,
    write_synthetic_bootstrap,
)
from tests.support.synthetic_acquisition import (
    SyntheticAcquisitionRun,
    SyntheticOfficialAcquisition,
    write_synthetic_official_acquisition,
    write_synthetic_source_run,
)


@pytest.fixture(scope="session")
def synthetic_bootstrap_corpus(
    tmp_path_factory: pytest.TempPathFactory,
) -> SyntheticBootstrapCorpus:
    project_root = Path(tmp_path_factory.mktemp("synthetic-bootstrap-project"))
    return write_synthetic_bootstrap(project_root)


@pytest.fixture(scope="session")
def synthetic_acquisition_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> SyntheticAcquisitionRun:
    root = Path(tmp_path_factory.mktemp("synthetic-source-run"))
    return write_synthetic_source_run(root)


@pytest.fixture(scope="session")
def synthetic_official_acquisition(
    tmp_path_factory: pytest.TempPathFactory,
    synthetic_acquisition_run: SyntheticAcquisitionRun,
) -> SyntheticOfficialAcquisition:
    run_root = Path(tmp_path_factory.mktemp("synthetic-official-acquisition"))
    return write_synthetic_official_acquisition(
        synthetic_acquisition_run,
        run_root,
    )


@pytest.fixture
def synthetic_acquisition_locks(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_acquisition_run: SyntheticAcquisitionRun,
) -> SyntheticAcquisitionRun:
    monkeypatch.setattr(
        staging,
        "EXPECTED_CANDIDATE_IDS",
        synthetic_acquisition_run.candidate_ids,
    )
    monkeypatch.setattr(
        staging,
        "EXPECTED_INPUT_LOCK",
        synthetic_acquisition_run.input_lock,
    )
    monkeypatch.setattr(
        tavily_results,
        "EXPECTED_CANDIDATE_IDS",
        synthetic_acquisition_run.candidate_ids,
    )
    monkeypatch.setattr(
        tavily_results,
        "EXPECTED_INPUT_LOCK",
        synthetic_acquisition_run.input_lock,
    )
    monkeypatch.setattr(
        tavily_results,
        "UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID",
        synthetic_acquisition_run.unresolved_candidate_id,
    )
    return synthetic_acquisition_run


@pytest.fixture
def synthetic_official_cli_bundle(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_acquisition_locks: SyntheticAcquisitionRun,
    synthetic_official_acquisition: SyntheticOfficialAcquisition,
) -> SyntheticOfficialAcquisition:
    monkeypatch.setattr(
        governance_source_cli,
        "EXPECTED_CANDIDATE_IDS",
        synthetic_acquisition_locks.candidate_ids,
    )
    monkeypatch.setattr(
        governance_source_cli,
        "EXPECTED_INPUT_LOCK",
        synthetic_acquisition_locks.input_lock,
    )
    monkeypatch.setattr(
        governance_source_cli,
        "_UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID",
        governance_source_cli.UUID(
            synthetic_acquisition_locks.unresolved_candidate_id
        ),
    )
    monkeypatch.setattr(
        governance_source_cli,
        "_OFFICIAL_MANIFEST_SHA256",
        synthetic_official_acquisition.manifest_sha256,
    )
    monkeypatch.setattr(
        governance_source_cli,
        "_OFFICIAL_RESULTS_SHA256",
        synthetic_official_acquisition.results_sha256,
    )
    monkeypatch.setattr(
        governance_source_cli,
        "_DEFAULT_PAYLOAD_DIRECTORY",
        synthetic_official_acquisition.payload_directory,
    )
    monkeypatch.setattr(
        governance_source_cli,
        "_DEFAULT_RESULTS_FILE",
        synthetic_official_acquisition.results_file,
    )
    return synthetic_official_acquisition
