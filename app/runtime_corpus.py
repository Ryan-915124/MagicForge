"""Validated, immutable identity for the corpus used by every runtime reader."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import TypeAdapter, ValidationError

from app.config import PROJECT_ROOT, MagicForgeProfile, Settings
from knowledge.bootstrap import (
    BOOTSTRAP_MANIFEST_SCHEMA_VERSION,
    BOOTSTRAP_PROJECTION_SCHEMA_VERSION,
    BootstrapQdrantProjection,
)
from knowledge.governance import MagicForgeMode
from knowledge.demo import (
    DEMO_COLLECTION_NAME,
    DEMO_CORPUS_ID,
    DEMO_CORPUS_SCHEMA_VERSION,
    DemoCorpusError,
    load_demo_bundle,
)
from knowledge.projections import (
    MANIFEST_SCHEMA_VERSION,
    PROJECTION_SCHEMA_VERSION,
    IngestionReceipt,
    ManifestStatus,
    StorageManifest,
)


AUTHORITATIVE_BOOTSTRAP_CORPUS_ID = "bootstrap-002"
AUTHORITATIVE_BOOTSTRAP_COLLECTION = "magicforge_bootstrap_v03"
_BOOTSTRAP_MANIFEST_RELATIVE = Path(
    "qdrant_manifest/bootstrap-manifest-v03.json"
)
_BOOTSTRAP_RECEIPT_RELATIVE = Path(
    "qdrant_manifest/ingestion-receipt-v03.json"
)
_BOOTSTRAP_SUMMARY_RELATIVE = Path("reports/run-summary.json")
_BOOTSTRAP_SMOKE_RELATIVE = Path("reports/retrieval-smoke-test.json")
_BOOTSTRAP_LOCAL_STORAGE_RELATIVE = Path("qdrant_storage_v03")

StorageKind = Literal["local", "remote"]


class ActiveCorpusConfigurationError(RuntimeError):
    """A fail-closed runtime configuration error safe for public status APIs."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        self.public_message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ActiveCorpus:
    """One content-addressed corpus identity shared by all runtime adapters."""

    mode: MagicForgeMode
    corpus_id: str
    manifest_schema_version: str
    projection_schema_version: str
    manifest_id: str
    manifest_hash: str
    manifest_path: Path | None
    receipt_id: str
    receipt_path: Path | None
    collection_name: str
    storage_kind: StorageKind
    local_storage_path: Path | None
    server_url: str | None
    artifact_root: Path | None
    run_summary_path: Path | None
    smoke_report_path: Path | None
    expected_point_ids: tuple[str, ...]
    expected_payload_checksums: tuple[tuple[str, str], ...]
    expected_point_count: int
    embedding_dimension: int
    authorized: bool
    bootstrap_generated: bool
    human_verified: bool
    generated_at: str
    ingested_at: str

    @property
    def schema_version(self) -> str:
        """Compatibility alias for callers that expose one manifest schema."""

        return self.manifest_schema_version

    @property
    def qdrant_collection_name(self) -> str:
        return self.collection_name

    @property
    def knowledge_artifact_root(self) -> Path | None:
        return self.artifact_root


