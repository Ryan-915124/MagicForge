from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest

alembic = pytest.importorskip("alembic")
sa = pytest.importorskip("sqlalchemy")

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_TABLES = {
    "users",
    "roles",
    "user_roles",
    "sessions",
    "audit_events",
    "idempotency_records",
}
WORKFLOW_TABLES = {
    "sources",
    "source_versions",
    "source_permission_requests",
    "citation_verification_evidence",
    "source_review_decisions",
    "claim_candidates",
    "claim_review_decisions",
    "evidence_card_versions",
}
MAPPING_TABLES = {
    "mapping_proposals",
    "mapping_proposal_evidence",
    "mapping_validation_runs",
    "mapping_review_decisions",
    "knowledge_entities",
    "knowledge_node_versions",
    "knowledge_relationships",
    "knowledge_relationship_assertions",
}
STORAGE_TABLES = {
    "storage_manifests",
    "storage_manifest_items",
    "manifest_authorizations",
    "ingestion_operations",
    "ingestion_receipts",
    "corpus_versions",
    "active_corpus_pointers",
}
EXTRACTION_TABLES = {
    "extraction_jobs",
    "extraction_attempts",
    "extraction_proposal_artifacts",
}
ALL_TABLES = (
    FOUNDATION_TABLES
    | WORKFLOW_TABLES
    | MAPPING_TABLES
    | STORAGE_TABLES
    | EXTRACTION_TABLES
)
SEEDED_ROLE_IDS = {
    "reader": "284f9d33-7d47-5d81-829d-68ed9f925765",
    "reviewer": "0a8671f6-b41f-5c44-bda4-5f2be8c805f3",
    "operator": "b83b6fee-eba4-5db5-a70a-dc06a3ee00b4",
    "admin": "35112fa5-7e29-53fc-aa8e-099e72b95130",
}


def _alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    if database_url is not None:
        # An in-process attribute avoids putting a test credential into logs or
        # modifying the repository's environment files.
        config.attributes["database_url"] = database_url
    return config


@contextmanager
def _isolated_postgres_schema():
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip(
            "TEST_DATABASE_URL is not configured; PostgreSQL migration "
            "integration test was not run."
        )

    base_url = make_url(configured)
    if base_url.get_backend_name() != "postgresql":
        pytest.fail("TEST_DATABASE_URL must point to PostgreSQL, never SQLite.")

    schema_name = f"magicforge_migration_{uuid4().hex}"
    admin_engine = sa.create_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema_name}"'))

        query = dict(base_url.query)
        query["options"] = f"-csearch_path={schema_name}"
        isolated_url = base_url.set(query=query)
        yield isolated_url.render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            )
        admin_engine.dispose()


def test_alembic_has_one_linear_head() -> None:
    scripts = ScriptDirectory.from_config(_alembic_config())

    assert scripts.get_heads() == ["20260809_0007"]
    assert scripts.get_base() == "20260807_0001"


def test_offline_downgrade_sql_guards_permission_history() -> None:
    config = _alembic_config(
        "postgresql+psycopg://magicforge:placeholder@localhost/magicforge"
    )
    output = StringIO()
    config.output_buffer = output

    command.downgrade(
        config,
        "20260808_0006:20260808_0005",
        sql=True,
    )

    sql = output.getvalue()
    guard = "source permission history is not pure 0006 legacy backfill"
    assert guard in sql
    assert "source-permission-request-legacy-0.1" in sql
    assert "request.sequence_number IS DISTINCT FROM 1" in sql
    assert "request.supersedes_request_id IS NOT NULL" in sql
    assert "request.domain_schema_version IS DISTINCT FROM" in sql
    assert "request.requested_extraction_permission IS DISTINCT FROM" in sql
    assert "request.requested_storage_permission IS DISTINCT FROM" in sql
    assert "request.requested_scope_locators IS DISTINCT FROM" in sql
    assert "request.request_checksum IS DISTINCT FROM" in sql
    assert "FROM source_review_decisions" in sql
    assert sql.index(guard) < sql.index("DROP TABLE source_permission_requests")


