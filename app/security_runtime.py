"""FastAPI authentication boundary and request-scoped authorization helpers.

Raw credentials are confined to this HTTP adapter.  Application routes receive
only an immutable actor and a server-derived retrieval authorization object.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.database_runtime import DatabaseRuntime, DatabaseRuntimeError
from persistence.models import SessionTransport
from security.audit_read import AuditReadService
from security.crypto import PasswordPolicyError
from security.policy import (
    AccessDeniedError,
    AuthenticatedActor,
    AuthenticationRequiredError,
    Permission,
    bootstrap_anonymous_actor,
    require_permission,
)
from security.services import (
    AdminService,
    AuthService,
    CsrfValidationError,
    SecurityServiceError,
)


@dataclass(frozen=True, slots=True)
class SecurityServices:
    auth: AuthService
    admin: AdminService
    audit: AuditReadService


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    actor: AuthenticatedActor
    transport: SessionTransport
    token: str

    def __repr__(self) -> str:
        return (
            "AuthenticatedSession("
            f"actor={self.actor!r}, transport={self.transport!r}, token=<redacted>)"
        )


def build_security_services(
    database_runtime: DatabaseRuntime,
    settings: Settings,
) -> SecurityServices | None:
    """Build services once per application lifespan when SQL is configured."""

    try:
        session_factory = database_runtime.require_database_ready()
    except DatabaseRuntimeError:
        if database_runtime.required:
            raise
        return None
    return SecurityServices(
        auth=AuthService(
            session_factory,
            session_ttl=timedelta(seconds=settings.auth_session_ttl_seconds),
            last_used_write_interval=timedelta(
                seconds=settings.auth_last_used_update_interval_seconds
            ),
        ),
        admin=AdminService(session_factory),
        audit=AuditReadService(session_factory),
    )


def get_security_services(request: Request) -> SecurityServices:
    services: SecurityServices | None = request.app.state.security_services
    if services is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "authentication_not_ready",
                "message": "Authentication persistence is unavailable.",
            },
        )
    return services


def request_context(request: Request) -> tuple[str | None, str | None]:
    """Return bounded tracing IDs; never derive them from credential headers."""

    return (
        _bounded_header(request.headers.get("x-request-id")),
        _bounded_header(request.headers.get("x-correlation-id")),
    )


def client_metadata(request: Request, device_label: str | None) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if device_label and device_label.strip():
        metadata["device_label"] = device_label.strip()[:200]
    if request.client is not None and request.client.host:
        metadata["remote_address"] = request.client.host[:200]
    user_agent = request.headers.get("user-agent", "").strip()
    if user_agent:
        metadata["user_agent"] = user_agent[:500]
    return metadata


def enforce_cookie_origin(request: Request) -> None:
    """Require an exact trusted Origin for every cookie-auth mutation."""

    origin = request.headers.get("origin", "").strip().rstrip("/")
    settings: Settings = request.app.state.settings
    if not origin or origin not in settings.auth_allowed_origins:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "origin_validation_failed",
                "message": "The request origin is not authorized.",
            },
        )


def enforce_cookie_csrf(
    request: Request,
    session: AuthenticatedSession,
) -> None:
    enforce_actor_csrf(request, session.actor)


def enforce_actor_csrf(request: Request, actor: AuthenticatedActor) -> None:
    """Apply Origin + double-submit + persisted-token checks to cookie actors."""

    if actor.session_transport != SessionTransport.COOKIE:
        return
    enforce_cookie_origin(request)
    settings: Settings = request.app.state.settings
    header_token = request.headers.get("x-csrf-token", "")
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name, "")
    if (
        not header_token
        or not cookie_token
        or not secrets.compare_digest(header_token, cookie_token)
    ):
        raise security_http_exception(CsrfValidationError())
    services = get_security_services(request)
    try:
        services.auth.verify_csrf(actor, header_token)
    except SecurityServiceError as exc:
        raise security_http_exception(exc) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc


async def get_authenticated_session(request: Request) -> AuthenticatedSession:
    token, transport = _request_credential(request)
    if token is None or transport is None:
        raise unauthenticated()
    return await run_in_threadpool(
        _authenticate_credential,
        request,
        token,
        transport,
    )


async def get_authenticated_actor(
    session: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
) -> AuthenticatedActor:
    return session.actor


async def get_product_actor(request: Request) -> AuthenticatedActor:
    token, transport = _request_credential(request)
    settings: Settings = request.app.state.settings
    if token is None and transport is None:
        if settings.anonymous_product_reads_enabled:
            return bootstrap_anonymous_actor()
        raise unauthenticated()

    # An invalid or unavailable configured session must never fall back to the
    # Bootstrap anonymous principal.
    session = await run_in_threadpool(
        _authenticate_credential,
        request,
        token,
        transport,
    )
    try:
        return require_permission(session.actor, Permission.PRODUCT_READ)
    except (AuthenticationRequiredError, AccessDeniedError) as exc:
        raise authorization_http_exception(exc) from exc


async def get_research_console_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_product_actor)],
) -> AuthenticatedActor:
    try:
        return require_permission(actor, Permission.RESEARCH_CONSOLE)
    except (AuthenticationRequiredError, AccessDeniedError) as exc:
        raise authorization_http_exception(exc) from exc


async def get_source_submit_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.SOURCE_REGISTER)


async def get_claim_submit_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.CLAIM_SUBMIT)


async def get_extraction_run_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.EXTRACTION_RUN)


async def get_mapping_submit_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.MAPPING_SUBMIT)


async def get_source_review_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.SOURCE_REVIEW)


async def get_claim_review_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.CLAIM_REVIEW)


async def get_mapping_review_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.MAPPING_REVIEW)


async def get_user_admin_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.USER_ADMIN)


async def get_role_admin_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.ROLE_ADMIN)


async def get_session_admin_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.SESSION_REVOKE)


async def get_audit_read_actor(
    actor: Annotated[AuthenticatedActor, Depends(get_authenticated_actor)],
) -> AuthenticatedActor:
    return _permission_actor(actor, Permission.AUDIT_READ)


ProductActorDependency = Annotated[AuthenticatedActor, Depends(get_product_actor)]
ResearchActorDependency = Annotated[
    AuthenticatedActor, Depends(get_research_console_actor)
]
StrictActorDependency = Annotated[
    AuthenticatedActor, Depends(get_authenticated_actor)
]
AuthenticatedSessionDependency = Annotated[
    AuthenticatedSession, Depends(get_authenticated_session)
]
UserAdminDependency = Annotated[
    AuthenticatedActor, Depends(get_user_admin_actor)
]
RoleAdminDependency = Annotated[
    AuthenticatedActor, Depends(get_role_admin_actor)
]
SessionAdminDependency = Annotated[
    AuthenticatedActor, Depends(get_session_admin_actor)
]
AuditReadDependency = Annotated[AuthenticatedActor, Depends(get_audit_read_actor)]
SourceSubmitDependency = Annotated[
    AuthenticatedActor, Depends(get_source_submit_actor)
]
ClaimSubmitDependency = Annotated[
    AuthenticatedActor, Depends(get_claim_submit_actor)
]
ExtractionRunDependency = Annotated[
    AuthenticatedActor, Depends(get_extraction_run_actor)
]
MappingSubmitDependency = Annotated[
    AuthenticatedActor, Depends(get_mapping_submit_actor)
]
SourceReviewDependency = Annotated[
    AuthenticatedActor, Depends(get_source_review_actor)
]
ClaimReviewDependency = Annotated[
    AuthenticatedActor, Depends(get_claim_review_actor)
]
MappingReviewDependency = Annotated[
    AuthenticatedActor, Depends(get_mapping_review_actor)
]


def security_http_exception(exc: SecurityServiceError) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.public_message},
        headers=headers,
    )


def password_policy_http_exception(exc: PasswordPolicyError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "password_policy_failed", "message": str(exc)},
    )


def authorization_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthenticationRequiredError):
        return unauthenticated()
    return HTTPException(
        status_code=403,
        detail={
            "code": "forbidden",
            "message": "The authenticated actor is not authorized.",
        },
    )


def unauthenticated(code: str = "unauthenticated") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": code, "message": "Authentication is required."},
        headers={"WWW-Authenticate": "Bearer"},
    )


def dependency_failure() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "authentication_dependency_unavailable",
            "message": "Authentication persistence is temporarily unavailable.",
        },
    )


def _request_credential(
    request: Request,
) -> tuple[str | None, SessionTransport | None]:
    settings: Settings = request.app.state.settings
    cookie_token = request.cookies.get(settings.auth_session_cookie_name)
    authorization = request.headers.get("authorization")
    bearer_token: str | None = None
    if authorization is not None:
        scheme, separator, value = authorization.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not value.strip()
            or " " in value.strip()
        ):
            raise unauthenticated("invalid_authorization_header")
        bearer_token = value.strip()
    if cookie_token and bearer_token:
        raise unauthenticated("multiple_credentials")
    if bearer_token:
        return bearer_token, SessionTransport.BEARER
    if cookie_token:
        return cookie_token, SessionTransport.COOKIE
    return None, None


def _authenticate_credential(
    request: Request,
    token: str | None,
    transport: SessionTransport | None,
) -> AuthenticatedSession:
    if token is None or transport is None:
        raise unauthenticated()
    services = get_security_services(request)
    request_id, correlation_id = request_context(request)
    try:
        actor = services.auth.authenticate(
            token,
            expected_transport=transport,
            request_id=request_id,
            correlation_id=correlation_id,
        )
    except SecurityServiceError as exc:
        raise security_http_exception(exc) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc
    return AuthenticatedSession(actor=actor, transport=transport, token=token)


def _permission_actor(
    actor: AuthenticatedActor,
    permission: Permission,
) -> AuthenticatedActor:
    try:
        return require_permission(actor, permission)
    except (AuthenticationRequiredError, AccessDeniedError) as exc:
        raise authorization_http_exception(exc) from exc


def _bounded_header(value: str | None) -> str | None:
    """Accept trace identifiers only, never arbitrary request content."""

    if value is None:
        return None
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", normalized):
        return None
    return normalized


__all__ = [
    "AuditReadDependency",
    "AuthenticatedSession",
    "AuthenticatedSessionDependency",
    "ProductActorDependency",
    "ResearchActorDependency",
    "ClaimReviewDependency",
    "ClaimSubmitDependency",
    "MappingReviewDependency",
    "MappingSubmitDependency",
    "RoleAdminDependency",
    "SecurityServices",
    "SessionAdminDependency",
    "SourceReviewDependency",
    "SourceSubmitDependency",
    "StrictActorDependency",
    "UserAdminDependency",
    "build_security_services",
    "client_metadata",
    "dependency_failure",
    "enforce_actor_csrf",
    "enforce_cookie_csrf",
    "enforce_cookie_origin",
    "get_security_services",
    "password_policy_http_exception",
    "request_context",
    "security_http_exception",
]
