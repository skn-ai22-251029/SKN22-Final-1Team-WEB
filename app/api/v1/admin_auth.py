from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.core import signing
from rest_framework import authentication
from rest_framework import exceptions
from rest_framework.permissions import BasePermission

from app.services.model_team_bridge import (
    get_admin_by_identifier,
    get_admin_by_legacy_id,
    get_client_by_identifier,
    get_client_by_legacy_id,
    get_legacy_admin_id,
    get_legacy_client_id,
)
from app.services.runtime_client import RuntimeClient as Client

if TYPE_CHECKING:
    from app.models_django import AdminAccount


ADMIN_ACCESS_TOKEN_SALT = "mirrai.admin.auth.v1"
ADMIN_REFRESH_TOKEN_SALT = "mirrai.admin.refresh.v1"
CLIENT_ACCESS_TOKEN_SALT = "mirrai.client.auth.v1"
CLIENT_REFRESH_TOKEN_SALT = "mirrai.client.refresh.v1"
ACCESS_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 12
REFRESH_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24
TOKEN_MAX_AGE_SECONDS = ACCESS_TOKEN_MAX_AGE_SECONDS


def get_admin_auth_policy_snapshot() -> dict:
    return {
        "token_type": "bearer",
        "refresh_token_supported": True,
        "token_max_age_seconds": ACCESS_TOKEN_MAX_AGE_SECONDS,
        "refresh_token_max_age_seconds": REFRESH_TOKEN_MAX_AGE_SECONDS,
    }


def _build_signed_token(*, payload: dict, salt: str) -> str:
    return signing.dumps(
        payload,
        key=settings.SECRET_KEY,
        salt=salt,
        compress=True,
    )


def _decode_signed_token(
    token: str,
    *,
    salt: str,
    max_age: int,
    expired_message: str,
    invalid_message: str,
) -> dict:
    try:
        return signing.loads(
            token,
            key=settings.SECRET_KEY,
            salt=salt,
            max_age=max_age,
        )
    except signing.SignatureExpired as exc:
        raise exceptions.AuthenticationFailed(expired_message) from exc
    except signing.BadSignature as exc:
        raise exceptions.AuthenticationFailed(invalid_message) from exc


def build_admin_token(*, admin: AdminAccount) -> str:
    payload = {
        "type": "admin",
        "token_kind": "access",
        "admin_id": admin.id,
        "legacy_admin_id": get_legacy_admin_id(admin=admin),
        "role": admin.role,
        "store_name": admin.store_name,
    }
    return _build_signed_token(payload=payload, salt=ADMIN_ACCESS_TOKEN_SALT)


def build_admin_refresh_token(*, admin: AdminAccount) -> str:
    payload = {
        "type": "admin",
        "token_kind": "refresh",
        "admin_id": admin.id,
        "legacy_admin_id": get_legacy_admin_id(admin=admin),
    }
    return _build_signed_token(payload=payload, salt=ADMIN_REFRESH_TOKEN_SALT)


def build_client_token(*, client: Client) -> str:
    payload = {
        "type": "client",
        "token_kind": "access",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "phone": client.phone,
    }
    return _build_signed_token(payload=payload, salt=CLIENT_ACCESS_TOKEN_SALT)


def build_client_refresh_token(*, client: Client) -> str:
    payload = {
        "type": "client",
        "token_kind": "refresh",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
    }
    return _build_signed_token(payload=payload, salt=CLIENT_REFRESH_TOKEN_SALT)


def decode_admin_token(token: str) -> dict:
    payload = _decode_signed_token(
        token,
        salt=ADMIN_ACCESS_TOKEN_SALT,
        max_age=ACCESS_TOKEN_MAX_AGE_SECONDS,
        expired_message="Admin token expired.",
        invalid_message="Invalid admin token.",
    )

    if payload.get("type") != "admin":
        raise exceptions.AuthenticationFailed("Unsupported token type.")
    if payload.get("token_kind") != "access":
        raise exceptions.AuthenticationFailed("Unsupported admin token kind.")
    return payload


