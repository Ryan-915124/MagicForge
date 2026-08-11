"""Environment-backed configuration for MagicForge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from dotenv import load_dotenv

from knowledge.governance import MagicForgeMode


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SettingsConfigurationError(ValueError):
    """Raised when environment-backed runtime settings are ambiguous."""


class MagicForgeProfile(StrEnum):
    """Deployment profile; Production remains the fail-closed default.

    ``development`` preserves the existing private Bootstrap workflow.  The
    public ``demo`` profile is intentionally separate so it can never resolve
    artifacts from ``research/runs`` or contact GLM.
    """

    DEMO = "demo"
    DEVELOPMENT = "development"
    PRODUCTION = "production"

    @property
    def governance_mode(self) -> MagicForgeMode:
        if self == self.PRODUCTION:
            return MagicForgeMode.PRODUCTION
        return MagicForgeMode.BOOTSTRAP


def _environment_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SettingsConfigurationError(
        f"{name} must be one of true, false, 1, 0, yes, no, on, or off"
    )


def _allowed_origins_from_env() -> tuple[str, ...]:
    raw_value = os.getenv(
        "AUTH_ALLOWED_ORIGINS",
        "http://127.0.0.1:3000,http://localhost:3000",
    )
    origins: list[str] = []
    for candidate in raw_value.split(","):
        origin = candidate.strip().rstrip("/")
        if not origin:
            continue
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise SettingsConfigurationError(
                "AUTH_ALLOWED_ORIGINS must contain comma-separated HTTP(S) origins"
            )
        if origin not in origins:
            origins.append(origin)
    if not origins:
        raise SettingsConfigurationError(
            "AUTH_ALLOWED_ORIGINS must contain at least one origin"
        )
    return tuple(origins)


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings.

    Required external credentials are validated by the adapter that uses them,
    which lets the API and its health endpoint start before secrets are present.
    """

    glm_api_key: str = ""
    glm_model: str = "glm-4.7"
    glm_timeout_seconds: float = 90.0
    glm_max_retries: int = 0
    production_glm_extraction_enabled: bool = False
    database_url: str = ""
    database_connect_timeout_seconds: float = 5.0
    auth_session_ttl_seconds: int = 43_200
    auth_last_used_update_interval_seconds: int = 300
    auth_session_cookie_name: str = "magicforge_session"
    auth_csrf_cookie_name: str = "magicforge_csrf"
    auth_cookie_secure: bool = True
    auth_allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    )
    bootstrap_allow_anonymous_reads: bool = False
    magicforge_mode: MagicForgeMode = MagicForgeMode.PRODUCTION
    # ``None`` keeps direct Settings(...) construction backward compatible;
    # runtime code derives the profile from ``magicforge_mode`` in that case.
    magicforge_profile: MagicForgeProfile | None = None
    demo_qdrant_url: str = ""
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection_name: str = "magicforge_knowledge_v01"
    qdrant_bootstrap_collection_name: str = "magicforge_bootstrap_v02"
    qdrant_bootstrap_local_path: str = ""
    production_qdrant_writes_enabled: bool = False
    production_qdrant_write_manifest_id: str = ""
    production_qdrant_write_manifest_hash: str = ""
    production_qdrant_write_collection: str = ""
    production_qdrant_write_point_count: int = 0
    # Canonical API runtime corpus settings. The v0.2 fields above remain for
    # historical offline Bootstrap writers and are not used by the API runtime.
    active_corpus_root: str = ""
    active_corpus_id: str = ""
    active_corpus_manifest_path: str = ""
    active_corpus_receipt_path: str = ""
    active_corpus_manifest_schema: str = ""
    active_qdrant_storage_kind: Literal["local", "remote"] = "remote"
    active_qdrant_local_path: str = ""
    knowledge_run_path: str = str(PROJECT_ROOT / "research/runs/bootstrap-002")
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimension: int = 384
    retrieval_limit: int = 5
    chunk_size: int = 1_200
    chunk_overlap: int = 150

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        profile_value = os.getenv("MAGICFORGE_PROFILE", "").strip().casefold()
        if profile_value:
            try:
                profile = MagicForgeProfile(profile_value)
            except ValueError as exc:
                raise SettingsConfigurationError(
                    "MAGICFORGE_PROFILE must be demo, development, or production"
                ) from exc
            # The profile is the new source of truth.  An existing .env may
            # still contain the legacy mode while a caller selects Demo in the
            # process environment, so do not let that legacy value override it.
            mode = profile.governance_mode
        else:
            try:
                mode = MagicForgeMode(
                    os.getenv("MAGICFORGE_MODE", "production").strip().casefold()
                )
            except ValueError as exc:
                raise SettingsConfigurationError(
                    "MAGICFORGE_MODE must be bootstrap or production"
                ) from exc
            profile = (
                MagicForgeProfile.PRODUCTION
                if mode == MagicForgeMode.PRODUCTION
                else MagicForgeProfile.DEVELOPMENT
            )

        is_demo = profile == MagicForgeProfile.DEMO
        demo_root = PROJECT_ROOT / "data/demo"
        demo_qdrant_url = (
            os.getenv("MAGICFORGE_DEMO_QDRANT_URL", "").strip()
            if is_demo
            else ""
        )
        if demo_qdrant_url:
            parsed_demo_qdrant = urlsplit(demo_qdrant_url)
            try:
                parsed_demo_port = parsed_demo_qdrant.port
            except ValueError as exc:
                raise SettingsConfigurationError(
                    "MAGICFORGE_DEMO_QDRANT_URL must identify a credential-free local Qdrant endpoint"
                ) from exc
            if (
                parsed_demo_qdrant.scheme != "http"
                or parsed_demo_qdrant.hostname
                not in {"qdrant", "localhost", "127.0.0.1", "::1"}
                or parsed_demo_qdrant.username is not None
                or parsed_demo_qdrant.password is not None
                or (
                    parsed_demo_port is not None
                    and not 1 <= parsed_demo_port <= 65535
                )
                or parsed_demo_qdrant.path not in {"", "/"}
                or bool(parsed_demo_qdrant.query)
                or bool(parsed_demo_qdrant.fragment)
            ):
                raise SettingsConfigurationError(
                    "MAGICFORGE_DEMO_QDRANT_URL must identify a credential-free local Qdrant endpoint"
                )
        configured_root = os.getenv("ACTIVE_CORPUS_ROOT", "").strip()
        legacy_root = os.getenv("KNOWLEDGE_RUN_PATH", "").strip()
        if not is_demo and configured_root and legacy_root:
            if Path(configured_root).resolve() != Path(legacy_root).resolve():
                raise SettingsConfigurationError(
                    "ACTIVE_CORPUS_ROOT conflicts with legacy KNOWLEDGE_RUN_PATH"
                )
        active_root = str(demo_root) if is_demo else configured_root or legacy_root
        if (
            not active_root
            and mode == MagicForgeMode.BOOTSTRAP
            and not (profile_value and profile == MagicForgeProfile.DEVELOPMENT)
        ):
            active_root = str(PROJECT_ROOT / "research/runs/bootstrap-002")

        storage_kind = (
            ("remote" if demo_qdrant_url else "local")
            if is_demo
            else os.getenv(
                "ACTIVE_QDRANT_STORAGE_KIND",
                "local" if mode == MagicForgeMode.BOOTSTRAP else "remote",
            ).strip().casefold()
        )
        if storage_kind not in {"local", "remote"}:
            raise SettingsConfigurationError(
                "ACTIVE_QDRANT_STORAGE_KIND must be local or remote"
            )

        active_local_path = (
            "" if is_demo else os.getenv("ACTIVE_QDRANT_LOCAL_PATH", "").strip()
        )
        if (
            not is_demo
            and not active_local_path
            and storage_kind == "local"
            and mode == MagicForgeMode.BOOTSTRAP
            and active_root
        ):
            active_local_path = str(Path(active_root) / "qdrant_storage_v03")

        manifest_path = (
            str(demo_root / "corpus.json")
            if is_demo
            else os.getenv("ACTIVE_CORPUS_MANIFEST_PATH", "").strip()
        )
        receipt_path = (
            str(demo_root / "corpus.json")
            if is_demo
            else os.getenv("ACTIVE_CORPUS_RECEIPT_PATH", "").strip()
        )
        if active_root and mode == MagicForgeMode.BOOTSTRAP:
            manifest_path = manifest_path or str(
                Path(active_root)
                / "qdrant_manifest"
                / "bootstrap-manifest-v03.json"
            )
            receipt_path = receipt_path or str(
                Path(active_root)
                / "qdrant_manifest"
                / "ingestion-receipt-v03.json"
            )

        try:
            database_connect_timeout_seconds = float(
                os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "5")
            )
        except ValueError as exc:
            raise SettingsConfigurationError(
                "DATABASE_CONNECT_TIMEOUT_SECONDS must be a number"
            ) from exc
        if database_connect_timeout_seconds <= 0:
            raise SettingsConfigurationError(
                "DATABASE_CONNECT_TIMEOUT_SECONDS must be greater than zero"
            )

        try:
            auth_session_ttl_seconds = int(
                os.getenv("AUTH_SESSION_TTL_SECONDS", "43200")
            )
            auth_last_used_update_interval_seconds = int(
                os.getenv("AUTH_LAST_USED_UPDATE_INTERVAL_SECONDS", "300")
            )
        except ValueError as exc:
            raise SettingsConfigurationError(
                "AUTH session duration settings must be integers"
            ) from exc
        if not 300 <= auth_session_ttl_seconds <= 2_592_000:
            raise SettingsConfigurationError(
                "AUTH_SESSION_TTL_SECONDS must be between 300 and 2592000"
            )
        if not 0 <= auth_last_used_update_interval_seconds <= auth_session_ttl_seconds:
            raise SettingsConfigurationError(
                "AUTH_LAST_USED_UPDATE_INTERVAL_SECONDS must be between zero and the session TTL"
            )

        auth_session_cookie_name = os.getenv(
            "AUTH_SESSION_COOKIE_NAME", "magicforge_session"
        ).strip()
        auth_csrf_cookie_name = os.getenv(
            "AUTH_CSRF_COOKIE_NAME", "magicforge_csrf"
        ).strip()
        if not auth_session_cookie_name or not auth_csrf_cookie_name:
            raise SettingsConfigurationError(
                "Authentication cookie names must not be empty"
            )
        if auth_session_cookie_name == auth_csrf_cookie_name:
            raise SettingsConfigurationError(
                "Session and CSRF cookie names must be different"
            )

        auth_cookie_secure = _environment_bool(
            "AUTH_COOKIE_SECURE",
            default=profile == MagicForgeProfile.PRODUCTION,
        )
        bootstrap_allow_anonymous_reads = (
            True
            if is_demo
            else _environment_bool(
                "BOOTSTRAP_ALLOW_ANONYMOUS_READS",
                default=False,
            )
        )
        if (
            mode == MagicForgeMode.PRODUCTION
            and bootstrap_allow_anonymous_reads
        ):
            raise SettingsConfigurationError(
                "BOOTSTRAP_ALLOW_ANONYMOUS_READS cannot be enabled in Production"
            )

        production_writes_enabled = (
            False
            if is_demo
            else _environment_bool(
                "PRODUCTION_QDRANT_WRITES_ENABLED", default=False
            )
        )
        production_glm_extraction_enabled = (
            False
            if is_demo
            else _environment_bool(
                "PRODUCTION_GLM_EXTRACTION_ENABLED", default=False
            )
        )
        if production_glm_extraction_enabled and mode != MagicForgeMode.PRODUCTION:
            raise SettingsConfigurationError(
                "PRODUCTION_GLM_EXTRACTION_ENABLED is valid only in Production mode"
            )
        qdrant_collection_name = os.getenv(
            "QDRANT_COLLECTION_NAME", "magicforge_knowledge_v01"
        ).strip()
        write_manifest_id = (
            ""
            if is_demo
            else os.getenv("PRODUCTION_QDRANT_WRITE_MANIFEST_ID", "").strip()
        )
        write_manifest_hash = (
            ""
            if is_demo
            else os.getenv("PRODUCTION_QDRANT_WRITE_MANIFEST_HASH", "")
            .strip()
            .casefold()
        )
        write_collection = (
            ""
            if is_demo
            else os.getenv("PRODUCTION_QDRANT_WRITE_COLLECTION", "").strip()
        )
        write_point_count_raw = (
            ""
            if is_demo
            else os.getenv("PRODUCTION_QDRANT_WRITE_POINT_COUNT", "").strip()
        )
        fingerprint_present = (
            bool(write_manifest_id),
            bool(write_manifest_hash),
            bool(write_collection),
            bool(write_point_count_raw),
        )
        if any(fingerprint_present) and not all(fingerprint_present):
            raise SettingsConfigurationError(
                "Production Qdrant write fingerprint fields must be configured together"
            )
        write_point_count = 0
        if all(fingerprint_present):
            try:
                UUID(write_manifest_id)
                write_point_count = int(write_point_count_raw)
            except (ValueError, TypeError) as exc:
                raise SettingsConfigurationError(
                    "Production Qdrant write fingerprint is invalid"
                ) from exc
            if (
                len(write_manifest_hash) != 64
                or any(value not in "0123456789abcdef" for value in write_manifest_hash)
                or write_point_count <= 0
                or "bootstrap" in write_collection.casefold()
                or write_collection != qdrant_collection_name
            ):
                raise SettingsConfigurationError(
                    "Production Qdrant write fingerprint is invalid"
                )
        if production_writes_enabled and not all(fingerprint_present):
            raise SettingsConfigurationError(
                "PRODUCTION_QDRANT_WRITES_ENABLED requires an exact manifest fingerprint"
            )
        if production_writes_enabled and mode != MagicForgeMode.PRODUCTION:
            raise SettingsConfigurationError(
                "Production Qdrant writes can only be enabled in Production mode"
            )

        return cls(
            # Demo must remain offline even when the local .env contains a key.
            glm_api_key="" if is_demo else os.getenv("GLM_API_KEY", "").strip(),
            glm_model=(
                "deterministic-demo"
                if is_demo
                else os.getenv("GLM_MODEL", "glm-4.7").strip()
            ),
            glm_timeout_seconds=(
                1.0 if is_demo else float(os.getenv("GLM_TIMEOUT_SECONDS", "90"))
            ),
            glm_max_retries=(
                0 if is_demo else int(os.getenv("GLM_MAX_RETRIES", "0"))
            ),
            production_glm_extraction_enabled=production_glm_extraction_enabled,
            database_url=(
                "" if is_demo else os.getenv("DATABASE_URL", "").strip()
            ),
            database_connect_timeout_seconds=database_connect_timeout_seconds,
            auth_session_ttl_seconds=auth_session_ttl_seconds,
            auth_last_used_update_interval_seconds=(
                auth_last_used_update_interval_seconds
            ),
            auth_session_cookie_name=auth_session_cookie_name,
            auth_csrf_cookie_name=auth_csrf_cookie_name,
            auth_cookie_secure=auth_cookie_secure,
            auth_allowed_origins=_allowed_origins_from_env(),
            bootstrap_allow_anonymous_reads=bootstrap_allow_anonymous_reads,
            magicforge_mode=mode,
            magicforge_profile=profile,
            demo_qdrant_url=demo_qdrant_url,
            qdrant_url=(
                demo_qdrant_url
                or os.getenv("QDRANT_URL", "http://localhost:6333").strip()
            ),
            qdrant_collection_name=qdrant_collection_name,
            qdrant_bootstrap_collection_name=os.getenv(
                "QDRANT_BOOTSTRAP_COLLECTION_NAME", "magicforge_bootstrap_v02"
            ).strip(),
            qdrant_bootstrap_local_path=os.getenv(
                "QDRANT_BOOTSTRAP_LOCAL_PATH", ""
            ).strip(),
            production_qdrant_writes_enabled=production_writes_enabled,
            production_qdrant_write_manifest_id=write_manifest_id,
            production_qdrant_write_manifest_hash=write_manifest_hash,
            production_qdrant_write_collection=write_collection,
            production_qdrant_write_point_count=write_point_count,
            active_corpus_root=active_root,
            active_corpus_id=(
                (
                    "magicforge-demo-v01"
                    if is_demo
                    else os.getenv("ACTIVE_CORPUS_ID", "").strip()
                )
                or (
                    "bootstrap-002"
                    if mode == MagicForgeMode.BOOTSTRAP and active_root
                    else ""
                )
            ),
            active_corpus_manifest_path=manifest_path,
            active_corpus_receipt_path=receipt_path,
            active_corpus_manifest_schema=(
                (
                    "demo-corpus-0.1"
                    if is_demo
                    else os.getenv("ACTIVE_CORPUS_MANIFEST_SCHEMA", "").strip()
                )
                or (
                    "bootstrap-manifest-0.2"
                    if mode == MagicForgeMode.BOOTSTRAP
                    else "manifest-0.2"
                )
            ),
            active_qdrant_storage_kind=storage_kind,
            active_qdrant_local_path=active_local_path,
            knowledge_run_path=os.getenv(
                "KNOWLEDGE_RUN_PATH",
                (
                    ""
                    if profile_value and profile == MagicForgeProfile.DEVELOPMENT
                    else str(PROJECT_ROOT / "research/runs/bootstrap-002")
                ),
            ).strip()
            if not is_demo
            else str(demo_root),
            embedding_model=(
                "deterministic-demo-hash"
                if is_demo
                else os.getenv(
                    "EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"
                ).strip()
            ),
            embedding_dimension=(
                384
                if is_demo
                else int(os.getenv("EMBEDDING_DIMENSION", "384"))
            ),
            retrieval_limit=int(os.getenv("RETRIEVAL_LIMIT", "5")),
            chunk_size=int(os.getenv("CHUNK_SIZE", "1200")),
            chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "150")),
        )

    @property
    def active_qdrant_collection_name(self) -> str:
        if self.effective_profile == MagicForgeProfile.DEMO:
            return "magicforge_demo_v01"
        if self.magicforge_mode == MagicForgeMode.BOOTSTRAP:
            return self.qdrant_bootstrap_collection_name
        return self.qdrant_collection_name

    @property
    def active_qdrant_location(self) -> str:
        if self.effective_profile == MagicForgeProfile.DEMO:
            return (
                self.demo_qdrant_url
                if self.demo_qdrant_url
                else "memory://magicforge-demo-v01"
            )
        if (
            self.magicforge_mode == MagicForgeMode.BOOTSTRAP
            and self.qdrant_bootstrap_local_path
        ):
            return f"local://{self.qdrant_bootstrap_local_path}"
        return self.qdrant_url

    @property
    def effective_profile(self) -> MagicForgeProfile:
        """Return an explicit profile for both new and legacy settings."""

        if self.magicforge_profile is not None:
            return MagicForgeProfile(self.magicforge_profile)
        return (
            MagicForgeProfile.PRODUCTION
            if self.magicforge_mode == MagicForgeMode.PRODUCTION
            else MagicForgeProfile.DEVELOPMENT
        )

    @property
    def anonymous_product_reads_enabled(self) -> bool:
        return bool(
            self.effective_profile == MagicForgeProfile.DEMO
            or (
                self.magicforge_mode == MagicForgeMode.BOOTSTRAP
                and self.bootstrap_allow_anonymous_reads
            )
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
