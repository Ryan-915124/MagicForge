"""Authentication and administrator HTTP routes for Checkpoint 2."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.security_runtime import (
    AuthenticatedSessionDependency,
    RoleAdminDependency,
    SessionAdminDependency,
    StrictActorDependency,
    UserAdminDependency,
    client_metadata,
    dependency_failure,
    enforce_actor_csrf,
    enforce_cookie_csrf,
    enforce_cookie_origin,
    get_security_services,
    password_policy_http_exception,
    request_context,
    security_http_exception,
)
from persistence.models import RoleName, SessionTransport, UserStatus
from security.crypto import PasswordPolicyError
from security.policy import AuthenticatedActor
from security.services import (
    RoleChangeResult,
    SecurityServiceError,
    SessionCredentials,
    SessionRevocationResult,
    UserSummary,
)


router = APIRouter()


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    transport: SessionTransport = SessionTransport.COOKIE
    device_label: str | None = Field(default=None, max_length=200)


class ActorResponse(BaseModel):
    id: UUID
    username: str
    roles: list[RoleName]
    session_id: UUID
    session_transport: SessionTransport
    session_expires_at: datetime

    @classmethod
    def from_actor(cls, actor: AuthenticatedActor) -> "ActorResponse":
        assert actor.user_id is not None
        assert actor.session_id is not None
        assert actor.session_transport is not None
        assert actor.session_expires_at is not None
        return cls(
            id=actor.user_id,
            username=actor.username,
            roles=sorted(actor.roles, key=lambda value: value.value),
            session_id=actor.session_id,
            session_transport=actor.session_transport,
            session_expires_at=actor.session_expires_at,
        )


class LoginResponse(BaseModel):
    actor: ActorResponse
    transport: SessionTransport
    token_type: Literal["Bearer"] | None = None
    access_token: str | None = None
    csrf_required: bool


class LogoutResponse(BaseModel):
    revoked: bool


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=320)
    password: str = Field(min_length=12, max_length=128)
    roles: list[RoleName] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2_000)


class UserResponse(BaseModel):
    id: UUID
    username: str
    email: str | None
    status: UserStatus
    roles: list[RoleName]
    created_at: datetime

    @classmethod
    def from_summary(cls, user: UserSummary) -> "UserResponse":
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            status=user.status,
            roles=sorted(user.roles, key=lambda value: value.value),
            created_at=user.created_at,
        )


class RoleChangeRequest(BaseModel):
    role: RoleName
    action: Literal["grant", "revoke"]
    reason: str = Field(min_length=1, max_length=2_000)


class RoleChangeResponse(BaseModel):
    user: UserResponse
    role: RoleName
    changed: bool

    @classmethod
    def from_result(cls, result: RoleChangeResult) -> "RoleChangeResponse":
        return cls(
            user=UserResponse.from_summary(result.user),
            role=result.role,
            changed=result.changed,
        )


class SessionRevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)


class SessionRevokeResponse(BaseModel):
    session_id: UUID
    changed: bool

    @classmethod
    def from_result(
        cls, result: SessionRevocationResult
    ) -> "SessionRevokeResponse":
        return cls(session_id=result.session_id, changed=result.changed)


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> LoginResponse:
    services = get_security_services(request)
    if payload.transport == SessionTransport.COOKIE:
        enforce_cookie_origin(request)
    request_id, correlation_id = request_context(request)
    try:
        credentials = await run_in_threadpool(
            services.auth.login,
            identifier=payload.identifier,
            password=payload.password,
            transport=payload.transport,
            client_metadata=client_metadata(request, payload.device_label),
            request_id=request_id,
            correlation_id=correlation_id,
        )
    except SecurityServiceError as exc:
        raise security_http_exception(exc) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc

    response.headers["Cache-Control"] = "no-store"
    if payload.transport == SessionTransport.COOKIE:
        _set_auth_cookies(response, request, credentials)
        return LoginResponse(
            actor=ActorResponse.from_actor(credentials.actor),
            transport=payload.transport,
            csrf_required=True,
        )
    return LoginResponse(
        actor=ActorResponse.from_actor(credentials.actor),
        transport=payload.transport,
        token_type="Bearer",
        access_token=credentials.session_token,
        csrf_required=False,
    )


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    session: AuthenticatedSessionDependency,
) -> LogoutResponse:
    await run_in_threadpool(enforce_cookie_csrf, request, session)
    services = get_security_services(request)
    request_id, correlation_id = request_context(request)
    try:
        revoked = await run_in_threadpool(
            services.auth.logout,
            session.token,
            request_id=request_id,
            correlation_id=correlation_id,
        )
    except SecurityServiceError as exc:
        raise security_http_exception(exc) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc
    response.headers["Cache-Control"] = "no-store"
    if session.transport == SessionTransport.COOKIE:
        _clear_auth_cookies(response, request)
    return LogoutResponse(revoked=revoked)


@router.get("/auth/me", response_model=ActorResponse)
async def me(actor: StrictActorDependency, response: Response) -> ActorResponse:
    response.headers["Cache-Control"] = "no-store"
    return ActorResponse.from_actor(actor)


@router.post(
    "/admin/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    actor: UserAdminDependency,
) -> UserResponse:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    services = get_security_services(request)
    request_id, correlation_id = request_context(request)
    try:
        user = await run_in_threadpool(
            services.admin.create_user,
            actor,
            username=payload.username,
            email=payload.email,
            password=payload.password,
            roles=payload.roles,
            reason=payload.reason,
            request_id=request_id,
            correlation_id=correlation_id,
        )
    except PasswordPolicyError as exc:
        raise password_policy_http_exception(exc) from exc
    except SecurityServiceError as exc:
        raise security_http_exception(exc) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc
    return UserResponse.from_summary(user)


@router.patch("/admin/users/{user_id}/roles", response_model=RoleChangeResponse)
async def change_role(
    user_id: UUID,
    payload: RoleChangeRequest,
    request: Request,
    actor: RoleAdminDependency,
) -> RoleChangeResponse:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    services = get_security_services(request)
    request_id, correlation_id = request_context(request)
    try:
        operation = (
            services.admin.grant_role
            if payload.action == "grant"
            else services.admin.revoke_role
        )
        result = await run_in_threadpool(
            operation,
            actor,
            user_id=user_id,
            role=payload.role,
            reason=payload.reason,
            request_id=request_id,
            correlation_id=correlation_id,
        )
    except SecurityServiceError as exc:
        raise security_http_exception(exc) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc
    return RoleChangeResponse.from_result(result)


@router.post(
    "/admin/sessions/{session_id}/revoke",
    response_model=SessionRevokeResponse,
)
async def revoke_session(
    session_id: UUID,
    payload: SessionRevokeRequest,
    request: Request,
    actor: SessionAdminDependency,
) -> SessionRevokeResponse:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    services = get_security_services(request)
    request_id, correlation_id = request_context(request)
    try:
        result = await run_in_threadpool(
            services.admin.revoke_session,
            actor,
            session_id=session_id,
            reason=payload.reason,
            request_id=request_id,
            correlation_id=correlation_id,
        )
    except SecurityServiceError as exc:
        raise security_http_exception(exc) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc
    return SessionRevokeResponse.from_result(result)


def _set_auth_cookies(
    response: Response,
    request: Request,
    credentials: SessionCredentials,
) -> None:
    settings = request.app.state.settings
    assert credentials.csrf_token is not None
    cookie_options = {
        "max_age": settings.auth_session_ttl_seconds,
        "secure": settings.auth_cookie_secure,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(
        settings.auth_session_cookie_name,
        credentials.session_token,
        httponly=True,
        **cookie_options,
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        credentials.csrf_token,
        httponly=False,
        **cookie_options,
    )


def _clear_auth_cookies(response: Response, request: Request) -> None:
    settings = request.app.state.settings
    for name in (
        settings.auth_session_cookie_name,
        settings.auth_csrf_cookie_name,
    ):
        response.delete_cookie(
            name,
            path="/",
            secure=settings.auth_cookie_secure,
            samesite="lax",
        )


__all__ = ["router"]