def test_offline_upgrade_never_rewrites_source_review_history() -> None:
    config = _alembic_config(
        "postgresql+psycopg://magicforge:placeholder@localhost/magicforge"
    )
    output = StringIO()
    config.output_buffer = output

    command.upgrade(
        config,
        "20260808_0005:20260808_0006",
        sql=True,
    )

    sql = output.getvalue()
    guard = "0006 requires empty source_review_decisions"
    assert guard in sql
    assert sql.index(guard) < sql.index("CREATE TABLE source_permission_requests")
    assert "DROP TRIGGER source_review_decisions_immutable" not in sql
    assert "UPDATE source_review_decisions" not in sql
    assert "CREATE TRIGGER source_review_decisions_immutable" not in sql


def test_offline_extraction_migration_is_bound_and_fail_closed() -> None:
    config = _alembic_config(
        "postgresql+psycopg://magicforge:placeholder@localhost/magicforge"
    )
    output = StringIO()
    config.output_buffer = output

    command.upgrade(config, "20260808_0006:20260809_0007", sql=True)

    sql = output.getvalue()
    assert "CREATE TABLE extraction_jobs" in sql
    assert "CREATE TABLE extraction_attempts" in sql
    assert "CREATE TABLE extraction_proposal_artifacts" in sql
    assert "fk_extraction_job_source_version_hash" in sql
    assert "fk_extraction_job_approved_source_chain" in sql
    assert "extraction_attempts_immutable" in sql
    assert "extraction_proposal_artifacts_immutable" in sql
    assert "magicforge_guard_extraction_job" in sql

    output = StringIO()
    config.output_buffer = output
    command.downgrade(config, "20260809_0007:20260808_0006", sql=True)
    downgrade_sql = output.getvalue()
    guard = "0007 downgrade would destroy Production extraction history"
    assert guard in downgrade_sql
    assert downgrade_sql.index(guard) < downgrade_sql.index(
        "DROP TABLE extraction_attempts"
    )


def test_orm_metadata_exposes_the_foundation_tables() -> None:
    models = pytest.importorskip("persistence.models")

    assert ALL_TABLES == set(models.Base.metadata.tables)


