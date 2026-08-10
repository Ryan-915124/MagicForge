"""Governed persistence adapter for reproducible Production extraction jobs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from knowledge.governance import ExtractionPermission
from persistence.idempotency import canonical_payload_hash
from persistence.models import (
    ExtractionAttempt,
    ExtractionAttemptStatus,
    ExtractionJob,
    ExtractionJobStatus,
    ExtractionProposalArtifact,
    CitationVerificationEvidence,
    SourcePermissionRequest,
    SourceReviewDecision,
    SourceVersion,
)
from research.review.source_permission_policy import (
    SourcePermissionPolicyError,
    require_current_source_approval,
)


class ExtractionPersistenceError(ValueError):
    """Safe base error for extraction persistence invariants."""


class ExtractionAuthorizationError(ExtractionPersistenceError):
    """The requested Source chain is not currently approved for extraction."""


class ExtractionStateError(ExtractionPersistenceError):
    """The job cannot perform the requested lifecycle transition."""


class ExtractionIntegrityError(ExtractionPersistenceError):
    """Persisted extraction identity or checksum state is inconsistent."""


_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_FORBIDDEN_SNAPSHOT_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "cookie",
    "credentials",
    "glmapikey",
    "password",
    "rawexception",
    "refreshtoken",
    "secret",
    "secretkey",
    "sessiontoken",
    "stacktrace",
    "token",
    "traceback",
}
_MAX_SNAPSHOT_BYTES = 131_072
_MAX_PROPOSAL_BYTES = 1_048_576
_FORBIDDEN_PROPOSAL_KEYS = {
    "candidatecontent",
    "chunkcontent",
    "chunktext",
    "content",
    "contenttext",
    "documenttext",
    "fulltext",
    "rawchunk",
    "rawchunks",
    "rawcontent",
    "sourcecontent",
    "sourcetext",
}


def deterministic_extraction_job_key(payload: dict[str, Any]) -> str:
    """Return the deterministic idempotency key for an exact job spec."""

    return canonical_payload_hash(payload)


class ExtractionJobRepository:
    """Persist job envelopes and append-only terminal attempts.

    The caller owns the surrounding transaction. This adapter never invokes
    the LLM and never persists source output or exception messages.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_job(
        self,
        *,
        source_version_id: UUID,
        source_review_decision_id: UUID,
        source_permission_request_id: UUID,
        requested_by_user_id: UUID,
        model_name: str,
        model_snapshot: dict[str, Any],
        prompt_version: str,
        prompt_snapshot: dict[str, Any],
        output_schema_version: str,
        schema_snapshot: dict[str, Any],
        chunk_config_snapshot: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[ExtractionJob, bool]:
        source = self._session.get(SourceVersion, source_version_id)
        review = self._session.get(SourceReviewDecision, source_review_decision_id)
        permission = self._session.get(
            SourcePermissionRequest, source_permission_request_id
        )
        if source is None or review is None or permission is None:
            raise ExtractionAuthorizationError(
                "the approved Source extraction chain is incomplete"
            )
        if (
            review.source_version_id != source.id
            or review.source_permission_request_id != permission.id
            or permission.source_version_id != source.id
        ):
            raise ExtractionAuthorizationError(
                "the Source review and permission do not bind the same version"
            )

        newest_version_number = self._session.scalar(
            select(func.max(SourceVersion.version_number)).where(
                SourceVersion.source_id == source.source_id
            )
        )
        if newest_version_number != source.version_number:
            raise ExtractionAuthorizationError(
                "only the newest immutable Source version may be extracted"
            )
        try:
            current_permission = require_current_source_approval(
                self._session, source, review
            )
        except SourcePermissionPolicyError as exc:
            raise ExtractionAuthorizationError(str(exc)) from exc
        if current_permission.id != permission.id:
            raise ExtractionAuthorizationError(
                "the approved Source permission does not match the requested chain"
            )
        if review.extraction_permission not in {
            ExtractionPermission.SELECTED_SECTIONS,
            ExtractionPermission.FULL_TEXT,
        }:
            raise ExtractionAuthorizationError(
                "the current Source review does not authorize extraction"
            )
        actual_content_hash = hashlib.sha256(
            source.content_text.encode("utf-8")
        ).hexdigest()
        if actual_content_hash != source.content_hash:
            raise ExtractionAuthorizationError(
                "the immutable Source content failed its integrity check"
            )
        if review.citation_verification_evidence_id is None:
            raise ExtractionAuthorizationError(
                "the Source approval lacks citation verification evidence"
            )
        citation_evidence = self._session.get(
            CitationVerificationEvidence,
            review.citation_verification_evidence_id,
        )
        if (
            citation_evidence is None
            or citation_evidence.source_version_id != source.id
            or citation_evidence.citation_id != source.citation_id
        ):
            raise ExtractionAuthorizationError(
                "citation verification is not bound to the exact Source version"
            )

        normalized_model = _bounded_text(model_name, "model_name", 255)
        normalized_prompt_version = _bounded_text(
            prompt_version, "prompt_version", 120
        )
        normalized_schema_version = _bounded_text(
            output_schema_version, "output_schema_version", 120
        )
        safe_model = _safe_snapshot(model_snapshot, "model_snapshot")
        provider = safe_model.get("provider")
        if provider is not None and provider != "GLM":
            raise ExtractionPersistenceError("model_snapshot provider must be GLM")
        snapshotted_model = safe_model.get("model")
        if snapshotted_model is not None and snapshotted_model != normalized_model:
            raise ExtractionPersistenceError(
                "model_snapshot model must match model_name"
            )
        safe_model = {
            **safe_model,
            "provider": "GLM",
            "model": normalized_model,
        }
        safe_prompt = _safe_snapshot(prompt_snapshot, "prompt_snapshot")
        safe_schema = _safe_snapshot(schema_snapshot, "schema_snapshot")
        safe_chunk = _safe_snapshot(
            chunk_config_snapshot, "chunk_config_snapshot"
        )
        safe_scope = _safe_snapshot(review.extraction_scope, "scope_snapshot")

        configuration = {
            "scope_snapshot": safe_scope,
            "sensitivity": review.sensitive_information_level.value,
            "model_snapshot": safe_model,
            "prompt_version": normalized_prompt_version,
            "prompt_snapshot": safe_prompt,
            "output_schema_version": normalized_schema_version,
            "schema_snapshot": safe_schema,
            "chunk_config_snapshot": safe_chunk,
        }
        configuration_checksum = canonical_payload_hash(configuration)
        identity = {
            "source_version_id": str(source.id),
            "source_review_decision_id": str(review.id),
            "source_permission_request_id": str(permission.id),
            "content_hash": source.content_hash,
            "configuration_checksum": configuration_checksum,
        }
        idempotency_key = deterministic_extraction_job_key(identity)
        existing = self._session.scalar(
            select(ExtractionJob).where(
                ExtractionJob.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            _verify_existing_job(existing, identity, configuration_checksum)
            return existing, False

        aware_now = _aware_utc(now or datetime.now(UTC))
        job = ExtractionJob(
            source_version_id=source.id,
            source_review_decision_id=review.id,
            source_permission_request_id=permission.id,
            content_hash=source.content_hash,
            scope_snapshot=safe_scope,
            sensitivity=review.sensitive_information_level,
            llm_provider="GLM",
            model_name=normalized_model,
            model_snapshot=safe_model,
            prompt_version=normalized_prompt_version,
            prompt_snapshot=safe_prompt,
            output_schema_version=normalized_schema_version,
            schema_snapshot=safe_schema,
            chunk_config_snapshot=safe_chunk,
            configuration_checksum=configuration_checksum,
            idempotency_key=idempotency_key,
            status=ExtractionJobStatus.QUEUED,
            attempt_count=0,
            requested_by_user_id=requested_by_user_id,
            created_at=aware_now,
            updated_at=aware_now,
        )
        if self._session.get_bind().dialect.name == "postgresql":
            try:
                with self._session.begin_nested():
                    self._session.add(job)
                    self._session.flush()
            except IntegrityError:
                winner = self._session.scalar(
                    select(ExtractionJob).where(
                        ExtractionJob.idempotency_key == idempotency_key
                    )
                )
                if winner is None:
                    raise
                _verify_existing_job(winner, identity, configuration_checksum)
                return winner, False
        else:
            # SQLite is confined to isolated tests. Releasing its first
            # SAVEPOINT can commit independently of the caller's UoW.
            self._session.add(job)
            self._session.flush()
        return job, True

    def get(self, job_id: UUID, *, for_update: bool = False) -> ExtractionJob | None:
        statement = select(ExtractionJob).where(ExtractionJob.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def start_attempt(
        self, job_id: UUID, *, now: datetime | None = None
    ) -> int:
        job = self._required_job(job_id, for_update=True)
        if job.status not in {ExtractionJobStatus.QUEUED, ExtractionJobStatus.FAILED}:
            raise ExtractionStateError(
                "only queued or failed extraction jobs may start an attempt"
            )
        aware_now = _aware_utc(now or datetime.now(UTC))
        job.status = ExtractionJobStatus.RUNNING
        job.attempt_count += 1
        job.result_checksum = None
        job.error_code = None
        job.started_at = aware_now
        job.finished_at = None
        job.updated_at = aware_now
        self._session.flush()
        return job.attempt_count

    def succeed_attempt(
        self,
        job_id: UUID,
        *,
        attempt_number: int,
        result_checksum: str,
        now: datetime | None = None,
    ) -> ExtractionAttempt:
        checksum = _sha256(result_checksum, "result_checksum")
        return self._finish_attempt(
            job_id,
            attempt_number=attempt_number,
            status=ExtractionAttemptStatus.SUCCEEDED,
            result_checksum=checksum,
            error_code=None,
            now=now,
        )

    def fail_attempt(
        self,
        job_id: UUID,
        *,
        attempt_number: int,
        error_code: str,
        now: datetime | None = None,
    ) -> ExtractionAttempt:
        safe_code = _safe_error_code(error_code)
        return self._finish_attempt(
            job_id,
            attempt_number=attempt_number,
            status=ExtractionAttemptStatus.FAILED,
            result_checksum=None,
            error_code=safe_code,
            now=now,
        )

    def list_attempts(self, job_id: UUID) -> list[ExtractionAttempt]:
        return list(
            self._session.scalars(
                select(ExtractionAttempt)
                .where(ExtractionAttempt.extraction_job_id == job_id)
                .order_by(ExtractionAttempt.attempt_number)
            )
        )

    def store_proposal_artifact(
        self,
        job_id: UUID,
        *,
        proposal_id: UUID,
        extraction_run_id: str,
        schema_version: str,
        payload: dict[str, Any],
        payload_checksum: str,
        now: datetime | None = None,
    ) -> tuple[ExtractionProposalArtifact, bool]:
        """Persist one sanitized validated proposal before Claim submission.

        A new artifact can only be produced by the active attempt of a running
        job. Replays after failure/success return the exact existing artifact,
        allowing Claim submission to resume without another GLM call.
        """

        job = self._required_job(job_id, for_update=True)
        safe_payload = _safe_proposal_payload(payload)
        expected_checksum = canonical_payload_hash(safe_payload)
        supplied_checksum = _sha256(payload_checksum, "payload_checksum")
        if supplied_checksum != expected_checksum:
            raise ExtractionIntegrityError(
                "proposal payload checksum does not match its canonical payload"
            )
        normalized_run_id = _bounded_text(
            extraction_run_id, "extraction_run_id", 255
        )
        normalized_schema = _bounded_text(schema_version, "schema_version", 120)
        existing = self._session.scalar(
            select(ExtractionProposalArtifact).where(
                ExtractionProposalArtifact.extraction_job_id == job.id
            )
        )
        if existing is not None:
            if (
                existing.source_version_id == job.source_version_id
                and existing.proposal_id == proposal_id
                and existing.extraction_run_id == normalized_run_id
                and existing.schema_version == normalized_schema
                and existing.payload_checksum == supplied_checksum
                and canonical_payload_hash(existing.payload) == supplied_checksum
                and existing.payload == safe_payload
                and existing.sensitivity == job.sensitivity
            ):
                return existing, False
            raise ExtractionIntegrityError(
                "the extraction job already has another proposal artifact"
            )
        if job.status != ExtractionJobStatus.RUNNING or job.attempt_count <= 0:
            raise ExtractionStateError(
                "a new proposal artifact requires an active extraction attempt"
            )
        artifact = ExtractionProposalArtifact(
            extraction_job_id=job.id,
            source_version_id=job.source_version_id,
            attempt_number=job.attempt_count,
            proposal_id=proposal_id,
            extraction_run_id=normalized_run_id,
            schema_version=normalized_schema,
            payload=safe_payload,
            payload_checksum=supplied_checksum,
            sensitivity=job.sensitivity,
            created_at=_aware_utc(now or datetime.now(UTC)),
        )
        self._session.add(artifact)
        self._session.flush()
        return artifact, True

    def get_proposal_artifact(
        self, job_id: UUID
    ) -> ExtractionProposalArtifact | None:
        artifact = self._session.scalar(
            select(ExtractionProposalArtifact).where(
                ExtractionProposalArtifact.extraction_job_id == job_id
            )
        )
        if artifact is None:
            return None
        job = self._required_job(job_id, for_update=False)
        if (
            artifact.source_version_id != job.source_version_id
            or artifact.sensitivity != job.sensitivity
            or canonical_payload_hash(artifact.payload) != artifact.payload_checksum
        ):
            raise ExtractionIntegrityError(
                "the persisted proposal artifact failed its integrity check"
            )
        return artifact

    def _finish_attempt(
        self,
        job_id: UUID,
        *,
        attempt_number: int,
        status: ExtractionAttemptStatus,
        result_checksum: str | None,
        error_code: str | None,
        now: datetime | None,
    ) -> ExtractionAttempt:
        if attempt_number <= 0:
            raise ExtractionStateError("attempt_number must be positive")
        job = self._required_job(job_id, for_update=True)
        existing = self._session.scalar(
            select(ExtractionAttempt).where(
                ExtractionAttempt.extraction_job_id == job.id,
                ExtractionAttempt.attempt_number == attempt_number,
            )
        )
        if existing is not None:
            if (
                existing.status == status
                and existing.result_checksum == result_checksum
                and existing.error_code == error_code
            ):
                if status == ExtractionAttemptStatus.SUCCEEDED:
                    artifact = self.get_proposal_artifact(job.id)
                    if (
                        artifact is None
                        or artifact.payload_checksum != result_checksum
                    ):
                        raise ExtractionIntegrityError(
                            "successful extraction lost its exact proposal artifact"
                        )
                return existing
            raise ExtractionIntegrityError(
                "the extraction attempt number already has another outcome"
            )
        if job.status != ExtractionJobStatus.RUNNING:
            raise ExtractionStateError("only a running extraction job may finish")
        if job.attempt_count != attempt_number or job.started_at is None:
            raise ExtractionStateError(
                "attempt_number does not match the active extraction attempt"
            )
        if status == ExtractionAttemptStatus.SUCCEEDED:
            artifact = self.get_proposal_artifact(job.id)
            if artifact is None or artifact.payload_checksum != result_checksum:
                raise ExtractionIntegrityError(
                    "successful extraction requires its exact persisted proposal artifact"
                )

        finished_at = _aware_utc(now or datetime.now(UTC))
        started_at = _aware_utc(job.started_at)
        if finished_at < started_at:
            raise ExtractionStateError("attempt completion precedes its start")
        attempt_payload = {
            "extraction_job_id": str(job.id),
            "attempt_number": attempt_number,
            "status": status.value,
            "result_checksum": result_checksum,
            "error_code": error_code,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
        }
        attempt = ExtractionAttempt(
            id=uuid4(),
            extraction_job_id=job.id,
            attempt_number=attempt_number,
            status=status,
            result_checksum=result_checksum,
            error_code=error_code,
            attempt_checksum=canonical_payload_hash(attempt_payload),
            started_at=started_at,
            finished_at=finished_at,
            created_at=finished_at,
        )
        self._session.add(attempt)
        job.status = (
            ExtractionJobStatus.SUCCEEDED
            if status == ExtractionAttemptStatus.SUCCEEDED
            else ExtractionJobStatus.FAILED
        )
        job.result_checksum = result_checksum
        job.error_code = error_code
        job.finished_at = finished_at
        job.updated_at = finished_at
        self._session.flush()
        return attempt

    def _required_job(self, job_id: UUID, *, for_update: bool) -> ExtractionJob:
        job = self.get(job_id, for_update=for_update)
        if job is None:
            raise ExtractionStateError("extraction job was not found")
        return job


def _verify_existing_job(
    job: ExtractionJob,
    identity: dict[str, Any],
    configuration_checksum: str,
) -> None:
    if (
        str(job.source_version_id) != identity["source_version_id"]
        or str(job.source_review_decision_id)
        != identity["source_review_decision_id"]
        or str(job.source_permission_request_id)
        != identity["source_permission_request_id"]
        or job.content_hash != identity["content_hash"]
        or job.configuration_checksum != configuration_checksum
    ):
        raise ExtractionIntegrityError(
            "the extraction idempotency key resolves to a different specification"
        )


def _safe_snapshot(
    value: dict[str, Any],
    field: str,
    *,
    maximum_bytes: int = _MAX_SNAPSHOT_BYTES,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExtractionPersistenceError(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        copied = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ExtractionPersistenceError(
            f"{field} must contain JSON-safe values"
        ) from exc
    if len(encoded) > maximum_bytes:
        raise ExtractionPersistenceError(f"{field} exceeds the safe size limit")
    _reject_sensitive_keys(copied, field)
    return copied


def _safe_proposal_payload(value: dict[str, Any]) -> dict[str, Any]:
    copied = _safe_snapshot(
        value, "proposal payload", maximum_bytes=_MAX_PROPOSAL_BYTES
    )
    encoded = json.dumps(
        copied,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_PROPOSAL_BYTES:
        raise ExtractionPersistenceError("proposal payload exceeds the safe size limit")
    _reject_proposal_source_material(copied)
    return copied


def _reject_proposal_source_material(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if normalized in _FORBIDDEN_PROPOSAL_KEYS:
                raise ExtractionPersistenceError(
                    "proposal payload must not contain Source text or raw chunks"
                )
            _reject_proposal_source_material(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_proposal_source_material(nested)


def _reject_sensitive_keys(value: Any, field: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if normalized in _FORBIDDEN_SNAPSHOT_KEYS or normalized.endswith(
                ("password", "token", "apikey", "secretkey")
            ):
                raise ExtractionPersistenceError(
                    f"{field} must not contain credentials or raw exception data"
                )
            _reject_sensitive_keys(nested, field)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_keys(nested, field)


def _safe_error_code(value: str) -> str:
    normalized = value.strip()
    if not _SAFE_ERROR_CODE.fullmatch(normalized):
        raise ExtractionPersistenceError(
            "error_code must be a lowercase machine code, not an exception message"
        )
    return normalized


def _sha256(value: str, field: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ExtractionPersistenceError(f"{field} must be a SHA-256 hex digest")
    return normalized


def _bounded_text(value: str, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ExtractionPersistenceError(f"{field} is required")
    if len(normalized) > maximum:
        raise ExtractionPersistenceError(f"{field} is too long")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "ExtractionAuthorizationError",
    "ExtractionIntegrityError",
    "ExtractionJobRepository",
    "ExtractionPersistenceError",
    "ExtractionStateError",
    "deterministic_extraction_job_key",
]