def load_active_corpus(settings: Settings) -> ActiveCorpus:
    """Resolve and validate the only corpus that the runtime may construct."""

    if settings.effective_profile == MagicForgeProfile.DEMO:
        return _load_demo(settings)

    try:
        mode = MagicForgeMode(settings.magicforge_mode)
    except (TypeError, ValueError) as exc:
        raise _error(
            "active_corpus_mode_invalid",
            "MAGICFORGE_MODE is not a supported runtime mode.",
        ) from exc

    if mode == MagicForgeMode.PRODUCTION and _looks_bootstrap(
        str(getattr(settings, "qdrant_collection_name", "") or "")
    ):
        raise _error(
            "active_corpus_mode_mismatch",
            "Production mode cannot target a Bootstrap collection.",
        )

    root_value = str(getattr(settings, "active_corpus_root", "") or "").strip()
    corpus_id = str(getattr(settings, "active_corpus_id", "") or "").strip()
    manifest_value = str(
        getattr(settings, "active_corpus_manifest_path", "") or ""
    ).strip()
    receipt_value = str(
        getattr(settings, "active_corpus_receipt_path", "") or ""
    ).strip()
    configured_schema = str(
        getattr(settings, "active_corpus_manifest_schema", "") or ""
    ).strip()
    if not all((root_value, corpus_id, manifest_value, receipt_value, configured_schema)):
        raise _error(
            "active_corpus_not_configured",
            "Active corpus identity, manifest, receipt, and schema must be explicit.",
        )

    root = _resolve_path(root_value)
    manifest_path = _resolve_path(manifest_value)
    receipt_path = _resolve_path(receipt_value)
    _require_directory(root, "artifact root")
    _require_within(root, manifest_path, "manifest")
    _require_within(root, receipt_path, "receipt")

    if mode == MagicForgeMode.BOOTSTRAP:
        return _load_bootstrap(
            settings=settings,
            root=root,
            corpus_id=corpus_id,
            manifest_path=manifest_path,
            receipt_path=receipt_path,
            configured_schema=configured_schema,
        )
    return _load_production(
        settings=settings,
        root=root,
        corpus_id=corpus_id,
        manifest_path=manifest_path,
        receipt_path=receipt_path,
        configured_schema=configured_schema,
    )


def _load_demo(settings: Settings) -> ActiveCorpus:
    """Load only the committed self-authored Demo corpus.

    All environment-provided corpus paths are deliberately ignored here.  That
    makes it impossible for the Demo profile to fall through to a private
    ``research/runs`` artifact, even when a developer's .env contains legacy
    Bootstrap settings.
    """

    corpus_path = (PROJECT_ROOT / "data/demo/corpus.json").resolve()
    expected_root = (PROJECT_ROOT / "data/demo").resolve()
    if corpus_path.parent != expected_root:
        raise _error(
            "demo_corpus_isolation_failed",
            "The Demo corpus path escaped its committed data directory.",
        )
    try:
        bundle = load_demo_bundle(corpus_path)
    except DemoCorpusError as exc:
        raise _error(
            "demo_corpus_invalid",
            "The committed Demo corpus is missing or invalid.",
        ) from exc
    if (
        bundle.spec.corpus_id != DEMO_CORPUS_ID
        or bundle.spec.collection_name != DEMO_COLLECTION_NAME
        or bundle.spec.schema_version != DEMO_CORPUS_SCHEMA_VERSION
    ):
        raise _error(
            "demo_corpus_identity_mismatch",
            "The committed Demo corpus has an unsupported identity.",
        )
    generated_at = bundle.spec.generated_at.isoformat().replace("+00:00", "Z")
    return ActiveCorpus(
        mode=MagicForgeMode.BOOTSTRAP,
        corpus_id=DEMO_CORPUS_ID,
        manifest_schema_version=DEMO_CORPUS_SCHEMA_VERSION,
        projection_schema_version=BOOTSTRAP_PROJECTION_SCHEMA_VERSION,
        manifest_id=bundle.manifest_id,
        manifest_hash=bundle.manifest_hash,
        manifest_path=corpus_path,
        receipt_id=bundle.receipt_id,
        receipt_path=corpus_path,
        collection_name=DEMO_COLLECTION_NAME,
        storage_kind="remote" if settings.demo_qdrant_url else "local",
        # Direct source/test runs stay process-local.  The public Compose
        # profile supplies an explicitly local, credential-free Qdrant URL so
        # the API and Doctor inspect the same seeded collection.
        local_storage_path=None,
        server_url=(
            settings.demo_qdrant_url
            if settings.demo_qdrant_url
            else None
        ),
        artifact_root=expected_root,
        run_summary_path=None,
        smoke_report_path=None,
        expected_point_ids=bundle.expected_point_ids,
        expected_payload_checksums=tuple(sorted(bundle.payload_checksums.items())),
        expected_point_count=len(bundle.projections),
        embedding_dimension=settings.embedding_dimension,
        authorized=False,
        bootstrap_generated=True,
        human_verified=False,
        generated_at=generated_at,
        ingested_at=generated_at,
    )


