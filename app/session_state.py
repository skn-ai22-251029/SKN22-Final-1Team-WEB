from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import HttpRequest

from app.services.model_team_bridge import (
    get_admin_by_identifier,
    get_admin_by_legacy_id,
    get_client_by_identifier,
    get_client_by_legacy_id,
    get_designer_by_identifier,
    get_designer_by_legacy_id,
    get_legacy_admin_id,
    get_legacy_client_id,
    get_legacy_designer_id,
)
from app.services.runtime_client import RuntimeClient as Client

if TYPE_CHECKING:
    from app.models_django import AdminAccount, Designer


CUSTOMER_ID_SESSION_KEY = "customer_id"
CUSTOMER_LEGACY_ID_SESSION_KEY = "customer_legacy_id"
CUSTOMER_NAME_SESSION_KEY = "customer_name"
ADMIN_ID_SESSION_KEY = "admin_id"
ADMIN_LEGACY_ID_SESSION_KEY = "admin_legacy_id"
ADMIN_STORE_NAME_SESSION_KEY = "admin_store_name"
ADMIN_NAME_SESSION_KEY = "admin_name"
DESIGNER_ID_SESSION_KEY = "designer_id"
DESIGNER_LEGACY_ID_SESSION_KEY = "designer_legacy_id"
DESIGNER_NAME_SESSION_KEY = "designer_name"
OWNER_DASHBOARD_ALLOWED_SESSION_KEY = "owner_dashboard_allowed"
DESIGNER_DASHBOARD_ALLOWED_SESSION_KEY = "designer_dashboard_allowed"


def _update_session_if_changed(request: HttpRequest, updates: dict) -> None:
    if any(request.session.get(k) != v for k, v in updates.items()):
        for k, v in updates.items():
            request.session[k] = v
        request.session.modified = True


def set_customer_session(*, request: HttpRequest, client: Client) -> None:
    request.session[CUSTOMER_ID_SESSION_KEY] = client.id
    request.session[CUSTOMER_LEGACY_ID_SESSION_KEY] = get_legacy_client_id(client=client)
    request.session[CUSTOMER_NAME_SESSION_KEY] = client.name
    request.session.modified = True


def clear_customer_session(*, request: HttpRequest) -> None:
    request.session.pop(CUSTOMER_ID_SESSION_KEY, None)
    request.session.pop(CUSTOMER_LEGACY_ID_SESSION_KEY, None)
    request.session.pop(CUSTOMER_NAME_SESSION_KEY, None)
    request.session[OWNER_DASHBOARD_ALLOWED_SESSION_KEY] = False
    request.session[DESIGNER_DASHBOARD_ALLOWED_SESSION_KEY] = False
    request.session.modified = True


def has_customer_session(*, request: HttpRequest) -> bool:
    return bool(
        request.session.get(CUSTOMER_ID_SESSION_KEY)
        or request.session.get(CUSTOMER_LEGACY_ID_SESSION_KEY)
    )


def get_session_customer(*, request: HttpRequest) -> Client | None:
    legacy_client_id = request.session.get(CUSTOMER_LEGACY_ID_SESSION_KEY)
    if legacy_client_id:
        client = get_client_by_legacy_id(legacy_client_id=legacy_client_id)
        if client is not None:
            _update_session_if_changed(request, {
                CUSTOMER_ID_SESSION_KEY: client.id,
                CUSTOMER_LEGACY_ID_SESSION_KEY: get_legacy_client_id(client=client),
                CUSTOMER_NAME_SESSION_KEY: client.name,
            })
            return client

    client_id = request.session.get(CUSTOMER_ID_SESSION_KEY)
    if client_id:
        client = get_client_by_identifier(identifier=client_id)
        if client is not None:
            _update_session_if_changed(request, {
                CUSTOMER_LEGACY_ID_SESSION_KEY: get_legacy_client_id(client=client),
                CUSTOMER_NAME_SESSION_KEY: client.name,
            })
            return client
    return None


def set_admin_session(*, request: HttpRequest, admin: AdminAccount) -> None:
    request.session[ADMIN_ID_SESSION_KEY] = admin.id
    request.session[ADMIN_LEGACY_ID_SESSION_KEY] = get_legacy_admin_id(admin=admin)
    request.session[ADMIN_STORE_NAME_SESSION_KEY] = admin.store_name
    request.session[ADMIN_NAME_SESSION_KEY] = admin.name
    request.session[OWNER_DASHBOARD_ALLOWED_SESSION_KEY] = False
    
    # 매장 세션은 24시간 유지
    request.session.set_expiry(24 * 60 * 60)
    request.session.modified = True


def clear_admin_session(*, request: HttpRequest) -> None:
    request.session.pop(ADMIN_ID_SESSION_KEY, None)
    request.session.pop(ADMIN_LEGACY_ID_SESSION_KEY, None)
    request.session.pop(ADMIN_STORE_NAME_SESSION_KEY, None)
    request.session.pop(ADMIN_NAME_SESSION_KEY, None)
    request.session.pop(OWNER_DASHBOARD_ALLOWED_SESSION_KEY, None)
    request.session.modified = True


