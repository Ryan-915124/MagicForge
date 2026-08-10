"""Password and opaque-token primitives for MagicForge authentication.

Only derived password hashes and SHA-256 token digests are persistence-safe.
Raw session and CSRF tokens must stay at the request/response boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class PasswordPolicyError(ValueError):
    """Raised when a new password does not meet the bounded baseline policy."""


class PasswordHasher:
    """Small Argon2id adapter with explicit production-oriented parameters."""

    MIN_LENGTH = 12
    MAX_LENGTH = 128

    def __init__(self) -> None:
        self._hasher = _Argon2PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def validate_new_password(
        self,
        password: str,
        *,
        identity_terms: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(password, str):
            raise PasswordPolicyError("password must be text")
        if len(password) < self.MIN_LENGTH:
            raise PasswordPolicyError(
                f"password must contain at least {self.MIN_LENGTH} characters"
            )
        if len(password) > self.MAX_LENGTH:
            raise PasswordPolicyError(
                f"password must contain at most {self.MAX_LENGTH} characters"
            )
        if not password.strip():
            raise PasswordPolicyError("password must not be blank")
        normalized_password = password.strip().casefold()
        for term in identity_terms:
            normalized_term = term.strip().casefold()
            if normalized_term and normalized_password == normalized_term:
                raise PasswordPolicyError("password must not equal a user identifier")

    def hash(self, password: str) -> str:
        if not isinstance(password, str):
            raise PasswordPolicyError("password must be text")
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        if not isinstance(password, str) or len(password) > self.MAX_LENGTH:
            return False
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except (InvalidHashError, TypeError):
            return True


def hash_token(token: str) -> str:
    """Return a storage-safe SHA-256 digest for an opaque token."""

    if not isinstance(token, str) or not token or len(token) > 512:
        raise ValueError("token is invalid")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_hashes_equal(left: str, right: str) -> bool:
    """Compare fixed-length token digests without content-dependent timing."""

    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    session_token: str = field(repr=False)
    csrf_token: str | None = field(repr=False)


class TokenFactory:
    """Generate independent 256-bit URL-safe session and CSRF tokens."""

    TOKEN_BYTES = 32

    def issue(self, *, include_csrf: bool) -> IssuedTokens:
        return IssuedTokens(
            session_token=secrets.token_urlsafe(self.TOKEN_BYTES),
            csrf_token=(
                secrets.token_urlsafe(self.TOKEN_BYTES) if include_csrf else None
            ),
        )


__all__ = [
    "IssuedTokens",
    "PasswordHasher",
    "PasswordPolicyError",
    "TokenFactory",
    "hash_token",
    "token_hashes_equal",
]