def _load_bootstrap(
    *,
    settings: Settings,
    root: Path,
    corpus_id: str,
    manifest_path: Path,
    receipt_path: Path,
    configured_schema: str,
) -> ActiveCorpus:
    if corpus_id != AUTHORITATIVE_BOOTSTRAP_CORPUS_ID:
        raise _error(
            "active_corpus_identity_mismatch",
            "Bootstrap mode only supports the authoritative bootstrap-002 corpus.",
        )

    expected_manifest = (root / _BOOTSTRAP_MANIFEST_RELATIVE).resolve()
    expected_receipt = (root / _BOOTSTRAP_RECEIPT_RELATIVE).resolve()
    if manifest_path != expected_manifest or receipt_path != expected_receipt:
        raise _error(
            "active_corpus_authority_mismatch",
            "Bootstrap mode requires the authoritative v0.3 manifest and receipt.",
        )

    summary_path = (root / _BOOTSTRAP_SUMMARY_RELATIVE).resolve()
    smoke_path = (root / _BOOTSTRAP_SMOKE_RELATIVE).resolve()
    registry_path = (root / "knowledge_nodes/_canonical_registry.json").resolve()
    evidence_path = (root / "evidence_cards").resolve()
    sources_path = (root / "sources").resolve()
    for path, label in (
        (manifest_path, "authoritative manifest"),
        (receipt_path, "authoritative receipt"),
        (summary_path, "authoritative run summary"),
        (smoke_path, "authoritative retrieval smoke report"),
        (registry_path, "canonical registry"),
    ):
        _require_file(path, label)
    _require_directory(evidence_path, "Evidence Card artifact directory")
    _require_directory(sources_path, "source artifact directory")

    manifest = _read_mapping(manifest_path, "authoritative manifest")
    receipt = _read_mapping(receipt_path, "authoritative receipt")
    summary = _read_mapping(summary_path, "authoritative run summary")
    smoke = _read_mapping(smoke_path, "authoritative retrieval smoke report")

    if configured_schema != BOOTSTRAP_MANIFEST_SCHEMA_VERSION:
        raise _error(
            "active_corpus_schema_mismatch",
            "Configured Bootstrap manifest schema is unsupported.",
        )
    if _text(manifest, "schema_version", "manifest") != configured_schema:
        raise _error(
            "active_corpus_schema_mismatch",
            "Bootstrap manifest schema does not match runtime configuration.",
        )

    raw_projections = _sequence(manifest, "projections", "manifest")
    try:
        projections = TypeAdapter(list[BootstrapQdrantProjection]).validate_python(
            raw_projections
        )
    except ValidationError as exc:
        raise _error(
            "active_corpus_manifest_invalid",
            "Bootstrap manifest projections do not satisfy the supported schema.",
        ) from exc
    projection_schemas = {item.schema_version for item in projections}
    if projection_schemas != {BOOTSTRAP_PROJECTION_SCHEMA_VERSION}:
        raise _error(
            "active_corpus_projection_schema_mismatch",
            "Bootstrap projection schema is unsupported.",
        )

    point_ids = tuple(item.knowledge_unit_id for item in projections)
    if len(point_ids) != len(set(point_ids)):
        raise _error(
            "active_corpus_point_identity_mismatch",
            "Bootstrap manifest contains duplicate point identifiers.",
        )
    if tuple(_string_sequence(manifest, "expected_point_ids", "manifest")) != point_ids:
        raise _error(
            "active_corpus_point_identity_mismatch",
            "Bootstrap manifest point identifiers do not match its projections.",
        )
    if _integer(manifest, "expected_point_count", "manifest") != len(point_ids):
        raise _error(
            "active_corpus_point_count_mismatch",
            "Bootstrap manifest point count does not match its projections.",
        )

    collection = _text(manifest, "collection_name", "manifest")
    if collection != AUTHORITATIVE_BOOTSTRAP_COLLECTION:
        raise _error(
            "active_corpus_collection_mismatch",
            "Bootstrap manifest targets an unsupported collection.",
        )
    calculated_hash = _json_hash(
        {
            "schema_version": configured_schema,
            "collection_name": collection,
            "projections": sorted(
                (item.knowledge_unit_id, item.payload_checksum)
                for item in projections
            ),
        }
    )
    manifest_hash = _text(manifest, "manifest_hash", "manifest")
    manifest_id = _text(manifest, "id", "manifest")
    expected_manifest_id = str(
        uuid5(NAMESPACE_URL, f"magicforge:bootstrap-manifest:{calculated_hash}")
    )
    if manifest_hash != calculated_hash or manifest_id != expected_manifest_id:
        raise _error(
            "active_corpus_manifest_identity_mismatch",
            "Bootstrap manifest identity does not match its immutable contents.",
        )
    if (
        manifest.get("bootstrap_generated") is not True
        or manifest.get("human_authorized") is not False
    ):
        raise _error(
            "active_corpus_governance_mismatch",
            "Bootstrap manifest contains incompatible governance markers.",
        )
    if any(
        not item.bootstrap_generated
        or item.human_verified
        or item.approved
        or item.claim_eligibility
        or item.storage_permission
        or item.review_status.value != "bootstrap"
        or item.verification_status != "unverified"
        or item.secret_exposure_rank > 1
        for item in projections
    ):
        raise _error(
            "active_corpus_governance_mismatch",
            "Bootstrap projections contain incompatible governance markers.",
        )

    receipt_manifest_id = _text(receipt, "manifest_id", "receipt")
    receipt_manifest_hash = _text(receipt, "manifest_hash", "receipt")
    receipt_collection = _text(receipt, "collection_name", "receipt")
    receipt_id = _text(receipt, "id", "receipt")
    receipt_points = tuple(_string_sequence(receipt, "point_ids", "receipt"))
    expected_receipt_id = str(
        uuid5(
            NAMESPACE_URL,
            f"magicforge:bootstrap-receipt:{manifest_id}:{manifest_hash}",
        )
    )
    if (
        receipt_manifest_id != manifest_id
        or receipt_manifest_hash != manifest_hash
        or receipt_collection != collection
        or receipt_id != expected_receipt_id
        or receipt_points != point_ids
        or len(receipt_points) != len(set(receipt_points))
    ):
        raise _error(
            "active_corpus_receipt_mismatch",
            "Bootstrap receipt does not match the active manifest.",
        )
    if (
        receipt.get("bootstrap_generated") is not True
        or receipt.get("human_verified") is not False
    ):
        raise _error(
            "active_corpus_governance_mismatch",
            "Bootstrap receipt contains incompatible governance markers.",
        )

    _validate_bootstrap_summary(
        summary=summary,
        corpus_id=corpus_id,
        collection=collection,
        manifest_id=manifest_id,
        manifest_hash=manifest_hash,
        receipt_id=receipt_id,
        point_count=len(point_ids),
    )
    _validate_bootstrap_smoke(
        smoke=smoke,
        collection=collection,
        point_count=len(point_ids),
    )
    storage_kind, local_path, server_url = _validate_storage(
        settings=settings,
        mode=MagicForgeMode.BOOTSTRAP,
        artifact_root=root,
        collection=collection,
    )

    return ActiveCorpus(
        mode=MagicForgeMode.BOOTSTRAP,
        corpus_id=corpus_id,
        manifest_schema_version=configured_schema,
        projection_schema_version=BOOTSTRAP_PROJECTION_SCHEMA_VERSION,
        manifest_id=manifest_id,
        manifest_hash=manifest_hash,
        manifest_path=manifest_path,
        receipt_id=receipt_id,
        receipt_path=receipt_path,
        collection_name=collection,
        storage_kind=storage_kind,
        local_storage_path=local_path,
        server_url=server_url,
        artifact_root=root,
        run_summary_path=summary_path,
        smoke_report_path=smoke_path,
        expected_point_ids=point_ids,
        expected_payload_checksums=tuple(
            (item.knowledge_unit_id, item.payload_checksum)
            for item in projections
        ),
        expected_point_count=len(point_ids),
        embedding_dimension=int(settings.embedding_dimension),
        authorized=False,
        bootstrap_generated=True,
        human_verified=False,
        generated_at=_text(summary, "generated_at", "run summary"),
        ingested_at=_text(receipt, "ingested_at", "receipt"),
    )


