"""Actor, role, and retrieval-authorization policy for MagicForge.

This module is deliberately independent from FastAPI.  Authentication adapters
construct an :class:`AuthenticatedActor`; application services consume the
actor's permissions or a server-derived :class:`RetrievalAuthorization`.
Neither roles nor security filters are accepted from client payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from persistence.models import RoleName, SessionTransport
from retrieval.interfaces import RetrievalAuthorization
from knowledge.governance import ClearanceRank, SensitiveInformationLevel


BOOTSTRAP_ANONYMOUS_PRINCIPAL = "bootstrap-anonymous-readonly"


class Permission(StrEnum):
    """Application capabilities granted by an additive role matrix."""

    PRODUCT_READ = "product_read"
    RESEARCH_CONSOLE = "research_console"
    SOURCE_REGISTER = "source_register"
    EXTRACTION_RUN = "extraction_run"
    CLAIM_SUBMIT = "claim_submit"
    MAPPING_SUBMIT = "mapping_submit"
    SOURCE_REVIEW = "source_review"
    CLAIM_REVIEW = "claim_review"
    MAPPING_REVIEW = "mapping_review"
    MANIFEST_BUILD = "manifest_build"
    MANIFEST_AUTHORIZE = "manifest_authorize"
    MANIFEST_INGEST = "manifest_ingest"
    CORPUS_ACTIVATE = "corpus_activate"
    USER_ADMIN = "user_admin"
    ROLE_ADMIN = "role_admin"
    SESSION_REVOKE = "session_revoke"
    AUDIT_READ = "audit_read"
    BREAK_GLASS = "break_glass"


ROLE_PERMISSIONS = MappingProxyType(
    {
        RoleName.READER: frozenset({Permission.PRODUCT_READ}),
        RoleName.REVIEWER: frozenset(
            {
                Permission.PRODUCT_READ,
                Permission.SOURCE_REVIEW,
                Permission.CLAIM_REVIEW,
                Permission.MAPPING_REVIEW,
            }
        ),
        RoleName.OPERATOR: frozenset(
            {
                Permission.PRODUCT_READ,
                Permission.RESEARCH_CONSOLE,
                Permission.SOURCE_REGISTER,
                Permission.EXTRACTION_RUN,
                Permission.CLAIM_SUBMIT,
                Permission.MAPPING_SUBMIT,
                Permission.MANIFEST_BUILD,
                Permission.MANIFEST_AUTHORIZE,
                Permission.MANIFEST_INGEST,
                Permission.CORPUS_ACTIVATE,
            }
        ),
        # Admin is intentionally not an implicit reviewer or operator.  Any
        # exceptional content access must use the explicit break-glass path.
        RoleName.ADMIN: frozenset(
            {
                Permission.PRODUCT_READ,
                Permission.RESEARCH_CONSOLE,
                Permission.USER_ADMIN,
                Permission.ROLE_ADMIN,
                Permission.SESSION_REVOKE,
                Permission.AUDIT_READ,
                Permission.BREAK_GLASS,
            }
        ),
    }
)


class AuthorizationError(PermissionError):
    """Base class for safe, stable authorization failures."""


class AuthenticationRequiredError(AuthorizationError):
    pass


class AccessDeniedError(AuthorizationError):
    pass


class BreakGlassPolicyError(AuthorizationError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedActor:
    """Immutable server-side principal resolved from a live session.

    ``user_id=None`` is valid only for the dedicated Bootstrap anonymous actor.
    Normal callers must never construct that principal from request data.
    """

    user_id: UUID | None
    username: str
    roles: frozenset[RoleName]
    session_id: UUID | None = None
    session_transport: SessionTransport | None = None
    session_expires_at: datetime | None = None
    is_bootstrap_anonymous: bool = False
    break_glass: bool = False

    def __post_init__(self) -> None:
        username = self.username.strip()
        if not username:
            raise ValueError("actor username must not be empty")
        object.__setattr__(self, "username", username)
        try:
            roles = frozenset(RoleName(value) for value in self.roles)
        except (TypeError, ValueError) as exc:
            raise ValueError("actor contains an unsupported role") from exc
        object.__setattr__(self, "roles", roles)

        if self.is_bootstrap_anonymous:
            if (
                self.user_id is not None
                or self.session_id is not None
                or self.session_transport is not None
                or self.session_expires_at is not None
                or roles
                or self.break_glass
                or username != BOOTSTRAP_ANONYMOUS_PRINCIPAL
            ):
                raise ValueError("Bootstrap anonymous actor must remain strictly limited")
            return

        if self.user_id is None:
            raise ValueError("authenticated actor requires a user id")
        if self.session_id is not None and self.session_transport is None:
            raise ValueError("session-backed actor requires a session transport")
        if self.break_glass and RoleName.ADMIN not in roles:
            raise ValueError("only an administrator may hold break-glass state")

    @property
    def principal_id(self) -> str:
        return (
            BOOTSTRAP_ANONYMOUS_PRINCIPAL
            if self.is_bootstrap_anonymous
            else str(self.user_id)
        )

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(sorted(role.value for role in self.roles))


def bootstrap_anonymous_actor() -> AuthenticatedActor:
    """Create the explicit, Bootstrap-only anonymous read principal."""

    return AuthenticatedActor(
        user_id=None,
        username=BOOTSTRAP_ANONYMOUS_PRINCIPAL,
        roles=frozenset(),
        is_bootstrap_anonymous=True,
    )


def permissions_for_actor(
    actor: AuthenticatedActor | None,
) -> frozenset[Permission]:
    if actor is None:
        return frozenset()
    if actor.is_bootstrap_anonymous:
        # The legacy Bootstrap frontend has one additional read-only surface.
        # API policy must create this actor only when the explicit Bootstrap
        # anonymous-read flag is enabled; no mutation permission is granted.
        return frozenset(
            {Permission.PRODUCT_READ, Permission.RESEARCH_CONSOLE}
        )
    permissions: set[Permission] = set()
    for role in actor.roles:
        permissions.update(ROLE_PERMISSIONS[role])
    return frozenset(permissions)


def has_permission(
    actor: AuthenticatedActor | None,
    permission: Permission,
) -> bool:
    return Permission(permission) in permissions_for_actor(actor)


def require_permission(
    actor: AuthenticatedActor | None,
    permission: Permission,
) -> AuthenticatedActor:
    """Return an authorized actor or fail without consulting request data."""

    if actor is None:
        raise AuthenticationRequiredError("authentication is required")
    required = Permission(permission)
    if not has_permission(actor, required):
        raise AccessDeniedError(f"permission {required.value!r} is required")
    return actor


def activate_break_glass(
    actor: AuthenticatedActor,
    *,
    reason: str,
) -> AuthenticatedActor:
    """Return an explicitly elevated admin actor for an audited request only.

    Persistence of ``reason`` is the caller's responsibility; this helper only
    proves that an elevation cannot be activated silently or by a non-admin.
    """

    require_permission(actor, Permission.BREAK_GLASS)
    if len(reason.strip()) < 12:
        raise BreakGlassPolicyError(
            "break-glass access requires a specific reason"
        )
    return replace(actor, break_glass=True)


def retrieval_authorization_for_actor(
    actor: AuthenticatedActor | None,
) -> RetrievalAuthorization:
    """Derive immutable retrieval clearance from trusted actor state."""

    actor = require_permission(actor, Permission.PRODUCT_READ)
    if actor.is_bootstrap_anonymous:
        return bootstrap_anonymous_retrieval_authorization()

    levels = [
        SensitiveInformationLevel.PUBLIC,
        SensitiveInformationLevel.CONTROLLED,
    ]
    clearance = ClearanceRank.GENERAL_PRINCIPLE
    if RoleName.REVIEWER in actor.roles:
        levels.append(SensitiveInformationLevel.SECRET_METHOD)
        clearance = ClearanceRank.METHOD_DETAIL
    if actor.break_glass:
        # Authenticated admins do not receive this clearance by default.
        require_permission(actor, Permission.BREAK_GLASS)
        levels.append(SensitiveInformationLevel.RESTRICTED)
        clearance = ClearanceRank.OPERATIONAL_SECRET

    return RetrievalAuthorization(
        principal_id=actor.principal_id,
        clearance_rank=clearance,
        allowed_sensitive_levels=levels,
        bootstrap_limited=False,
        break_glass=actor.break_glass,
    )


def bootstrap_anonymous_retrieval_authorization() -> RetrievalAuthorization:
    """Explicitly authorize only Bootstrap-safe anonymous read projections."""

    return RetrievalAuthorization(
        principal_id=BOOTSTRAP_ANONYMOUS_PRINCIPAL,
        clearance_rank=ClearanceRank.GENERAL_PRINCIPLE,
        allowed_sensitive_levels=[
            SensitiveInformationLevel.PUBLIC,
            SensitiveInformationLevel.CONTROLLED,
        ],
        bootstrap_limited=True,
        break_glass=False,
    )


__all__ = [
    "AccessDeniedError",
    "AuthenticatedActor",
    "AuthenticationRequiredError",
    "AuthorizationError",
    "BOOTSTRAP_ANONYMOUS_PRINCIPAL",
    "BreakGlassPolicyError",
    "Permission",
    "ROLE_PERMISSIONS",
    "activate_break_glass",
    "bootstrap_anonymous_actor",
    "bootstrap_anonymous_retrieval_authorization",
    "has_permission",
    "permissions_for_actor",
    "require_permission",
    "retrieval_authorization_for_actor",
]