def has_admin_session(*, request: HttpRequest) -> bool:
    return bool(
        request.session.get(ADMIN_ID_SESSION_KEY)
        or request.session.get(ADMIN_LEGACY_ID_SESSION_KEY)
    )


def get_session_admin(*, request: HttpRequest) -> AdminAccount | None:
    legacy_admin_id = request.session.get(ADMIN_LEGACY_ID_SESSION_KEY)
    if legacy_admin_id:
        admin = get_admin_by_legacy_id(legacy_admin_id=legacy_admin_id)
        if admin is not None:
            _update_session_if_changed(request, {
                ADMIN_ID_SESSION_KEY: admin.id,
                ADMIN_LEGACY_ID_SESSION_KEY: get_legacy_admin_id(admin=admin),
                ADMIN_STORE_NAME_SESSION_KEY: admin.store_name,
                ADMIN_NAME_SESSION_KEY: admin.name,
            })
            return admin

    admin_id = request.session.get(ADMIN_ID_SESSION_KEY)
    if admin_id:
        admin = get_admin_by_identifier(identifier=admin_id)
        if admin is not None:
            _update_session_if_changed(request, {
                ADMIN_LEGACY_ID_SESSION_KEY: get_legacy_admin_id(admin=admin),
                ADMIN_STORE_NAME_SESSION_KEY: admin.store_name,
                ADMIN_NAME_SESSION_KEY: admin.name,
            })
            return admin
    return None


def set_designer_session(*, request: HttpRequest, designer: Designer) -> None:
    request.session[DESIGNER_ID_SESSION_KEY] = designer.id
    request.session[DESIGNER_LEGACY_ID_SESSION_KEY] = get_legacy_designer_id(designer=designer)
    request.session[DESIGNER_NAME_SESSION_KEY] = designer.name
    request.session[OWNER_DASHBOARD_ALLOWED_SESSION_KEY] = False
    request.session[DESIGNER_DASHBOARD_ALLOWED_SESSION_KEY] = True
    
    # 디자이너 인증 세션은 30분 유지
    request.session.set_expiry(30 * 60)
    request.session.modified = True


def clear_designer_session(*, request: HttpRequest) -> None:
    request.session.pop(DESIGNER_ID_SESSION_KEY, None)
    request.session.pop(DESIGNER_LEGACY_ID_SESSION_KEY, None)
    request.session.pop(DESIGNER_NAME_SESSION_KEY, None)
    request.session[OWNER_DASHBOARD_ALLOWED_SESSION_KEY] = False
    request.session[DESIGNER_DASHBOARD_ALLOWED_SESSION_KEY] = False
    request.session.modified = True


def has_designer_session(*, request: HttpRequest) -> bool:
    return bool(
        request.session.get(DESIGNER_ID_SESSION_KEY)
        or request.session.get(DESIGNER_LEGACY_ID_SESSION_KEY)
    )


def get_session_designer(*, request: HttpRequest) -> Designer | None:
    legacy_designer_id = request.session.get(DESIGNER_LEGACY_ID_SESSION_KEY)
    if legacy_designer_id:
        designer = get_designer_by_legacy_id(legacy_designer_id=legacy_designer_id)
        if designer is not None:
            _update_session_if_changed(request, {
                DESIGNER_ID_SESSION_KEY: designer.id,
                DESIGNER_LEGACY_ID_SESSION_KEY: get_legacy_designer_id(designer=designer),
                DESIGNER_NAME_SESSION_KEY: designer.name,
            })
            return designer

    designer_id = request.session.get(DESIGNER_ID_SESSION_KEY)
    if designer_id:
        designer = get_designer_by_identifier(identifier=designer_id)
        if designer is not None:
            _update_session_if_changed(request, {
                DESIGNER_LEGACY_ID_SESSION_KEY: get_legacy_designer_id(designer=designer),
                DESIGNER_NAME_SESSION_KEY: designer.name,
            })
            return designer
    return None


def allow_owner_dashboard(*, request: HttpRequest) -> None:
    request.session[OWNER_DASHBOARD_ALLOWED_SESSION_KEY] = True
    request.session.modified = True


def revoke_owner_dashboard(*, request: HttpRequest) -> None:
    request.session[OWNER_DASHBOARD_ALLOWED_SESSION_KEY] = False
    request.session.modified = True


def can_access_owner_dashboard(*, request: HttpRequest) -> bool:
    return bool(request.session.get(OWNER_DASHBOARD_ALLOWED_SESSION_KEY))


def allow_designer_dashboard(*, request: HttpRequest) -> None:
    request.session[DESIGNER_DASHBOARD_ALLOWED_SESSION_KEY] = True
    request.session.modified = True


def revoke_designer_dashboard(*, request: HttpRequest) -> None:
    request.session[DESIGNER_DASHBOARD_ALLOWED_SESSION_KEY] = False
    request.session.modified = True


def can_access_designer_dashboard(*, request: HttpRequest) -> bool:
    return bool(request.session.get(DESIGNER_DASHBOARD_ALLOWED_SESSION_KEY))


def clear_all_sessions(*, request: HttpRequest) -> None:
    """모든 세션 데이터를 삭제하고 초기화합니다."""
    clear_customer_session(request=request)
    clear_admin_session(request=request)
    clear_designer_session(request=request)
    request.session.flush()