def decode_admin_refresh_token(token: str) -> dict:
    payload = _decode_signed_token(
        token,
        salt=ADMIN_REFRESH_TOKEN_SALT,
        max_age=REFRESH_TOKEN_MAX_AGE_SECONDS,
        expired_message="Admin refresh token expired.",
        invalid_message="Invalid admin refresh token.",
    )
    if payload.get("type") != "admin":
        raise exceptions.AuthenticationFailed("Unsupported token type.")
    if payload.get("token_kind") != "refresh":
        raise exceptions.AuthenticationFailed("Unsupported admin token kind.")
    return payload


def decode_client_refresh_token(token: str) -> dict:
    payload = _decode_signed_token(
        token,
        salt=CLIENT_REFRESH_TOKEN_SALT,
        max_age=REFRESH_TOKEN_MAX_AGE_SECONDS,
        expired_message="Client refresh token expired.",
        invalid_message="Invalid client refresh token.",
    )
    if payload.get("type") != "client":
        raise exceptions.AuthenticationFailed("Unsupported token type.")
    if payload.get("token_kind") != "refresh":
        raise exceptions.AuthenticationFailed("Unsupported client token kind.")
    return payload


def issue_admin_token_pair(*, admin: AdminAccount) -> dict:
    return {
        "admin_id": admin.id,
        "legacy_admin_id": get_legacy_admin_id(admin=admin),
        "access_token": build_admin_token(admin=admin),
        "refresh_token": build_admin_refresh_token(admin=admin),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_MAX_AGE_SECONDS,
        "refresh_expires_in": REFRESH_TOKEN_MAX_AGE_SECONDS,
    }


def issue_client_token_pair(*, client: Client) -> dict:
    return {
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "access_token": build_client_token(client=client),
        "refresh_token": build_client_refresh_token(client=client),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_MAX_AGE_SECONDS,
        "refresh_expires_in": REFRESH_TOKEN_MAX_AGE_SECONDS,
    }


def _resolve_admin_from_payload(payload: dict) -> AdminAccount | None:
    legacy_admin_id = payload.get("legacy_admin_id")
    if legacy_admin_id:
        admin = get_admin_by_legacy_id(legacy_admin_id=legacy_admin_id)
        if admin is not None:
            return admin
    return get_admin_by_identifier(identifier=payload.get("admin_id"))


def _resolve_client_from_payload(payload: dict) -> Client | None:
    legacy_client_id = payload.get("legacy_client_id")
    if legacy_client_id:
        client = get_client_by_legacy_id(legacy_client_id=legacy_client_id)
        if client is not None:
            return client
    return get_client_by_identifier(identifier=payload.get("client_id"))


def refresh_admin_access_token(*, refresh_token: str) -> dict:
    payload = decode_admin_refresh_token(refresh_token)
    admin = _resolve_admin_from_payload(payload)
    if admin is None:
        raise exceptions.AuthenticationFailed("Admin account not found.")
    return {
        **issue_admin_token_pair(admin=admin),
    }


def refresh_client_access_token(*, refresh_token: str) -> dict:
    payload = decode_client_refresh_token(refresh_token)
    client = _resolve_client_from_payload(payload)
    if client is None:
        raise exceptions.AuthenticationFailed("Client account not found.")
    return {
        **issue_client_token_pair(client=client),
    }


class AdminTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        auth_header = authentication.get_authorization_header(request).decode("utf-8").strip()
        if not auth_header:
            return None

        keyword, _, token = auth_header.partition(" ")
        if keyword.lower() != self.keyword.lower() or not token:
            raise exceptions.AuthenticationFailed("Authorization header must use Bearer token.")

        payload = decode_admin_token(token)
        admin = _resolve_admin_from_payload(payload)
        if admin is None:
            raise exceptions.AuthenticationFailed("Admin account not found.")
        return admin, payload


class IsAuthenticatedAdmin(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None:
            return False
        if getattr(user, "role", None) not in {"owner", "manager", "staff"}:
            return False
        return get_admin_by_identifier(identifier=getattr(user, "id", None)) is not None