def test_upgrade_empty_postgres_and_downgrade_foundation() -> None:
    with _isolated_postgres_schema() as database_url:
        engine = sa.create_engine(database_url)
        config = _alembic_config(database_url)
        try:
            assert sa.inspect(engine).get_table_names() == []

            # Exercise 0006 against actual pre-existing Source history instead
            # of validating only the empty-table path.
            command.upgrade(config, "20260808_0005")
            legacy_metadata = sa.MetaData()
            legacy_users = sa.Table(
                "users", legacy_metadata, autoload_with=engine
            )
            legacy_sources = sa.Table(
                "sources", legacy_metadata, autoload_with=engine
            )
            legacy_versions = sa.Table(
                "source_versions", legacy_metadata, autoload_with=engine
            )
            legacy_actor_id = uuid4()
            legacy_source_id = uuid4()
            legacy_version_id = uuid4()
            legacy_now = datetime.now(UTC)
            with engine.begin() as connection:
                connection.execute(
                    legacy_users.insert().values(
                        id=legacy_actor_id,
                        username="permission-backfill-auditor",
                        username_normalized="permission-backfill-auditor",
                        password_hash="not-a-real-password-hash",
                        password_changed_at=legacy_now,
                        created_at=legacy_now,
                        updated_at=legacy_now,
                    )
                )
                connection.execute(
                    legacy_sources.insert().values(
                        id=legacy_source_id,
                        canonical_key="doi:10.0000/permission-backfill",
                        submitted_by_user_id=legacy_actor_id,
                        created_at=legacy_now,
                        updated_at=legacy_now,
                        row_version=1,
                    )
                )
                connection.execute(
                    legacy_versions.insert().values(
                        id=legacy_version_id,
                        source_id=legacy_source_id,
                        version_number=1,
                        citation_id=uuid4(),
                        citation_metadata={"title": "Permission backfill"},
                        access_metadata={
                            "access_method": "legacy test access",
                            "requested_extraction_permission": "none",
                            "requested_storage_permission": "none",
                            "requested_scope_locators": [],
                        },
                        content_text="Legacy immutable source text.",
                        content_hash="0" * 64,
                        content_access="full_text",
                        source_type="journal_article",
                        knowledge_origin="scientific_evidence",
                        sensitivity="controlled",
                        submitted_by_user_id=legacy_actor_id,
                        created_at=legacy_now + timedelta(microseconds=1),
                    )
                )

            command.upgrade(config, "head")

            inspector = sa.inspect(engine)
            assert ALL_TABLES <= set(inspector.get_table_names())
            assert {fk["referred_table"] for fk in inspector.get_foreign_keys("sessions")} == {
                "users"
            }
            assert any(
                set(constraint["column_names"])
                == {"actor_user_id", "scope", "idempotency_key"}
                for constraint in inspector.get_unique_constraints("idempotency_records")
            )

            migrated_permissions = sa.Table(
                "source_permission_requests", sa.MetaData(), autoload_with=engine
            )
            with engine.connect() as connection:
                backfilled = connection.execute(
                    sa.select(migrated_permissions).where(
                        migrated_permissions.c.source_version_id
                        == legacy_version_id
                    )
                ).one()
                assert backfilled.sequence_number == 1
                assert backfilled.requested_extraction_permission == "none"
                assert backfilled.requested_storage_permission == "none"
                assert backfilled.requested_scope_locators == []
                assert backfilled.rights_basis == (
                    "legacy_access_metadata_snapshot_only_no_grant"
                )
                assert backfilled.rights_evidence == [
                    f"source-version:{legacy_version_id}",
                    f"content-sha256:{'0' * 64}",
                ]
                assert backfilled.domain_schema_version == (
                    "source-permission-request-legacy-0.1"
                )
                assert backfilled.request_checksum == sha256(
                    (
                        "magicforge:legacy-source-permission-request:"
                        f"{legacy_version_id}:{'0' * 64}"
                    ).encode("utf-8")
                ).hexdigest()

            # The exact deterministic backfill is the only permission history
            # state that may be safely removed by a downgrade.
            command.downgrade(config, "20260808_0005")
            downgraded_inspector = sa.inspect(engine)
            assert "source_permission_requests" not in set(
                downgraded_inspector.get_table_names()
            )
            assert "source_permission_request_id" not in {
                column["name"]
                for column in downgraded_inspector.get_columns(
                    "source_review_decisions"
                )
            }
            command.upgrade(config, "head")
            inspector = sa.inspect(engine)
            assert {fk["referred_table"] for fk in inspector.get_foreign_keys(
                "idempotency_records"
            )} == {"users"}
            assert {
                fk["referred_table"]
                for fk in inspector.get_foreign_keys("source_versions")
            } == {"sources", "source_versions", "users"}
            assert {
                fk["referred_table"]
                for fk in inspector.get_foreign_keys(
                    "source_permission_requests"
                )
            } == {
                "source_permission_requests",
                "source_versions",
                "users",
            }
            assert any(
                fk["referred_table"] == "source_permission_requests"
                and fk["constrained_columns"]
                == ["supersedes_request_id", "source_version_id"]
                and fk["referred_columns"] == ["id", "source_version_id"]
                for fk in inspector.get_foreign_keys(
                    "source_permission_requests"
                )
            )
            permission_checks = {
                constraint["name"]
                for constraint in inspector.get_check_constraints(
                    "source_permission_requests"
                )
            }
            assert {
                "ck_source_permission_request_scope_array",
                "ck_source_permission_request_rights_evidence_nonempty_array",
                "ck_source_permission_request_actor_roles_array",
            } <= permission_checks
            assert any(
                fk["referred_table"] == "source_permission_requests"
                and fk["constrained_columns"]
                == ["source_permission_request_id", "source_version_id"]
                and fk["referred_columns"] == ["id", "source_version_id"]
                for fk in inspector.get_foreign_keys(
                    "source_review_decisions"
                )
            )
            assert {
                fk["referred_table"]
                for fk in inspector.get_foreign_keys("claim_review_decisions")
            } == {"claim_candidates", "claim_review_decisions", "users"}
            assert any(
                set(constraint["column_names"])
                == {"source_id", "version_number"}
                for constraint in inspector.get_unique_constraints("source_versions")
            )
            assert any(
                set(constraint["column_names"])
                == {"claim_candidate_id", "sequence_number"}
                for constraint in inspector.get_unique_constraints(
                    "claim_review_decisions"
                )
            )

            roles = sa.Table("roles", sa.MetaData(), autoload_with=engine)
            with engine.connect() as connection:
                seeded_roles = {
                    name: str(role_id)
                    for role_id, name in connection.execute(
                        sa.select(roles.c.id, roles.c.name)
                    )
                }
                assert seeded_roles == SEEDED_ROLE_IDS

            from persistence.database import (
                DatabaseManager,
                DatabasePhase,
                EXPECTED_DATABASE_REVISION,
            )
            from persistence.models import Base

            with engine.connect() as connection:
                migration_context = MigrationContext.configure(
                    connection,
                    opts={
                        "compare_type": True,
                        "compare_server_default": True,
                    },
                )
                assert compare_metadata(migration_context, Base.metadata) == []

            manager = DatabaseManager(
                database_url,
                expected_revision=EXPECTED_DATABASE_REVISION,
            )
            manager.start()
            assert manager.phase == DatabasePhase.READY
            manager.close()

            metadata = sa.MetaData()
            users = sa.Table("users", metadata, autoload_with=engine)
            audit_events = sa.Table("audit_events", metadata, autoload_with=engine)
            sources = sa.Table("sources", metadata, autoload_with=engine)
            source_versions = sa.Table(
                "source_versions", metadata, autoload_with=engine
            )
            source_permission_requests = sa.Table(
                "source_permission_requests", metadata, autoload_with=engine
            )
            claim_candidates = sa.Table(
                "claim_candidates", metadata, autoload_with=engine
            )
            claim_review_decisions = sa.Table(
                "claim_review_decisions", metadata, autoload_with=engine
            )
            actor_id = uuid4()
            event_id = uuid4()
            source_id = uuid4()
            source_version_id = uuid4()
            source_permission_request_id = uuid4()
            claim_candidate_id = uuid4()
            now = datetime.now(UTC)

            with engine.begin() as connection:
                connection.execute(
                    users.insert().values(
                        id=actor_id,
                        username="migration-auditor",
                        username_normalized="migration-auditor",
                        password_hash="not-a-real-password-hash",
                        password_changed_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                connection.execute(
                    audit_events.insert().values(
                        event_id=event_id,
                        event_type="migration.audit.probe",
                        actor_user_id=actor_id,
                        actor_role_snapshot=["admin"],
                        object_type="migration_probe",
                        object_id=str(uuid4()),
                        reason="Verify the database append-only boundary.",
                        request_id=str(uuid4()),
                        correlation_id=str(uuid4()),
                        previous_state=None,
                        new_state={"status": "created"},
                        metadata={},
                        created_at=now + timedelta(microseconds=1),
                    )
                )
                connection.execute(
                    sources.insert().values(
                        id=source_id,
                        canonical_key="doi:10.0000/migration-probe",
                        submitted_by_user_id=actor_id,
                        created_at=now,
                        updated_at=now,
                        row_version=1,
                    )
                )
                connection.execute(
                    source_versions.insert().values(
                        id=source_version_id,
                        source_id=source_id,
                        version_number=1,
                        citation_id=uuid4(),
                        citation_metadata={"title": "Migration probe"},
                        access_metadata={"permission": "test-only"},
                        content_text="Immutable source text.",
                        content_hash="a" * 64,
                        content_access="full_text",
                        source_type="journal_article",
                        knowledge_origin="scientific_evidence",
                        sensitivity="public",
                        submitted_by_user_id=actor_id,
                        created_at=now + timedelta(microseconds=2),
                    )
                )
                connection.execute(
                    source_permission_requests.insert().values(
                        id=source_permission_request_id,
                        source_version_id=source_version_id,
                        sequence_number=1,
                        requested_extraction_permission="selected_sections",
                        requested_storage_permission="derived_knowledge_only",
                        requested_scope_locators=["results"],
                        rights_basis="Migration test licensed access.",
                        rights_evidence=["license:migration-probe"],
                        reason="Request bounded access for migration testing.",
                        submitted_by_user_id=actor_id,
                        actor_role_snapshot=["operator"],
                        request_checksum="1" * 64,
                        created_at=now + timedelta(microseconds=3),
                    )
                )
                connection.execute(
                    claim_candidates.insert().values(
                        id=claim_candidate_id,
                        source_version_id=source_version_id,
                        canonical_claim_id=uuid4(),
                        claim_text="An immutable submitted Claim.",
                        claim_role="result",
                        claim_polarity="supports",
                        locator={"source_locator": "migration-probe"},
                        extraction_provenance={"producer": "pipeline"},
                        model_run_metadata={},
                        proposed_evidence_card={"claim": "probe"},
                        candidate_schema_version="claim-candidate-0.1",
                        candidate_checksum="b" * 64,
                        extraction_confidence=0.8,
                        evidence_card_eligible=True,
                        status="submitted",
                        submitted_by_user_id=actor_id,
                        created_by_kind="pipeline",
                        created_at=now + timedelta(microseconds=3),
                        updated_at=now + timedelta(microseconds=3),
                        row_version=1,
                    )
                )

            with pytest.raises(DBAPIError, match="initial status"):
                with engine.begin() as connection:
                    connection.execute(
                        claim_candidates.insert().values(
                            id=uuid4(),
                            source_version_id=source_version_id,
                            canonical_claim_id=uuid4(),
                            claim_text="A forged initially approved Claim.",
                            claim_role="result",
                            claim_polarity="supports",
                            locator={"source_locator": "migration-probe"},
                            extraction_provenance={"producer": "pipeline"},
                            model_run_metadata={},
                            proposed_evidence_card={"claim": "forged"},
                            candidate_schema_version="claim-candidate-0.1",
                            candidate_checksum="c" * 64,
                            extraction_confidence=0.8,
                            evidence_card_eligible=True,
                            status="approved",
                            submitted_by_user_id=actor_id,
                            created_by_kind="pipeline",
                            created_at=now + timedelta(microseconds=4),
                            updated_at=now + timedelta(microseconds=4),
                            row_version=1,
                        )
                    )

            with pytest.raises(DBAPIError, match="append-only"):
                with engine.begin() as connection:
                    connection.execute(
                        audit_events.update()
                        .where(audit_events.c.event_id == event_id)
                        .values(reason="Mutation must be rejected.")
                    )

            with pytest.raises(DBAPIError, match="append-only"):
                with engine.begin() as connection:
                    connection.execute(
                        audit_events.delete().where(audit_events.c.event_id == event_id)
                    )

            with pytest.raises(DBAPIError, match="immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        source_versions.update()
                        .where(source_versions.c.id == source_version_id)
                        .values(content_text="Rewritten source text.")
                    )

            with pytest.raises(DBAPIError, match="immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        source_permission_requests.update()
                        .where(
                            source_permission_requests.c.id
                            == source_permission_request_id
                        )
                        .values(reason="Rewritten permission request.")
                    )

            with pytest.raises(DBAPIError, match="immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        source_permission_requests.delete().where(
                            source_permission_requests.c.id
                            == source_permission_request_id
                        )
                    )

            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        source_permission_requests.insert().values(
                            id=uuid4(),
                            source_version_id=source_version_id,
                            sequence_number=2,
                            requested_extraction_permission="none",
                            requested_storage_permission="none",
                            requested_scope_locators=[],
                            rights_basis="No asserted rights.",
                            rights_evidence=[],
                            reason="The empty evidence array must be rejected.",
                            submitted_by_user_id=actor_id,
                            actor_role_snapshot=["operator"],
                            request_checksum="3" * 64,
                            supersedes_request_id=(
                                source_permission_request_id
                            ),
                            created_at=now + timedelta(microseconds=4),
                        )
                    )

            with pytest.raises(DBAPIError, match="identity fields are immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        sources.update()
                        .where(sources.c.id == source_id)
                        .values(canonical_key="doi:10.0000/rewritten")
                    )

            with pytest.raises(DBAPIError, match="proposal fields are immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        claim_candidates.update()
                        .where(claim_candidates.c.id == claim_candidate_id)
                        .values(claim_text="A rewritten Claim.")
                    )

            with pytest.raises(DBAPIError, match="latest review decision"):
                with engine.begin() as connection:
                    connection.execute(
                        claim_candidates.update()
                        .where(claim_candidates.c.id == claim_candidate_id)
                        .values(status="rejected")
                    )

            rejected_decision_id = uuid4()
            revoked_decision_id = uuid4()
            with engine.begin() as connection:
                connection.execute(
                    claim_review_decisions.insert().values(
                        id=rejected_decision_id,
                        claim_candidate_id=claim_candidate_id,
                        sequence_number=1,
                        decision="rejected",
                        resulting_status="rejected",
                        reviewer_user_id=actor_id,
                        actor_role_snapshot=["reviewer"],
                        reason="Reject the migration probe Claim.",
                        confidence=None,
                        limitations=[],
                        evidence_class=None,
                        knowledge_origin=None,
                        evidence_level=None,
                        claim_eligibility="ineligible",
                        storage_permission="none",
                        contradiction_status="not_checked",
                        contradiction_check_status="not_checked",
                        contradicting_evidence_ids=[],
                        sensitive_information_level="public",
                        secret_exposure_level="none",
                        decision_checksum="d" * 64,
                        created_at=now + timedelta(microseconds=5),
                    )
                )
                connection.execute(
                    claim_candidates.update()
                    .where(claim_candidates.c.id == claim_candidate_id)
                    .values(status="rejected")
                )

            with engine.begin() as connection:
                connection.execute(
                    claim_review_decisions.insert().values(
                        id=revoked_decision_id,
                        claim_candidate_id=claim_candidate_id,
                        sequence_number=2,
                        decision="revoked",
                        resulting_status="revoked",
                        reviewer_user_id=actor_id,
                        actor_role_snapshot=["reviewer"],
                        reason="Revoke the migration probe Claim.",
                        confidence=None,
                        limitations=[],
                        evidence_class=None,
                        knowledge_origin=None,
                        evidence_level=None,
                        claim_eligibility="ineligible",
                        storage_permission="none",
                        contradiction_status="not_checked",
                        contradiction_check_status="not_checked",
                        contradicting_evidence_ids=[],
                        sensitive_information_level="public",
                        secret_exposure_level="none",
                        decision_checksum="e" * 64,
                        supersedes_decision_id=rejected_decision_id,
                        created_at=now + timedelta(microseconds=6),
                    )
                )
                connection.execute(
                    claim_candidates.update()
                    .where(claim_candidates.c.id == claim_candidate_id)
                    .values(status="revoked")
                )

            with pytest.raises(DBAPIError, match="latest review decision"):
                with engine.begin() as connection:
                    connection.execute(
                        claim_candidates.update()
                        .where(claim_candidates.c.id == claim_candidate_id)
                        .values(status="rejected")
                    )

            with pytest.raises(DBAPIError, match="immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        source_versions.delete().where(
                            source_versions.c.id == source_version_id
                        )
                    )

            with pytest.raises(
                DBAPIError,
                match="source permission history is not pure 0006 legacy backfill",
            ):
                command.downgrade(config, "20260808_0003")
        finally:
            engine.dispose()


