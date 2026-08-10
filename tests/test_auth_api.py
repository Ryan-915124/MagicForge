"""HTTP security-boundary tests without external services."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

import app.audit_routes as audit_routes_module
import app.auth_routes as auth_routes_module
import app.security_runtime as security_runtime_module
from app.config import Settings
from app.main import create_app
from app.runtime import RuntimeInitializationError
from knowledge.governance import MagicForgeMode
from persistence import (
    AuditEvent,
    Base,
    DatabaseManager,
    Role,
    RoleName,
    SessionRecord,
)


ADMIN_PASSWORD = "correct horse battery staple"
READER_PASSWORD = "another carefully chosen passphrase"


class _SQLiteAuthenticationManager:
    """Isolated test adapter behind DatabaseRuntime's injectable factory."""

    def __init__(self, path) -> None:
        self._manager = DatabaseManager(f"sqlite+pysqlite:///{path}")

    def start(self) -> None:
        self._manager.start()
        Base.metadata.create_all(self._manager.engine)
        with self._manager.session_factory() as session:
            session.add_all(
                [
                    Role(name=role, description=role.value)
                    for role in RoleName
                ]
            )
            session.commit()

    def require_ready(self):
        return self._manager.require_ready()

    def close(self) -> None:
        self._manager.close()


def _unavailable_corpus(_settings):
    raise RuntimeInitializationError(
        "test_corpus_unavailable",
        "The isolated authentication test has no corpus runtime.",
    )


def _authentication_app(tmp_path):
    manager = _SQLiteAuthenticationManager(tmp_path / "auth-api.sqlite")
    settings = Settings(
        magicforge_mode=MagicForgeMode.BOOTSTRAP,
        database_url="postgresql+psycopg://test:test@database/magicforge",
        auth_cookie_secure=False,
        auth_allowed_origins=("http://testserver",),
        bootstrap_allow_anonymous_reads=False,
    )
    application = create_app(
        settings=settings,
        runtime_builder=_unavailable_corpus,
        database_manager_factory=lambda _url, _timeout: manager,
    )
    return application, manager


def _create_initial_admin(application) -> None:
    application.state.security_services.admin.bootstrap_initial_admin(
        username="ArchiveAdmin",
        email="admin@example.test",
        password=ADMIN_PASSWORD,
    )


async def _bearer_login(client: httpx.AsyncClient, identifier: str, password: str):
    return await client.post(
        "/auth/login",
        json={
            "identifier": identifier,
            "password": password,
            "transport": "bearer",
        },
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def deterministic_security_threadpool(monkeypatch) -> None:
    async def dispatch(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    monkeypatch.setattr(auth_routes_module, "run_in_threadpool", dispatch)
    monkeypatch.setattr(audit_routes_module, "run_in_threadpool", dispatch)
    monkeypatch.setattr(security_runtime_module, "run_in_threadpool", dispatch)


@pytest.mark.anyio
async def test_bearer_login_me_and_token_storage_are_safe(tmp_path) -> None:
    application, manager = _authentication_app(tmp_path)
    async with application.router.lifespan_context(application):
        _create_initial_admin(application)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            invalid = await _bearer_login(client, "ArchiveAdmin", "wrong password")
            assert invalid.status_code == 401
            assert invalid.json()["detail"]["code"] == "invalid_credentials"

            login = await _bearer_login(client, "ArchiveAdmin", ADMIN_PASSWORD)
            assert login.status_code == 200
            body = login.json()
            raw_token = body["access_token"]
            assert body["transport"] == "bearer"
            assert body["actor"]["roles"] == ["admin"]

            me = await client.get(
                "/auth/me", headers={"Authorization": f"Bearer {raw_token}"}
            )
            assert me.status_code == 200
            assert me.json()["username"] == "ArchiveAdmin"

            with manager.require_ready()() as session:
                record = session.scalar(select(SessionRecord))
                assert record is not None
                assert record.token_hash != raw_token
                assert raw_token not in repr(record)
                event = session.scalar(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "session_created"
                    )
                )
                assert event is not None
                assert event.actor_user_id == record.user_id
                assert raw_token not in repr(event.new_state)


@pytest.mark.anyio
async def test_cookie_session_requires_origin_and_csrf(tmp_path) -> None:
    application, _manager = _authentication_app(tmp_path)
    async with application.router.lifespan_context(application):
        _create_initial_admin(application)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            rejected_login = await client.post(
                "/auth/login",
                json={
                    "identifier": "ArchiveAdmin",
                    "password": ADMIN_PASSWORD,
                    "transport": "cookie",
                },
            )
            assert rejected_login.status_code == 403

            login = await client.post(
                "/auth/login",
                headers={"Origin": "http://testserver"},
                json={
                    "identifier": "ArchiveAdmin",
                    "password": ADMIN_PASSWORD,
                    "transport": "cookie",
                },
            )
            assert login.status_code == 200
            assert login.json()["access_token"] is None
            session_cookie = client.cookies.get("magicforge_session")
            csrf_cookie = client.cookies.get("magicforge_csrf")
            assert session_cookie
            assert csrf_cookie
            assert "HttpOnly" in login.headers.get_list("set-cookie")[0]

            missing_csrf = await client.post(
                "/auth/logout", headers={"Origin": "http://testserver"}
            )
            assert missing_csrf.status_code == 403
            assert missing_csrf.json()["detail"]["code"] == (
                "csrf_validation_failed"
            )

            logout = await client.post(
                "/auth/logout",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": csrf_cookie,
                },
            )
            assert logout.status_code == 200
            assert logout.json() == {"revoked": True}

            me = await client.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {session_cookie}"},
            )
            assert me.status_code == 401


