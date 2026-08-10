"""Focused SQLite tests for the Checkpoint 2 authentication core."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from persistence import (
    AuditEvent,
    Base,
    DatabaseManager,
    Role,
    RoleName,
    SessionRecord,
    SessionStatus,
    SessionTransport,
    User,
    UserStatus,
)
from security.audit_read import AuditReadService
from security.crypto import IssuedTokens, PasswordHasher, hash_token
from security.services import (
    AdminService,
    AuthorizationDeniedError,
    AuthService,
    CsrfValidationError,
    InvalidCredentialsError,
    SecurityStateConflictError,
    UnauthenticatedError,
)


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


class FastPasswordHasher:
    MIN_LENGTH = 12
    MAX_LENGTH = 128

    def validate_new_password(self, password, *, identity_terms=()):
        if len(password) < self.MIN_LENGTH:
            raise ValueError("password too short")
        if password.casefold() in {value.casefold() for value in identity_terms if value}:
            raise ValueError("password equals identity")

    def hash(self, password):
        return "test$" + hashlib.sha256(password.encode()).hexdigest()

    def verify(self, password_hash, password):
        return hmac.compare_digest(password_hash, self.hash(password))

    def needs_rehash(self, password_hash):
        return False


class DeterministicTokenFactory:
    global_counter = 0

    def __init__(self) -> None:
        pass

    def issue(self, *, include_csrf: bool) -> IssuedTokens:
        type(self).global_counter += 1
        counter = type(self).global_counter
        return IssuedTokens(
            session_token=f"session-raw-token-{counter:04d}",
            csrf_token=f"csrf-raw-token-{counter:04d}" if include_csrf else None,
        )


@pytest.fixture
def security_database(tmp_path):
    manager = DatabaseManager(
        f"sqlite+pysqlite:///{tmp_path / 'security.sqlite'}"
    )
    manager.start()
    Base.metadata.create_all(manager.engine)
    with manager.session_factory() as session:
        session.add_all(
            [
                Role(name=role_name, description=role_name.value)
                for role_name in RoleName
            ]
        )
        session.commit()
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def services(security_database):
    password_hasher = FastPasswordHasher()
    tokens = DeterministicTokenFactory()
    admin = AdminService(
        security_database.session_factory,
        password_hasher=password_hasher,
        clock=lambda: NOW,
    )
    initial = admin.bootstrap_initial_admin(
        username="ArchiveAdmin",
        email="admin@example.test",
        password="correct horse battery staple",
    )
    auth = AuthService(
        security_database.session_factory,
        password_hasher=password_hasher,
        token_factory=tokens,
        clock=lambda: NOW,
    )
    return security_database, admin, auth, initial


def test_password_hasher_is_argon2id() -> None:
    hasher = PasswordHasher()
    password_hash = hasher.hash("a carefully chosen passphrase")
    assert password_hash.startswith("$argon2id$")
    assert hasher.verify(password_hash, "a carefully chosen passphrase")
    assert not hasher.verify(password_hash, "the wrong passphrase")


def test_login_stores_only_hashes_and_attributes_audit_to_actor(services) -> None:
    database, _, auth, initial = services
    credentials = auth.login(
        identifier="ADMIN@EXAMPLE.TEST",
        password="correct horse battery staple",
        transport=SessionTransport.COOKIE,
        client_metadata={"device_label": "test-browser"},
        request_id="request-login-1",
    )
    assert credentials.actor.user_id == initial.id
    assert credentials.actor.roles == frozenset({RoleName.ADMIN})
    assert credentials.csrf_token is not None
    assert credentials.session_token not in repr(credentials)
    assert credentials.csrf_token not in repr(credentials)
    assert credentials.session_token not in repr(credentials)
    assert credentials.csrf_token not in repr(credentials)

    with database.session_factory() as session:
        stored = session.scalar(select(SessionRecord))
        assert stored is not None
        assert stored.token_hash == hash_token(credentials.session_token)
        assert stored.csrf_token_hash == hash_token(credentials.csrf_token)
        assert credentials.session_token not in str(stored.client_metadata)
        events = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "session_created")
            )
        )
        assert len(events) == 1
        assert events[0].actor_user_id == initial.id
        assert credentials.session_token not in repr(events[0].new_state)

    authenticated = auth.authenticate(
        credentials.session_token, expected_transport=SessionTransport.COOKIE
    )
    assert authenticated.user_id == initial.id
    auth.verify_csrf(authenticated, credentials.csrf_token)
    with pytest.raises(CsrfValidationError):
        auth.verify_csrf(authenticated, "wrong-csrf-token")


def test_invalid_disabled_expired_and_revoked_sessions_fail_closed(services) -> None:
    database, admin, auth, initial = services
    with pytest.raises(InvalidCredentialsError):
        auth.login(
            identifier="unknown",
            password="correct horse battery staple",
            transport=SessionTransport.BEARER,
        )

    credentials = auth.login(
        identifier="ArchiveAdmin",
        password="correct horse battery staple",
        transport=SessionTransport.BEARER,
    )
    assert auth.logout(credentials.session_token)
    assert not auth.logout(credentials.session_token)
    with pytest.raises(UnauthenticatedError) as revoked:
        auth.authenticate(credentials.session_token)
    assert revoked.value.code == "session_revoked"

    expired_auth = AuthService(
        database.session_factory,
        password_hasher=FastPasswordHasher(),
        token_factory=DeterministicTokenFactory(),
        session_ttl=timedelta(minutes=1),
        clock=lambda: NOW,
    )
    expired = expired_auth.login(
        identifier="ArchiveAdmin",
        password="correct horse battery staple",
        transport=SessionTransport.BEARER,
    )
    later_auth = AuthService(
        database.session_factory,
        password_hasher=FastPasswordHasher(),
        token_factory=DeterministicTokenFactory(),
        clock=lambda: NOW + timedelta(minutes=2),
    )
    with pytest.raises(UnauthenticatedError) as expiry:
        later_auth.authenticate(expired.session_token)
    assert expiry.value.code == "session_expired"
    with database.session_factory() as session:
        record = session.get(SessionRecord, expired.actor.session_id)
        assert record is not None
        assert record.status == SessionStatus.EXPIRED

    active = auth.login(
        identifier="ArchiveAdmin",
        password="correct horse battery staple",
        transport=SessionTransport.BEARER,
    )
    with database.session_factory() as session:
        user = session.get(User, initial.id)
        assert user is not None
        user.status = UserStatus.DISABLED
        user.disabled_at = NOW
        session.commit()
    with pytest.raises(UnauthenticatedError) as disabled:
        auth.authenticate(active.session_token)
    assert disabled.value.code == "user_disabled"


def test_admin_service_rechecks_live_database_role_and_audits_mutations(services) -> None:
    database, admin, auth, _ = services
    credentials = auth.login(
        identifier="ArchiveAdmin",
        password="correct horse battery staple",
        transport=SessionTransport.BEARER,
    )
    reader = admin.create_user(
        credentials.actor,
        username="ReaderOne",
        email="reader@example.test",
        password="another strong passphrase",
        roles=[RoleName.READER],
        reason="Provision a read-only research account.",
        request_id="request-user-1",
    )
    role_change = admin.grant_role(
        credentials.actor,
        user_id=reader.id,
        role=RoleName.REVIEWER,
        reason="Permit governed source and claim review.",
    )
    assert role_change.changed
    assert role_change.user.roles == frozenset(
        {RoleName.READER, RoleName.REVIEWER}
    )

    reader_credentials = auth.login(
        identifier="ReaderOne",
        password="another strong passphrase",
        transport=SessionTransport.BEARER,
    )
    with pytest.raises(AuthorizationDeniedError):
        admin.create_user(
            reader_credentials.actor,
            username="ForbiddenUser",
            password="one more strong passphrase",
            roles=[RoleName.READER],
            reason="This operation must be rejected.",
        )

    revoked = admin.revoke_session(
        credentials.actor,
        session_id=reader_credentials.actor.session_id,
        reason="Terminate the user session during the security test.",
    )
    assert revoked.changed
    with pytest.raises(UnauthenticatedError):
        auth.authenticate(reader_credentials.session_token)

    with database.session_factory() as session:
        event_types = set(session.scalars(select(AuditEvent.event_type)))
        assert {"user_created", "role_granted", "session_revoked"} <= event_types

    assert auth.logout(credentials.session_token)
    with pytest.raises(AuthorizationDeniedError):
        admin.create_user(
            credentials.actor,
            username="StaleAdminActor",
            password="a sufficiently long password",
            roles=[RoleName.READER],
            reason="A revoked administrator session must not authorize this.",
        )


def test_audit_read_service_rechecks_live_role_after_authentication(services) -> None:
    database, admin, auth, _ = services
    administrator = auth.login(
        identifier="ArchiveAdmin",
        password="correct horse battery staple",
        transport=SessionTransport.BEARER,
    )
    delegated = admin.create_user(
        administrator.actor,
        username="DelegatedAuditAdmin",
        email="delegated-audit@example.test",
        password="a separate strong passphrase",
        roles=[RoleName.ADMIN, RoleName.READER],
        reason="Create a delegated audit administrator for live-RBAC coverage.",
    )
    delegated_session = auth.login(
        identifier="DelegatedAuditAdmin",
        password="a separate strong passphrase",
        transport=SessionTransport.BEARER,
    )
    audit = AuditReadService(database.session_factory, clock=lambda: NOW)

    assert audit.list_events(delegated_session.actor, limit=5).items

    admin.revoke_role(
        administrator.actor,
        user_id=delegated.id,
        role=RoleName.ADMIN,
        reason="Remove delegated audit access while its session remains active.",
    )
    with pytest.raises(AuthorizationDeniedError):
        audit.list_events(delegated_session.actor, limit=5)


def test_initial_admin_bootstrap_is_one_shot(security_database) -> None:
    service = AdminService(
        security_database.session_factory,
        password_hasher=FastPasswordHasher(),
        clock=lambda: NOW,
    )
    service.bootstrap_initial_admin(
        username="FirstAdmin",
        password="first explicit admin password",
    )
    with pytest.raises(SecurityStateConflictError) as conflict:
        service.bootstrap_initial_admin(
            username="SecondAdmin",
            password="second explicit admin password",
        )
    assert conflict.value.code == "initial_admin_already_configured"
