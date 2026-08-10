from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge.governance import ClearanceRank, SensitiveInformationLevel
from persistence.models import RoleName
from retrieval.interfaces import RetrievalAuthorization
from security.policy import (
    AccessDeniedError,
    AuthenticatedActor,
    Permission,
    activate_break_glass,
    bootstrap_anonymous_actor,
    bootstrap_anonymous_retrieval_authorization,
    require_permission,
    retrieval_authorization_for_actor,
)


def _actor(*roles: RoleName) -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        username="policy-user",
        roles=frozenset(roles),
    )


def test_role_permissions_are_additive_without_operator_review_escalation() -> None:
    operator = _actor(RoleName.OPERATOR)
    require_permission(operator, Permission.MANIFEST_INGEST)
    require_permission(operator, Permission.SOURCE_REGISTER)
    require_permission(operator, Permission.CLAIM_SUBMIT)
    with pytest.raises(AccessDeniedError):
        require_permission(operator, Permission.CLAIM_REVIEW)

    reviewer = _actor(RoleName.REVIEWER)
    require_permission(reviewer, Permission.SOURCE_REVIEW)
    require_permission(reviewer, Permission.CLAIM_REVIEW)
    with pytest.raises(AccessDeniedError):
        require_permission(reviewer, Permission.SOURCE_REGISTER)
    with pytest.raises(AccessDeniedError):
        require_permission(reviewer, Permission.CLAIM_SUBMIT)

    combined = _actor(RoleName.OPERATOR, RoleName.REVIEWER)
    require_permission(combined, Permission.MANIFEST_INGEST)
    require_permission(combined, Permission.CLAIM_REVIEW)


def test_retrieval_clearance_is_derived_from_roles_not_operator_status() -> None:
    reader = retrieval_authorization_for_actor(_actor(RoleName.READER))
    operator = retrieval_authorization_for_actor(_actor(RoleName.OPERATOR))
    reviewer = retrieval_authorization_for_actor(_actor(RoleName.REVIEWER))

    assert reader.clearance_rank == ClearanceRank.GENERAL_PRINCIPLE
    assert operator.clearance_rank == ClearanceRank.GENERAL_PRINCIPLE
    assert SensitiveInformationLevel.SECRET_METHOD not in operator.allowed_sensitive_levels
    assert reviewer.clearance_rank == ClearanceRank.METHOD_DETAIL
    assert SensitiveInformationLevel.SECRET_METHOD in reviewer.allowed_sensitive_levels


def test_admin_requires_explicit_reasoned_break_glass_for_restricted_access() -> None:
    admin = _actor(RoleName.ADMIN)
    normal = retrieval_authorization_for_actor(admin)

    assert normal.break_glass is False
    assert SensitiveInformationLevel.RESTRICTED not in normal.allowed_sensitive_levels

    elevated = activate_break_glass(
        admin,
        reason="Investigate a documented security incident",
    )
    restricted = retrieval_authorization_for_actor(elevated)
    assert restricted.break_glass is True
    assert restricted.clearance_rank == ClearanceRank.OPERATIONAL_SECRET
    assert SensitiveInformationLevel.RESTRICTED in restricted.allowed_sensitive_levels


def test_bootstrap_anonymous_access_uses_a_dedicated_limited_principal() -> None:
    actor = bootstrap_anonymous_actor()
    authorization = bootstrap_anonymous_retrieval_authorization()

    require_permission(actor, Permission.PRODUCT_READ)
    require_permission(actor, Permission.RESEARCH_CONSOLE)
    with pytest.raises(AccessDeniedError):
        require_permission(actor, Permission.CLAIM_REVIEW)
    assert authorization.bootstrap_limited is True
    assert authorization.clearance_rank == ClearanceRank.GENERAL_PRINCIPLE
    assert set(authorization.allowed_sensitive_levels) == {
        SensitiveInformationLevel.PUBLIC,
        SensitiveInformationLevel.CONTROLLED,
    }


def test_retrieval_authorization_has_no_permissive_constructor_defaults() -> None:
    with pytest.raises(ValidationError):
        RetrievalAuthorization()

    with pytest.raises(ValidationError):
        RetrievalAuthorization(
            principal_id="reader",
            clearance_rank=ClearanceRank.OPERATIONAL_SECRET,
            allowed_sensitive_levels=[SensitiveInformationLevel.RESTRICTED],
        )
