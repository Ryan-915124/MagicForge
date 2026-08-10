"""Transactional authentication and administrator application services."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from persistence import (
    AuditEventRepository,
    RoleName,
    SessionFactory,
    SessionRecord,
    SessionStatus,
    SessionTransport,
    SqlAlchemyUnitOfWork,
    User,
    UserStatus,
)
from security.crypto import PasswordHasher, TokenFactory, hash_token, token_hashes_equal
from security.policy import AuthenticatedActor, Permission, has_permission
from security.repositories import (
    RoleRepository,
    SessionRepository,
    UserRepository,
    normalize_identifier,
)


class SecurityServiceError(RuntimeError):
    """Base error with a stable machine code and safe public text."""

    status_code = 400

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


class InvalidCredentialsError(SecurityServiceError):
    status_code = 401

    def __init__(self) -> None:
        super().__init__("invalid_credentials", "The credentials are invalid.")


class UnauthenticatedError(SecurityServiceError):
    status_code = 401

    def __init__(self, code: str = "unauthenticated") -> None:
        super().__init__(code, "Authentication is required.")


class CsrfValidationError(SecurityServiceError):
    status_code = 403

    def __init__(self) -> None:
        super().__init__("csrf_validation_failed", "CSRF validation failed.")


class AuthorizationDeniedError(SecurityServiceError):
    status_code = 403

    def __init__(self, code: str = "forbidden") -> None:
        super().__init__(code, "The authenticated actor is not authorized.")


class SecurityResourceNotFoundError(SecurityServiceError):
    status_code = 404

    def __init__(self, resource: str) -> None:
        super().__init__(f"{resource}_not_found", "The requested resource was not found.")


class SecurityStateConflictError(SecurityServiceError):
    status_code = 409

    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(code, public_message)


class SecurityInputError(SecurityServiceError):
    status_code = 422

    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(code, public_message)


@dataclass(frozen=True, slots=True)
class SessionCredentials:
    """One-time raw credentials returned only to the login boundary."""

    actor: AuthenticatedActor
    session_token: str = field(repr=False)
    csrf_token: str | None = field(default=None, repr=False)

    @property
    def expires_at(self) -> datetime:
        assert self.actor.session_expires_at is not None
        return self.actor.session_expires_at

    @property
    def transport(self) -> SessionTransport:
        assert self.actor.session_transport is not None
        return self.actor.session_transport


@dataclass(frozen=True, slots=True)
class UserSummary:
    id: UUID
    username: str
    email: str | None
    status: UserStatus
    roles: frozenset[RoleName]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RoleChangeResult:
    user: UserSummary
    role: RoleName
    changed: bool


@dataclass(frozen=True, slots=True)
class SessionRevocationResult:
    session_id: UUID
    changed: bool


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _required_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        raise SecurityInputError("reason_required", "A reason is required.")
    if len(normalized) > 2_000:
        raise SecurityInputError("reason_too_long", "The reason is too long.")
    return normalized


def _request_context(
    *, request_id: str | None, correlation_id: str | None
) -> tuple[str | None, str]:
    normalized_request = request_id.strip() if request_id else None
    normalized_correlation = (
        correlation_id.strip() if correlation_id else normalized_request or str(uuid4())
    )
    if not normalized_correlation:
        normalized_correlation = str(uuid4())
    return normalized_request, normalized_correlation


def _normalize_roles(values: Iterable[RoleName | str]) -> frozenset[RoleName]:
    try:
        roles = frozenset(
            value if isinstance(value, RoleName) else RoleName(value.strip().casefold())
            for value in values
        )
    except (AttributeError, ValueError) as exc:
        raise SecurityInputError("invalid_role", "One or more roles are invalid.") from exc
    if not roles:
        raise SecurityInputError("role_required", "At least one role is required.")
    return roles


def _safe_client_metadata(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    if not metadata:
        return {}
    allowed = {"device_label", "remote_address", "user_agent"}
    if any(key not in allowed for key in metadata):
        raise SecurityInputError(
            "invalid_client_metadata", "Client metadata contains an unsupported field."
        )
    normalized: dict[str, str] = {}
    for key, value in metadata.items():
        if not isinstance(value, str):
            raise SecurityInputError(
                "invalid_client_metadata", "Client metadata values must be text."
            )
        normalized[key] = value.strip()[:500]
    return normalized


class AuthService:
    """Authenticate users and manage opaque, revocable sessions."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        password_hasher: PasswordHasher | None = None,
        token_factory: TokenFactory | None = None,
        session_ttl: timedelta = timedelta(hours=12),
        last_used_write_interval: timedelta = timedelta(minutes=5),
        clock: Clock = utc_now,
    ) -> None:
        if session_ttl <= timedelta(0) or session_ttl > timedelta(days=30):
            raise ValueError("session_ttl must be between zero and 30 days")
        if last_used_write_interval < timedelta(0):
            raise ValueError("last_used_write_interval must not be negative")
        self._session_factory = session_factory
        self._passwords = password_hasher or PasswordHasher()
        self._tokens = token_factory or TokenFactory()
        self._session_ttl = session_ttl
        self._last_used_write_interval = last_used_write_interval
        self._clock = clock
        # Equalize the expensive verification path when an identifier is absent.
        self._dummy_password_hash = self._passwords.hash(
            "MagicForge authentication timing sentinel"
        )

    def login(
        self,
        *,
        identifier: str,
        password: str,
        transport: SessionTransport,
        client_metadata: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SessionCredentials:
        try:
            normalized_identifier = normalize_identifier(identifier)
        except ValueError:
            raise InvalidCredentialsError() from None
        if not isinstance(password, str) or len(password) > self._passwords.MAX_LENGTH:
            raise InvalidCredentialsError()
        if not isinstance(transport, SessionTransport):
            try:
                transport = SessionTransport(transport)
            except (TypeError, ValueError):
                raise SecurityInputError(
                    "invalid_session_transport", "The session transport is invalid."
                ) from None
        metadata = _safe_client_metadata(client_metadata)
        request_id, correlation_id = _request_context(
            request_id=request_id, correlation_id=correlation_id
        )
        now = _aware_utc(self._clock())

        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            users = UserRepository(unit.session)
            roles = RoleRepository(unit.session)
            user = users.get_by_identifier(normalized_identifier, for_update=True)
            stored_hash = user.password_hash if user is not None else self._dummy_password_hash
            password_valid = self._passwords.verify(stored_hash, password)
            if (
                user is None
                or not password_valid
                or user.status != UserStatus.ACTIVE
            ):
                raise InvalidCredentialsError()

            role_names = roles.active_names_for_user(user.id)
            if not role_names:
                raise AuthorizationDeniedError("user_has_no_active_role")

            if self._passwords.needs_rehash(user.password_hash):
                user.password_hash = self._passwords.hash(password)
                AuditEventRepository(unit.session).append(
                    event_type="credential_hash_upgraded",
                    actor_user_id=user.id,
                    actor_role_snapshot=role_names,
                    object_type="user",
                    object_id=str(user.id),
                    new_state={"hash_scheme": "argon2id"},
                    reason="Upgrade the stored credential hash parameters after login.",
                    request_id=request_id,
                    correlation_id=correlation_id,
                )

            issued = self._tokens.issue(include_csrf=transport == SessionTransport.COOKIE)
            session_record = SessionRecord(
                user_id=user.id,
                token_hash=hash_token(issued.session_token),
                csrf_token_hash=(
                    hash_token(issued.csrf_token) if issued.csrf_token else None
                ),
                transport=transport,
                status=SessionStatus.ACTIVE,
                created_at=now,
                expires_at=now + self._session_ttl,
                last_used_at=now,
                client_metadata=metadata,
            )
            SessionRepository(unit.session).add(session_record)
            unit.flush()
            AuditEventRepository(unit.session).append(
                event_type="session_created",
                actor_user_id=user.id,
                actor_role_snapshot=role_names,
                object_type="session",
                object_id=str(session_record.id),
                new_state={
                    "status": SessionStatus.ACTIVE.value,
                    "transport": transport.value,
                    "expires_at": session_record.expires_at.isoformat(),
                },
                reason="Create an authenticated session.",
                request_id=request_id,
                correlation_id=correlation_id,
            )
            actor = self._actor(user, role_names, session_record)
            unit.commit()
        return SessionCredentials(
            actor=actor,
            session_token=issued.session_token,
            csrf_token=issued.csrf_token,
        )

    def authenticate(
        self,
        token: str,
        *,
        expected_transport: SessionTransport | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AuthenticatedActor:
        try:
            digest = hash_token(token)
        except ValueError:
            raise UnauthenticatedError() from None
        now = _aware_utc(self._clock())
        request_id, correlation_id = _request_context(
            request_id=request_id, correlation_id=correlation_id
        )
        terminal_error: UnauthenticatedError | None = None

        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            sessions = SessionRepository(unit.session)
            record = sessions.get_by_token_hash(digest, for_update=True)
            if record is None or not token_hashes_equal(record.token_hash, digest):
                raise UnauthenticatedError()
            if expected_transport is not None and record.transport != expected_transport:
                raise UnauthenticatedError("session_transport_mismatch")

            user = UserRepository(unit.session).get(record.user_id, for_update=True)
            if user is None:
                raise UnauthenticatedError()
            role_names = RoleRepository(unit.session).active_names_for_user(user.id)

            if record.status == SessionStatus.REVOKED:
                raise UnauthenticatedError("session_revoked")
            if record.status == SessionStatus.EXPIRED:
                raise UnauthenticatedError("session_expired")
            if _aware_utc(record.expires_at) <= now:
                record.status = SessionStatus.EXPIRED
                if role_names:
                    AuditEventRepository(unit.session).append(
                        event_type="session_expired",
                        actor_user_id=user.id,
                        actor_role_snapshot=role_names,
                        object_type="session",
                        object_id=str(record.id),
                        previous_state={"status": SessionStatus.ACTIVE.value},
                        new_state={"status": SessionStatus.EXPIRED.value},
                        reason="The absolute session lifetime elapsed.",
                        request_id=request_id,
                        correlation_id=correlation_id,
                    )
                unit.commit()
                terminal_error = UnauthenticatedError("session_expired")
            elif user.status != UserStatus.ACTIVE:
                SessionRepository.revoke(
                    record,
                    revoked_at=now,
                    revoked_by_user_id=user.id,
                    reason="user_disabled",
                )
                if role_names:
                    AuditEventRepository(unit.session).append(
                        event_type="session_revoked",
                        actor_user_id=user.id,
                        actor_role_snapshot=role_names,
                        object_type="session",
                        object_id=str(record.id),
                        previous_state={"status": SessionStatus.ACTIVE.value},
                        new_state={"status": SessionStatus.REVOKED.value},
                        reason="Revoke the session because its user is disabled.",
                        request_id=request_id,
                        correlation_id=correlation_id,
                    )
                unit.commit()
                terminal_error = UnauthenticatedError("user_disabled")
            elif _aware_utc(user.password_changed_at) > _aware_utc(record.created_at):
                SessionRepository.revoke(
                    record,
                    revoked_at=now,
                    revoked_by_user_id=user.id,
                    reason="credential_changed",
                )
                if role_names:
                    AuditEventRepository(unit.session).append(
                        event_type="session_revoked",
                        actor_user_id=user.id,
                        actor_role_snapshot=role_names,
                        object_type="session",
                        object_id=str(record.id),
                        previous_state={"status": SessionStatus.ACTIVE.value},
                        new_state={"status": SessionStatus.REVOKED.value},
                        reason="Revoke the session after a credential change.",
                        request_id=request_id,
                        correlation_id=correlation_id,
                    )
                unit.commit()
                terminal_error = UnauthenticatedError("credential_changed")
            elif not role_names:
                raise AuthorizationDeniedError("user_has_no_active_role")
            else:
                last_used = (
                    _aware_utc(record.last_used_at) if record.last_used_at else None
                )
                if (
                    last_used is None
                    or now - last_used >= self._last_used_write_interval
                ):
                    record.last_used_at = now
                    unit.commit()
                actor = self._actor(user, role_names, record)

        if terminal_error is not None:
            raise terminal_error
        return actor

    def verify_csrf(
        self, actor: AuthenticatedActor, csrf_token: str
    ) -> None:
        if actor.session_transport != SessionTransport.COOKIE:
            raise CsrfValidationError()
        try:
            provided_hash = hash_token(csrf_token)
        except ValueError:
            raise CsrfValidationError() from None
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            record = SessionRepository(unit.session).get(actor.session_id)
            user = (
                UserRepository(unit.session).get(actor.user_id)
                if actor.user_id is not None
                else None
            )
            if (
                record is None
                or record.user_id != actor.user_id
                or record.transport != SessionTransport.COOKIE
                or record.status != SessionStatus.ACTIVE
                or _aware_utc(record.expires_at) <= _aware_utc(self._clock())
                or user is None
                or user.status != UserStatus.ACTIVE
                or record.csrf_token_hash is None
                or not token_hashes_equal(record.csrf_token_hash, provided_hash)
            ):
                raise CsrfValidationError()

    def logout(
        self,
        token: str,
        *,
        reason: str = "User requested logout.",
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        try:
            digest = hash_token(token)
        except ValueError:
            raise UnauthenticatedError() from None
        reason = _required_reason(reason)
        request_id, correlation_id = _request_context(
            request_id=request_id, correlation_id=correlation_id
        )
        now = _aware_utc(self._clock())
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            sessions = SessionRepository(unit.session)
            record = sessions.get_by_token_hash(digest, for_update=True)
            if record is None or not token_hashes_equal(record.token_hash, digest):
                raise UnauthenticatedError()
            if record.status == SessionStatus.REVOKED:
                return False
            user = UserRepository(unit.session).get(record.user_id)
            if user is None:
                raise UnauthenticatedError()
            role_names = RoleRepository(unit.session).active_names_for_user(user.id)
            if not role_names:
                raise AuthorizationDeniedError("user_has_no_active_role")
            previous = record.status.value
            sessions.revoke(
                record,
                revoked_at=now,
                revoked_by_user_id=user.id,
                reason="logout",
            )
            AuditEventRepository(unit.session).append(
                event_type="session_revoked",
                actor_user_id=user.id,
                actor_role_snapshot=role_names,
                object_type="session",
                object_id=str(record.id),
                previous_state={"status": previous},
                new_state={"status": SessionStatus.REVOKED.value},
                reason=reason,
                request_id=request_id,
                correlation_id=correlation_id,
            )
            unit.commit()
        return True

    @staticmethod
    def _actor(
        user: User,
        roles: frozenset[RoleName],
        record: SessionRecord,
    ) -> AuthenticatedActor:
        return AuthenticatedActor(
            user_id=user.id,
            username=user.username,
            roles=roles,
            session_id=record.id,
            session_transport=record.transport,
            session_expires_at=_aware_utc(record.expires_at),
        )


class AdminService:
    """Administrative mutations with an independent DB-backed RBAC check."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        password_hasher: PasswordHasher | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._passwords = password_hasher or PasswordHasher()
        self._clock = clock

    def bootstrap_initial_admin(
        self,
        *,
        username: str,
        password: str,
        email: str | None = None,
        reason: str = "Explicitly bootstrap the initial administrator.",
    ) -> UserSummary:
        reason = _required_reason(reason)
        username, username_normalized, email, email_normalized = self._identity(
            username, email
        )
        self._passwords.validate_new_password(
            password, identity_terms=(username, email or "")
        )
        now = _aware_utc(self._clock())
        try:
            with SqlAlchemyUnitOfWork(self._session_factory) as unit:
                if unit.session.bind is not None and unit.session.bind.dialect.name == "postgresql":
                    unit.session.execute(
                        text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
                    )
                users = UserRepository(unit.session)
                if users.count() != 0:
                    raise SecurityStateConflictError(
                        "initial_admin_already_configured",
                        "Initial administrator bootstrap is no longer available.",
                    )
                admin_role = RoleRepository(unit.session).get(RoleName.ADMIN)
                if admin_role is None:
                    raise SecurityStateConflictError(
                        "role_catalog_not_ready", "The role catalog is not ready."
                    )
                user = User(
                    username=username,
                    username_normalized=username_normalized,
                    email=email,
                    email_normalized=email_normalized,
                    password_hash=self._passwords.hash(password),
                    status=UserStatus.ACTIVE,
                    password_changed_at=now,
                    created_at=now,
                    updated_at=now,
                )
                users.add(user)
                unit.flush()
                RoleRepository(unit.session).grant(
                    user_id=user.id,
                    role=admin_role,
                    granted_by_user_id=user.id,
                    granted_at=now,
                )
                AuditEventRepository(unit.session).append(
                    event_type="initial_admin_created",
                    actor_user_id=user.id,
                    actor_role_snapshot=[RoleName.ADMIN],
                    object_type="user",
                    object_id=str(user.id),
                    new_state={
                        "username": user.username,
                        "email": user.email,
                        "status": user.status.value,
                        "roles": [RoleName.ADMIN.value],
                    },
                    reason=reason,
                    metadata={"channel": "admin_cli"},
                )
                unit.commit()
                return self._summary(user, frozenset({RoleName.ADMIN}))
        except IntegrityError as exc:
            raise SecurityStateConflictError(
                "identity_already_exists", "The user identity already exists."
            ) from exc

    def create_user(
        self,
        actor: AuthenticatedActor,
        *,
        username: str,
        password: str,
        roles: Iterable[RoleName | str],
        email: str | None = None,
        reason: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> UserSummary:
        reason = _required_reason(reason)
        requested_roles = _normalize_roles(roles)
        username, username_normalized, email, email_normalized = self._identity(
            username, email
        )
        self._passwords.validate_new_password(
            password, identity_terms=(username, email or "")
        )
        request_id, correlation_id = _request_context(
            request_id=request_id, correlation_id=correlation_id
        )
        now = _aware_utc(self._clock())
        try:
            with SqlAlchemyUnitOfWork(self._session_factory) as unit:
                actor_roles = self._require_permission(
                    unit, actor, Permission.USER_ADMIN
                )
                users = UserRepository(unit.session)
                user = User(
                    username=username,
                    username_normalized=username_normalized,
                    email=email,
                    email_normalized=email_normalized,
                    password_hash=self._passwords.hash(password),
                    status=UserStatus.ACTIVE,
                    password_changed_at=now,
                    created_at=now,
                    updated_at=now,
                )
                users.add(user)
                unit.flush()
                role_repository = RoleRepository(unit.session)
                for role_name in sorted(requested_roles, key=lambda value: value.value):
                    role = role_repository.get(role_name)
                    if role is None:
                        raise SecurityStateConflictError(
                            "role_catalog_not_ready", "The role catalog is not ready."
                        )
                    role_repository.grant(
                        user_id=user.id,
                        role=role,
                        granted_by_user_id=actor.user_id,
                        granted_at=now,
                    )
                AuditEventRepository(unit.session).append(
                    event_type="user_created",
                    actor_user_id=actor.user_id,
                    actor_role_snapshot=actor_roles,
                    object_type="user",
                    object_id=str(user.id),
                    new_state={
                        "username": user.username,
                        "email": user.email,
                        "status": user.status.value,
                        "roles": sorted(role.value for role in requested_roles),
                    },
                    reason=reason,
                    request_id=request_id,
                    correlation_id=correlation_id,
                )
                unit.commit()
                return self._summary(user, requested_roles)
        except IntegrityError as exc:
            raise SecurityStateConflictError(
                "identity_already_exists", "The user identity already exists."
            ) from exc

    def grant_role(
        self,
        actor: AuthenticatedActor,
        *,
        user_id: UUID,
        role: RoleName | str,
        reason: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> RoleChangeResult:
        return self._change_role(
            actor,
            user_id=user_id,
            role=role,
            grant=True,
            reason=reason,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def revoke_role(
        self,
        actor: AuthenticatedActor,
        *,
        user_id: UUID,
        role: RoleName | str,
        reason: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> RoleChangeResult:
        return self._change_role(
            actor,
            user_id=user_id,
            role=role,
            grant=False,
            reason=reason,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def _change_role(
        self,
        actor: AuthenticatedActor,
        *,
        user_id: UUID,
        role: RoleName | str,
        grant: bool,
        reason: str,
        request_id: str | None,
        correlation_id: str | None,
    ) -> RoleChangeResult:
        reason = _required_reason(reason)
        try:
            role_name = role if isinstance(role, RoleName) else RoleName(role.strip().casefold())
        except (AttributeError, ValueError):
            raise SecurityInputError("invalid_role", "The role is invalid.") from None
        if not grant and user_id == actor.user_id and role_name == RoleName.ADMIN:
            raise SecurityStateConflictError(
                "self_admin_revocation_forbidden",
                "An administrator cannot revoke their own administrator role.",
            )
        request_id, correlation_id = _request_context(
            request_id=request_id, correlation_id=correlation_id
        )
        now = _aware_utc(self._clock())
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            actor_roles = self._require_permission(unit, actor, Permission.ROLE_ADMIN)
            users = UserRepository(unit.session)
            target = users.get(user_id, for_update=True)
            if target is None:
                raise SecurityResourceNotFoundError("user")
            roles = RoleRepository(unit.session)
            stored_role = roles.get(role_name)
            if stored_role is None:
                raise SecurityStateConflictError(
                    "role_catalog_not_ready", "The role catalog is not ready."
                )
            before = roles.active_names_for_user(target.id)
            if not grant and before == frozenset({role_name}):
                raise SecurityStateConflictError(
                    "role_required", "A user must retain at least one active role."
                )
            if grant:
                _, changed = roles.grant(
                    user_id=target.id,
                    role=stored_role,
                    granted_by_user_id=actor.user_id,
                    granted_at=now,
                )
            else:
                _, changed = roles.revoke(
                    user_id=target.id,
                    role=stored_role,
                    revoked_by_user_id=actor.user_id,
                    revoked_at=now,
                )
            after = (
                frozenset(set(before) | {role_name})
                if grant and changed
                else frozenset(set(before) - {role_name})
                if not grant and changed
                else before
            )
            if changed:
                AuditEventRepository(unit.session).append(
                    event_type="role_granted" if grant else "role_revoked",
                    actor_user_id=actor.user_id,
                    actor_role_snapshot=actor_roles,
                    object_type="user",
                    object_id=str(target.id),
                    previous_state={"roles": sorted(value.value for value in before)},
                    new_state={"roles": sorted(value.value for value in after)},
                    reason=reason,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    metadata={"changed_role": role_name.value},
                )
                unit.commit()
            return RoleChangeResult(
                user=self._summary(target, after), role=role_name, changed=changed
            )

    def revoke_session(
        self,
        actor: AuthenticatedActor,
        *,
        session_id: UUID,
        reason: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SessionRevocationResult:
        reason = _required_reason(reason)
        request_id, correlation_id = _request_context(
            request_id=request_id, correlation_id=correlation_id
        )
        now = _aware_utc(self._clock())
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            actor_roles = self._require_permission(
                unit, actor, Permission.SESSION_REVOKE
            )
            sessions = SessionRepository(unit.session)
            target = sessions.get(session_id, for_update=True)
            if target is None:
                raise SecurityResourceNotFoundError("session")
            previous = target.status.value
            changed = sessions.revoke(
                target,
                revoked_at=now,
                revoked_by_user_id=actor.user_id,
                reason="admin_revocation",
            )
            if changed:
                AuditEventRepository(unit.session).append(
                    event_type="session_revoked",
                    actor_user_id=actor.user_id,
                    actor_role_snapshot=actor_roles,
                    object_type="session",
                    object_id=str(target.id),
                    previous_state={"status": previous},
                    new_state={"status": SessionStatus.REVOKED.value},
                    reason=reason,
                    request_id=request_id,
                    correlation_id=correlation_id,
                )
                unit.commit()
            return SessionRevocationResult(session_id=target.id, changed=changed)

    def _require_permission(
        self,
        unit: SqlAlchemyUnitOfWork,
        actor: AuthenticatedActor,
        permission: Permission,
    ) -> frozenset[RoleName]:
        if (
            actor.user_id is None
            or actor.session_id is None
            or actor.is_bootstrap_anonymous
        ):
            raise AuthorizationDeniedError()
        session_record = SessionRepository(unit.session).get(
            actor.session_id, for_update=True
        )
        now = _aware_utc(self._clock())
        if (
            session_record is None
            or session_record.user_id != actor.user_id
            or session_record.status != SessionStatus.ACTIVE
            or _aware_utc(session_record.expires_at) <= now
            or session_record.transport != actor.session_transport
        ):
            raise AuthorizationDeniedError()
        user = UserRepository(unit.session).get(actor.user_id, for_update=True)
        if user is None or user.status != UserStatus.ACTIVE:
            raise AuthorizationDeniedError()
        roles = RoleRepository(unit.session).active_names_for_user(actor.user_id)
        current_actor = AuthenticatedActor(
            user_id=user.id,
            username=user.username,
            roles=roles,
            session_id=session_record.id,
            session_transport=session_record.transport,
            session_expires_at=_aware_utc(session_record.expires_at),
            break_glass=actor.break_glass,
        )
        if not has_permission(current_actor, permission):
            raise AuthorizationDeniedError(f"{permission.value}_required")
        return roles

    @staticmethod
    def _identity(
        username: str, email: str | None
    ) -> tuple[str, str, str | None, str | None]:
        if not isinstance(username, str):
            raise SecurityInputError("invalid_username", "The username is invalid.")
        display_username = username.strip()
        if not display_username or len(display_username) > 80:
            raise SecurityInputError("invalid_username", "The username is invalid.")
        username_normalized = normalize_identifier(display_username)
        display_email = email.strip() if isinstance(email, str) else None
        if email is not None and (
            not display_email or len(display_email) > 320 or "@" not in display_email
        ):
            raise SecurityInputError("invalid_email", "The email address is invalid.")
        email_normalized = (
            normalize_identifier(display_email) if display_email is not None else None
        )
        return display_username, username_normalized, display_email, email_normalized

    @staticmethod
    def _summary(user: User, roles: frozenset[RoleName]) -> UserSummary:
        return UserSummary(
            id=user.id,
            username=user.username,
            email=user.email,
            status=user.status,
            roles=roles,
            created_at=_aware_utc(user.created_at),
        )


__all__ = [
    "AdminService",
    "AuthorizationDeniedError",
    "AuthService",
    "CsrfValidationError",
    "InvalidCredentialsError",
    "RoleChangeResult",
    "SecurityInputError",
    "SecurityResourceNotFoundError",
    "SecurityServiceError",
    "SecurityStateConflictError",
    "SessionCredentials",
    "SessionRevocationResult",
    "UnauthenticatedError",
    "UserSummary",
]