def _load_production(
    *,
    settings: Settings,
    root: Path,
    corpus_id: str,
    manifest_path: Path,
    receipt_path: Path,
    configured_schema: str,
) -> ActiveCorpus:
    if _looks_bootstrap(corpus_id) or _looks_bootstrap(root.name):
        raise _error(
            "active_corpus_mode_mismatch",
            "Production mode cannot use a Bootstrap corpus identity or artifact root.",
        )
    _require_file(manifest_path, "production manifest")
    _require_file(receipt_path, "production receipt")
    manifest_raw = _read_mapping(manifest_path, "production manifest")
    receipt_raw = _read_mapping(receipt_path, "production receipt")
    if _contains_bootstrap_marker(manifest_raw) or _contains_bootstrap_marker(receipt_raw):
        raise _error(
            "active_corpus_mode_mismatch",
            "Production mode rejected Bootstrap governance markers.",
        )
    if configured_schema != MANIFEST_SCHEMA_VERSION:
        raise _error(
            "active_corpus_schema_mismatch",
            "Configured Production manifest schema is unsupported.",
        )
    try:
        manifest = StorageManifest.model_validate(manifest_raw)
        receipt = IngestionReceipt.model_validate(receipt_raw)
    except ValidationError as exc:
        raise _error(
            "active_corpus_manifest_invalid",
            "Production manifest or receipt does not satisfy the authorized schema.",
        ) from exc
    if manifest.schema_version != configured_schema:
        raise _error(
            "active_corpus_schema_mismatch",
            "Production manifest schema does not match runtime configuration.",
        )
    if manifest.corpus_id != corpus_id:
        raise _error(
            "active_corpus_identity_mismatch",
            "Configured Production corpus does not match the authorized manifest.",
        )
    if manifest.status != ManifestStatus.INGESTED or manifest.authorization is None:
        raise _error(
            "active_corpus_not_authorized",
            "Production corpus requires an ingested, human-authorized manifest.",
        )
    if _looks_bootstrap(manifest.collection_name):
        raise _error(
            "active_corpus_mode_mismatch",
            "Production mode cannot target a Bootstrap collection.",
        )
    configured_collection = _configured_collection(settings, MagicForgeMode.PRODUCTION)
    if configured_collection != manifest.collection_name:
        raise _error(
            "active_corpus_collection_mismatch",
            "Configured Qdrant collection does not match the active manifest.",
        )
    expected_checksums = {
        item.knowledge_unit_id: item.payload_checksum for item in manifest.projections
    }
    if (
        receipt.manifest_id != manifest.id
        or receipt.manifest_hash != manifest.manifest_hash
        or receipt.corpus_id != manifest.corpus_id
        or receipt.collection_name != manifest.collection_name
        or tuple(receipt.point_ids) != tuple(manifest.expected_point_ids)
        or receipt.payload_checksums != expected_checksums
        or receipt.success is not True
    ):
        raise _error(
            "active_corpus_receipt_mismatch",
            "Production receipt does not match the ingested manifest.",
        )
    storage_kind, local_path, server_url = _validate_storage(
        settings=settings,
        mode=MagicForgeMode.PRODUCTION,
        artifact_root=root,
        collection=manifest.collection_name,
    )
    return ActiveCorpus(
        mode=MagicForgeMode.PRODUCTION,
        corpus_id=corpus_id,
        manifest_schema_version=manifest.schema_version,
        projection_schema_version=PROJECTION_SCHEMA_VERSION,
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        manifest_path=manifest_path,
        receipt_id=receipt.id,
        receipt_path=receipt_path,
        collection_name=manifest.collection_name,
        storage_kind=storage_kind,
        local_storage_path=local_path,
        server_url=server_url,
        artifact_root=root,
        run_summary_path=None,
        smoke_report_path=None,
        expected_point_ids=tuple(manifest.expected_point_ids),
        expected_payload_checksums=tuple(expected_checksums.items()),
        expected_point_count=manifest.expected_point_count,
        embedding_dimension=int(settings.embedding_dimension),
        authorized=True,
        bootstrap_generated=False,
        human_verified=True,
        generated_at=manifest.created_at.isoformat(),
        ingested_at=receipt.ingested_at.isoformat(),
    )