@pytest.mark.anyio
async def test_admin_user_role_and_session_routes_enforce_live_rbac(tmp_path) -> None:
    application, manager = _authentication_app(tmp_path)
    async with application.router.lifespan_context(application):
        _create_initial_admin(application)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            admin_login = await _bearer_login(
                client, "ArchiveAdmin", ADMIN_PASSWORD
            )
            admin_token = admin_login.json()["access_token"]
            admin_headers = {"Authorization": f"Bearer {admin_token}"}

            created = await client.post(
                "/admin/users",
                headers=admin_headers,
                json={
                    "username": "ReaderOne",
                    "email": "reader@example.test",
                    "password": READER_PASSWORD,
                    "roles": ["reader"],
                    "reason": "Provision a corpus reader for the API test.",
                },
            )
            assert created.status_code == 201
            reader_id = created.json()["id"]

            duplicate = await client.post(
                "/admin/users",
                headers=admin_headers,
                json={
                    "username": "ReaderOne",
                    "password": READER_PASSWORD,
                    "roles": ["reader"],
                    "reason": "Verify uniqueness conflict behavior.",
                },
            )
            assert duplicate.status_code == 409

            reader_login = await _bearer_login(
                client, "ReaderOne", READER_PASSWORD
            )
            reader_token = reader_login.json()["access_token"]
            reader_session_id = reader_login.json()["actor"]["session_id"]
            reader_headers = {"Authorization": f"Bearer {reader_token}"}

            forbidden = await client.post(
                "/admin/users",
                headers=reader_headers,
                json={
                    "username": "ForbiddenUser",
                    "password": "yet another careful passphrase",
                    "roles": ["reader"],
                    "reason": "A reader must not provision users.",
                },
            )
            assert forbidden.status_code == 403
            assert forbidden.json()["detail"]["code"] == "forbidden"

            role_change = await client.patch(
                f"/admin/users/{reader_id}/roles",
                headers=admin_headers,
                json={
                    "role": "reviewer",
                    "action": "grant",
                    "reason": "Permit governed review in a later checkpoint.",
                },
            )
            assert role_change.status_code == 200
            assert role_change.json()["user"]["roles"] == ["reader", "reviewer"]

            revoked = await client.post(
                f"/admin/sessions/{reader_session_id}/revoke",
                headers=admin_headers,
                json={"reason": "End the reader session during the API test."},
            )
            assert revoked.status_code == 200
            assert revoked.json()["changed"] is True
            assert (await client.get("/auth/me", headers=reader_headers)).status_code == 401

            missing = await client.post(
                f"/admin/sessions/{uuid4()}/revoke",
                headers=admin_headers,
                json={"reason": "Verify a stable missing-resource response."},
            )
            assert missing.status_code == 404

            with manager.require_ready()() as session:
                admin_id = admin_login.json()["actor"]["id"]
                privileged_events = list(
                    session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.event_type.in_(
                                {"user_created", "role_granted", "session_revoked"}
                            )
                        )
                    )
                )
                assert privileged_events
                assert all(str(event.actor_user_id) == admin_id for event in privileged_events)