def test_empty_postgres_can_downgrade_to_foundation() -> None:
    with _isolated_postgres_schema() as database_url:
        engine = sa.create_engine(database_url)
        config = _alembic_config(database_url)
        try:
            command.upgrade(config, "head")
            command.downgrade(config, "20260808_0003")

            remaining_after_storage = set(sa.inspect(engine).get_table_names())
            assert STORAGE_TABLES.isdisjoint(remaining_after_storage)
            assert EXTRACTION_TABLES.isdisjoint(remaining_after_storage)
            assert "source_permission_requests" not in remaining_after_storage
            assert (
                FOUNDATION_TABLES
                | (WORKFLOW_TABLES - {"source_permission_requests"})
                | MAPPING_TABLES
            ) <= remaining_after_storage

            command.downgrade(config, "base")
            assert set(sa.inspect(engine).get_table_names()).isdisjoint(
                ALL_TABLES
            )
        finally:
            engine.dispose()


def test_upgrade_refuses_to_rewrite_existing_source_review_decisions() -> None:
    with _isolated_postgres_schema() as database_url:
        engine = sa.create_engine(database_url)
        config = _alembic_config(database_url)
        try:
            command.upgrade(config, "20260808_0005")
            metadata = sa.MetaData()
            users = sa.Table("users", metadata, autoload_with=engine)
            sources = sa.Table("sources", metadata, autoload_with=engine)
            versions = sa.Table(
                "source_versions", metadata, autoload_with=engine
            )
            decisions = sa.Table(
                "source_review_decisions", metadata, autoload_with=engine
            )
            actor_id = uuid4()
            source_id = uuid4()
            version_id = uuid4()
            decision_id = uuid4()
            now = datetime.now(UTC)
            with engine.begin() as connection:
                connection.execute(
                    users.insert().values(
                        id=actor_id,
                        username="immutable-decision-migration-guard",
                        username_normalized=(
                            "immutable-decision-migration-guard"
                        ),
                        password_hash="not-a-real-password-hash",
                        password_changed_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                connection.execute(
                    sources.insert().values(
                        id=source_id,
                        canonical_key="doi:10.0000/immutable-decision-guard",
                        submitted_by_user_id=actor_id,
                        created_at=now,
                        updated_at=now,
                        row_version=1,
                    )
                )
                connection.execute(
                    versions.insert().values(
                        id=version_id,
                        source_id=source_id,
                        version_number=1,
                        citation_id=uuid4(),
                        citation_metadata={"title": "Immutable review guard"},
                        access_metadata={
                            "requested_extraction_permission": "none",
                            "requested_storage_permission": "none",
                            "requested_scope_locators": [],
                        },
                        content_text="Immutable review history.",
                        content_hash="4" * 64,
                        content_access="full_text",
                        source_type="journal_article",
                        knowledge_origin="scientific_evidence",
                        sensitivity="controlled",
                        submitted_by_user_id=actor_id,
                        created_at=now,
                    )
                )
                connection.execute(
                    decisions.insert().values(
                        id=decision_id,
                        source_version_id=version_id,
                        sequence_number=1,
                        decision="rejected",
                        resulting_status="rejected",
                        reviewer_user_id=actor_id,
                        actor_role_snapshot=["reviewer"],
                        reason="Preserve this immutable historical decision.",
                        citation_verification_evidence_id=None,
                        claim_eligibility="ineligible",
                        extraction_permission="none",
                        storage_permission="none",
                        sensitive_information_level="controlled",
                        contradicting_evidence_checked="not_checked",
                        extraction_scope={},
                        decision_checksum="5" * 64,
                        created_at=now + timedelta(microseconds=1),
                    )
                )

            with pytest.raises(
                DBAPIError,
                match="0006 requires empty source_review_decisions",
            ):
                command.upgrade(config, "head")

            assert "source_permission_requests" not in set(
                sa.inspect(engine).get_table_names()
            )
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "20260808_0005"
            with pytest.raises(DBAPIError, match="immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        decisions.update()
                        .where(decisions.c.id == decision_id)
                        .values(reason="Attempted migration rewrite.")
                    )
        finally:
            engine.dispose()