def _validate_bootstrap_summary(
    *,
    summary: Mapping[str, Any],
    corpus_id: str,
    collection: str,
    manifest_id: str,
    manifest_hash: str,
    receipt_id: str,
    point_count: int,
) -> None:
    manifest_summary = _mapping(summary, "manifest", "run summary")
    counts = _mapping(summary, "counts", "run summary")
    safety = _mapping(summary, "safety", "run summary")
    if (
        _text(summary, "run_id", "run summary") != corpus_id
        or _text(summary, "mode", "run summary") != MagicForgeMode.BOOTSTRAP.value
        or _text(summary, "collection", "run summary") != collection
        or _text(manifest_summary, "id", "run summary manifest") != manifest_id
        or _text(manifest_summary, "hash", "run summary manifest") != manifest_hash
        or _text(manifest_summary, "receipt_id", "run summary manifest") != receipt_id
        or _integer(manifest_summary, "point_count", "run summary manifest")
        != point_count
        or _integer(counts, "qdrant_points_created", "run summary") != point_count
        or _integer(safety, "bootstrap_generated_points", "run summary")
        != point_count
        or safety.get("human_verified_points") != 0
        or safety.get("approved_points") != 0
        or safety.get("storage_permission_points") != 0
        or safety.get("production_collection_touched") is not False
    ):
        raise _error(
            "active_corpus_summary_mismatch",
            "Bootstrap run summary does not match the active manifest and receipt.",
        )