@pytest.mark.anyio
async def test_audit_catalogue_is_admin_only_paginated_filterable_and_redacted(
    tmp_path,
) -> None:
    application, _manager = _authentication_app(tmp_path)
    async with application.router.lifespan_context(application):
        _create_initial_admin(application)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            assert (await client.get("/audit/events")).status_code == 401

            admin_login = await _bearer_login(
                client, "ArchiveAdmin", ADMIN_PASSWORD
            )
            admin_token = admin_login.json()["access_token"]
            admin_headers = {"Authorization": f"Bearer {admin_token}"}

            created = await client.post(
                "/admin/users",
                headers={
                    **admin_headers,
                    "X-Request-ID": "Bearer hidden trace secret",
                },
                json={
                    "username": "AuditReader",
                    "email": "audit-reader@example.test",
                    "password": READER_PASSWORD,
                    "roles": ["reader"],
                    "reason": "SECRET_METHOD_DO_NOT_EXPOSE",
                },
            )
            assert created.status_code == 201

            reader_login = await _bearer_login(
                client, "AuditReader", READER_PASSWORD
            )
            reader_headers = {
                "Authorization": f"Bearer {reader_login.json()['access_token']}"
            }
            forbidden = await client.get("/audit/events", headers=reader_headers)
            assert forbidden.status_code == 403
            assert forbidden.json()["detail"]["code"] == "forbidden"

            first_page = await client.get(
                "/audit/events?limit=2", headers=admin_headers
            )
            assert first_page.status_code == 200
            assert first_page.headers["cache-control"] == "no-store"
            first_body = first_page.json()
            assert len(first_body["items"]) == 2
            assert first_body["has_more"] is True
            assert first_body["next_cursor"]

            first_ids = {item["event_id"] for item in first_body["items"]}
            for item in first_body["items"]:
                assert item["actor_username"]
                assert "previous_state" not in item
                assert "new_state" not in item
                assert "metadata" not in item
                assert "reason" not in item
                serialized = str(item).casefold()
                assert "password_hash" not in serialized
                assert "secret_method_do_not_expose" not in serialized
                assert admin_token.casefold() not in serialized

            second_page = await client.get(
                "/audit/events",
                headers=admin_headers,
                params={"limit": 2, "cursor": first_body["next_cursor"]},
            )
            assert second_page.status_code == 200
            second_ids = {item["event_id"] for item in second_page.json()["items"]}
            assert first_ids.isdisjoint(second_ids)

            filtered = await client.get(
                "/audit/events",
                headers=admin_headers,
                params={"event_type": "user_created", "object_type": "user"},
            )
            assert filtered.status_code == 200
            assert len(filtered.json()["items"]) == 1
            filtered_item = filtered.json()["items"][0]
            assert filtered_item["event_type"] == "user_created"
            assert filtered_item["object_id"] == created.json()["id"]
            assert filtered_item["request_id"] is None
            assert filtered_item["new_state_fields"] == [
                "email",
                "roles",
                "status",
                "username",
            ]

            mismatched_cursor = await client.get(
                "/audit/events",
                headers=admin_headers,
                params={
                    "limit": 2,
                    "cursor": first_body["next_cursor"],
                    "event_type": "user_created",
                },
            )
            assert mismatched_cursor.status_code == 422
            assert mismatched_cursor.json()["detail"]["code"] == (
                "audit_cursor_filter_mismatch"
            )

            invalid_cursor = await client.get(
                "/audit/events",
                headers=admin_headers,
                params={"cursor": "not-a-valid-cursor"},
            )
            assert invalid_cursor.status_code == 422
            assert invalid_cursor.json()["detail"]["code"] == (
                "invalid_audit_cursor"
            )

            naive_time = await client.get(
                "/audit/events",
                headers=admin_headers,
                params={"created_from": "2026-08-09T12:00:00"},
            )
            assert naive_time.status_code == 422
            assert naive_time.json()["detail"]["code"] == (
                "invalid_audit_time_range"
            )

            reversed_time = await client.get(
                "/audit/events",
                headers=admin_headers,
                params={
                    "created_from": "2026-08-10T12:00:00+08:00",
                    "created_to": "2026-08-09T12:00:00+08:00",
                },
            )
            assert reversed_time.status_code == 422
            assert reversed_time.json()["detail"]["code"] == (
                "invalid_audit_time_range"
            )


@pytest.mark.anyio
async def test_anonymous_production_product_access_is_denied_before_dependencies() -> None:
    application = create_app(
        settings=Settings(magicforge_mode=MagicForgeMode.PRODUCTION),
        runtime_builder=_unavailable_corpus,
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/assistant", json={"question": "Explain temporal misdirection."}
            )

    assert response.status_code == 401
    assert response.json() == {
        "detail": {
            "code": "unauthenticated",
            "message": "Authentication is required.",
        }
    }
