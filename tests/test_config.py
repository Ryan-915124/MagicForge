from pathlib import Path
from uuid import uuid4

import pytest

from app.config import PROJECT_ROOT, Settings, SettingsConfigurationError
from knowledge.governance import MagicForgeMode


def test_settings_loads_glm_request_budget_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("GLM_TIMEOUT_SECONDS", "42.5")
    monkeypatch.setenv("GLM_MAX_RETRIES", "2")

    settings = Settings.from_env()

    assert settings.glm_timeout_seconds == 42.5
    assert settings.glm_max_retries == 2


def test_settings_default_glm_budget_finishes_before_frontend_deadline() -> None:
    settings = Settings()

    assert settings.glm_timeout_seconds == 90.0
    assert settings.glm_max_retries == 0
    assert settings.production_glm_extraction_enabled is False


def test_production_glm_extraction_requires_explicit_switch(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "production")
    monkeypatch.setenv("PRODUCTION_GLM_EXTRACTION_ENABLED", "true")

    settings = Settings.from_env()

    assert settings.production_glm_extraction_enabled is True


def test_bootstrap_rejects_production_glm_extraction_switch(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "bootstrap")
    monkeypatch.setenv("PRODUCTION_GLM_EXTRACTION_ENABLED", "true")

    with pytest.raises(
        SettingsConfigurationError,
        match="valid only in Production mode",
    ):
        Settings.from_env()


def test_settings_loads_database_lifecycle_configuration(monkeypatch) -> None:
    database_url = "postgresql+psycopg://magicforge:local@db/magicforge"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "7.5")

    settings = Settings.from_env()

    assert settings.database_url == database_url
    assert settings.database_connect_timeout_seconds == 7.5


def test_bootstrap_anonymous_reads_require_explicit_setting(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "bootstrap")
    monkeypatch.setenv("BOOTSTRAP_ALLOW_ANONYMOUS_READS", "true")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")

    settings = Settings.from_env()

    assert settings.bootstrap_allow_anonymous_reads is True
    assert settings.auth_cookie_secure is False


def test_production_rejects_bootstrap_anonymous_override(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "production")
    monkeypatch.setenv("BOOTSTRAP_ALLOW_ANONYMOUS_READS", "true")

    with pytest.raises(
        SettingsConfigurationError,
        match="cannot be enabled in Production",
    ):
        Settings.from_env()


def test_invalid_auth_origin_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://example.test/path")

    with pytest.raises(SettingsConfigurationError, match=r"HTTP\(S\) origins"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_invalid_database_connect_timeout_is_rejected(
    monkeypatch, value: str
) -> None:
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", value)

    with pytest.raises(
        SettingsConfigurationError,
        match="DATABASE_CONNECT_TIMEOUT_SECONDS",
    ):
        Settings.from_env()


def test_bootstrap_environment_resolves_authoritative_v03_runtime(
    monkeypatch,
) -> None:
    root = PROJECT_ROOT / "research/runs/bootstrap-002"
    monkeypatch.setenv("MAGICFORGE_MODE", "bootstrap")
    monkeypatch.setenv("ACTIVE_CORPUS_ROOT", str(root))
    monkeypatch.setenv("KNOWLEDGE_RUN_PATH", str(root))
    monkeypatch.setenv("ACTIVE_CORPUS_ID", "")
    monkeypatch.setenv("ACTIVE_CORPUS_MANIFEST_PATH", "")
    monkeypatch.setenv("ACTIVE_CORPUS_RECEIPT_PATH", "")
    monkeypatch.setenv("ACTIVE_CORPUS_MANIFEST_SCHEMA", "")
    monkeypatch.setenv("ACTIVE_QDRANT_STORAGE_KIND", "local")
    monkeypatch.setenv("ACTIVE_QDRANT_LOCAL_PATH", "")

    settings = Settings.from_env()

    assert settings.magicforge_mode == MagicForgeMode.BOOTSTRAP
    assert settings.active_corpus_id == "bootstrap-002"
    assert Path(settings.active_corpus_manifest_path) == (
        root / "qdrant_manifest/bootstrap-manifest-v03.json"
    )
    assert Path(settings.active_corpus_receipt_path) == (
        root / "qdrant_manifest/ingestion-receipt-v03.json"
    )
    assert settings.active_corpus_manifest_schema == "bootstrap-manifest-0.2"
    assert Path(settings.active_qdrant_local_path) == root / "qdrant_storage_v03"


def test_conflicting_runtime_and_legacy_corpus_roots_are_rejected(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "bootstrap")
    monkeypatch.setenv("ACTIVE_CORPUS_ROOT", "research/runs/bootstrap-002")
    monkeypatch.setenv("KNOWLEDGE_RUN_PATH", "research/runs/bootstrap-001")

    with pytest.raises(SettingsConfigurationError, match="conflicts"):
        Settings.from_env()


def test_invalid_runtime_storage_kind_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "production")
    monkeypatch.setenv("BOOTSTRAP_ALLOW_ANONYMOUS_READS", "false")
    monkeypatch.setenv("ACTIVE_CORPUS_ROOT", "")
    monkeypatch.setenv("KNOWLEDGE_RUN_PATH", "")
    monkeypatch.setenv("ACTIVE_QDRANT_STORAGE_KIND", "automatic")

    with pytest.raises(SettingsConfigurationError, match="local or remote"):
        Settings.from_env()


def test_invalid_runtime_mode_is_rejected_with_configuration_error(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "mixed")

    with pytest.raises(SettingsConfigurationError, match="bootstrap or production"):
        Settings.from_env()


def test_production_write_switch_requires_complete_exact_fingerprint(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "production")
    monkeypatch.setenv("BOOTSTRAP_ALLOW_ANONYMOUS_READS", "false")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITES_ENABLED", "true")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_MANIFEST_ID", "")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_MANIFEST_HASH", "")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_COLLECTION", "")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_POINT_COUNT", "")

    with pytest.raises(SettingsConfigurationError, match="exact manifest fingerprint"):
        Settings.from_env()


def test_partial_production_write_fingerprint_is_rejected_even_when_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MAGICFORGE_MODE", "production")
    monkeypatch.setenv("BOOTSTRAP_ALLOW_ANONYMOUS_READS", "false")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITES_ENABLED", "false")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_MANIFEST_ID", str(uuid4()))
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_MANIFEST_HASH", "")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_COLLECTION", "")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_POINT_COUNT", "")

    with pytest.raises(SettingsConfigurationError, match="configured together"):
        Settings.from_env()


def test_complete_production_write_fingerprint_is_loaded_only_as_exact_values(
    monkeypatch,
) -> None:
    manifest_id = str(uuid4())
    monkeypatch.setenv("MAGICFORGE_MODE", "production")
    monkeypatch.setenv("BOOTSTRAP_ALLOW_ANONYMOUS_READS", "false")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "magicforge_knowledge_v01")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITES_ENABLED", "true")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_MANIFEST_ID", manifest_id)
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_MANIFEST_HASH", "a" * 64)
    monkeypatch.setenv(
        "PRODUCTION_QDRANT_WRITE_COLLECTION", "magicforge_knowledge_v01"
    )
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_POINT_COUNT", "17")

    settings = Settings.from_env()

    assert settings.production_qdrant_writes_enabled is True
    assert settings.production_qdrant_write_manifest_id == manifest_id
    assert settings.production_qdrant_write_manifest_hash == "a" * 64
    assert settings.production_qdrant_write_collection == "magicforge_knowledge_v01"
    assert settings.production_qdrant_write_point_count == 17