def _validate_bootstrap_smoke(
    *,
    smoke: Mapping[str, Any],
    collection: str,
    point_count: int,
) -> None:
    if (
        _text(smoke, "collection", "retrieval smoke report") != collection
        or _integer(smoke, "collection_count", "retrieval smoke report")
        != point_count
        or smoke.get("all_returned_hits_bootstrap_safe") is not True
        or smoke.get("safety_excluded_points_found") != 0
        or smoke.get("production_collection_present") is not False
    ):
        raise _error(
            "active_corpus_smoke_mismatch",
            "Bootstrap retrieval smoke report does not match the active corpus.",
        )


def _validate_storage(
    *,
    settings: Settings,
    mode: MagicForgeMode,
    artifact_root: Path,
    collection: str,
) -> tuple[StorageKind, Path | None, str | None]:
    storage_kind = str(
        getattr(settings, "active_qdrant_storage_kind", "") or ""
    ).strip().casefold()
    if storage_kind not in {"local", "remote"}:
        raise _error(
            "active_corpus_storage_invalid",
            "Qdrant storage kind must be either local or remote.",
        )
    if storage_kind == "remote":
        server_url = str(getattr(settings, "qdrant_url", "") or "").strip()
        if not server_url:
            raise _error(
                "active_corpus_storage_invalid",
                "Remote Qdrant requires an explicit server URL.",
            )
        return "remote", None, server_url

    local_value = str(
        getattr(settings, "active_qdrant_local_path", "") or ""
    ).strip()
    if not local_value:
        raise _error(
            "active_corpus_storage_invalid",
            "Local Qdrant requires an explicit storage root.",
        )
    local_path = _resolve_path(local_value)
    if mode == MagicForgeMode.BOOTSTRAP:
        expected = (artifact_root / _BOOTSTRAP_LOCAL_STORAGE_RELATIVE).resolve()
        if local_path != expected:
            raise _error(
                "active_corpus_storage_mismatch",
                "Bootstrap local storage does not belong to the active v0.3 corpus.",
            )
    _require_directory(local_path, "Qdrant storage root")
    meta = _read_mapping(local_path / "meta.json", "Qdrant storage metadata")
    collections = _mapping(meta, "collections", "Qdrant storage metadata")
    collection_config = collections.get(collection)
    if not isinstance(collection_config, Mapping):
        raise _error(
            "active_corpus_collection_unavailable",
            "Expected Qdrant collection is absent from local storage metadata.",
        )
    vectors = collection_config.get("vectors")
    if not isinstance(vectors, Mapping):
        raise _error(
            "active_corpus_vector_mismatch",
            "Qdrant vector configuration is malformed.",
        )
    if (
        vectors.get("size") != int(settings.embedding_dimension)
        or str(vectors.get("distance") or "").casefold() != "cosine"
    ):
        raise _error(
            "active_corpus_vector_mismatch",
            "Qdrant vector configuration is incompatible with the active corpus.",
        )
    storage_file = local_path / "collection" / collection / "storage.sqlite"
    _require_file(storage_file, "Qdrant collection storage")
    return "local", local_path, None


