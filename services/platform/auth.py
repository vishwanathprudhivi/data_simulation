"""Basic username/password auth with bearer tokens and workspace isolation (FR-API-07, Q10)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from services.platform.metadata import MetadataStore

__all__ = ["AuthError", "authenticate", "hash_password", "issue_token", "verify_password"]


class AuthError(PermissionError):
    pass


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hash_password(password, bytes.fromhex(salt)).split("$")[2]
    return hmac.compare_digest(check, digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(store: MetadataStore, user: str, password: str) -> str:
    row = store.user(user)
    if row is None or not verify_password(password, str(row["password_hash"])):
        raise AuthError("invalid username or password")
    token = secrets.token_urlsafe(32)
    store.add_token(_token_hash(token), user)
    return token


def authenticate(store: MetadataStore, header: str | None) -> str:
    """``Authorization`` header (Bearer token or Basic) -> user id."""
    if not header:
        raise AuthError("missing Authorization header")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer":
        user = store.token_user(_token_hash(value.strip()))
        if user is None:
            raise AuthError("invalid or expired token")
        return user
    if scheme.lower() == "basic":
        try:
            user, _, password = base64.b64decode(value).decode().partition(":")
        except Exception as exc:
            raise AuthError("malformed Basic credentials") from exc
        row = store.user(user)
        if row is None or not verify_password(password, str(row["password_hash"])):
            raise AuthError("invalid username or password")
        return user
    raise AuthError(f"unsupported auth scheme {scheme!r}")