def _configured_collection(settings: Settings, mode: MagicForgeMode) -> str:
    configured = getattr(settings, "active_qdrant_collection_name", "")
    if configured:
        return str(configured).strip()
    field = (
        "qdrant_bootstrap_collection_name"
        if mode == MagicForgeMode.BOOTSTRAP
        else "qdrant_collection_name"
    )
    return str(getattr(settings, field, "") or "").strip()


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _require_within(root: Path, path: Path, label: str) -> None:
    if not path.is_relative_to(root):
        raise _error(
            "active_corpus_artifact_mismatch",
            f"Configured {label} does not belong to the active artifact root.",
        )


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise _error(
            "active_corpus_missing_artifact",
            f"Required {label} is missing.",
        )


def _require_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise _error(
            "active_corpus_missing_artifact",
            f"Required {label} is missing.",
        )


def _read_mapping(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error(
            "active_corpus_artifact_invalid",
            f"Required {label} could not be read as JSON.",
        ) from exc
    if not isinstance(value, Mapping):
        raise _error(
            "active_corpus_artifact_invalid",
            f"Required {label} is not a JSON object.",
        )
    return value


def _mapping(value: Mapping[str, Any], key: str, label: str) -> Mapping[str, Any]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise _error(
            "active_corpus_artifact_invalid",
            f"Required {label} field is malformed: {key}.",
        )
    return result


def _sequence(value: Mapping[str, Any], key: str, label: str) -> Sequence[Any]:
    result = value.get(key)
    if not isinstance(result, list):
        raise _error(
            "active_corpus_artifact_invalid",
            f"Required {label} field is malformed: {key}.",
        )
    return result


def _string_sequence(value: Mapping[str, Any], key: str, label: str) -> list[str]:
    items = _sequence(value, key, label)
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise _error(
            "active_corpus_artifact_invalid",
            f"Required {label} field is malformed: {key}.",
        )
    for item in items:
        try:
            UUID(item)
        except ValueError as exc:
            raise _error(
                "active_corpus_artifact_invalid",
                f"Required {label} field contains an invalid identifier: {key}.",
            ) from exc
    return list(items)


def _text(value: Mapping[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise _error(
            "active_corpus_artifact_invalid",
            f"Required {label} field is malformed: {key}.",
        )
    return result


def _integer(value: Mapping[str, Any], key: str, label: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise _error(
            "active_corpus_artifact_invalid",
            f"Required {label} field is malformed: {key}.",
        )
    return result


def _contains_bootstrap_marker(value: Mapping[str, Any]) -> bool:
    return (
        value.get("bootstrap_generated") is not None
        or _looks_bootstrap(str(value.get("schema_version") or ""))
        or _looks_bootstrap(str(value.get("collection_name") or ""))
    )


def _looks_bootstrap(value: str) -> bool:
    return "bootstrap" in value.casefold()


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error(code: str, message: str) -> ActiveCorpusConfigurationError:
    return ActiveCorpusConfigurationError(code, message)
