from __future__ import annotations

import json
from functools import lru_cache
from types import SimpleNamespace
from typing import TYPE_CHECKING, Iterable

from django.contrib.auth.hashers import make_password
from django.db import DataError, connection, transaction
from django.db.models import Count, Max, Q
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from app.models_model_team import (
    LegacyClient,
    LegacyClientAnalysis,
    LegacyClientResult,
    LegacyClientResultDetail,
    LegacyClientSurvey,
    LegacyDesigner,
    LegacyHairstyle,
    LegacyShop,
)
from app.services.age_profile import build_age_profile
from app.services.storage_service import resolve_storage_reference
from app.services.legacy_model_sync import (
    _existing_legacy_tables,
    _legacy_gender,
    _legacy_uuid,
    sync_legacy_model_tables_if_present,
)

if TYPE_CHECKING:
    from app.models_django import (
        AdminAccount,
        CaptureRecord,
        Client,
        ConsultationRequest,
        Designer,
        FaceAnalysis,
        FormerRecommendation,
        Style,
        StyleSelection,
        Survey,
    )


LEGACY_SHOP_MODEL_COLUMNS = {
    "shop_id", "login_id", "shop_name", "biz_number", "owner_phone", "password",
    "admin_pin", "created_at", "updated_at", "backend_admin_id", "name",
    "store_name", "role", "phone", "business_number", "password_hash",
    "is_active", "consent_snapshot", "consented_at",
}
LEGACY_DESIGNER_MODEL_COLUMNS = {
    "designer_id", "shop_id", "designer_name", "login_id", "password", "is_active",
    "created_at", "updated_at", "backend_designer_id", "backend_shop_ref_id",
    "name", "phone", "pin_hash",
}
LEGACY_CLIENT_MODEL_COLUMNS = {
    "client_id", "shop_id", "client_name", "phone", "gender", "created_at", "updated_at",
    "backend_client_id", "backend_shop_ref_id", "backend_designer_ref_id", "name",
    "assigned_at", "assignment_source", "age_input", "birth_year_estimate",
}
LEGACY_SURVEY_MODEL_COLUMNS = {
    "survey_id", "client_id", "hair_length", "hair_mood", "hair_condition", "hair_color",
    "budget", "preference_vector", "updated_at", "backend_survey_id", "backend_client_ref_id",
    "target_length", "target_vibe", "scalp_type", "hair_colour", "budget_range",
    "preference_vector_json", "created_at_ts",
}
LEGACY_SURVEY_METADATA_COLUMNS = {
    "question_answers", "survey_profile", "gender_branch",
}
LEGACY_ANALYSIS_MODEL_COLUMNS = {
    "analysis_id", "client_id", "designer_id", "original_image_url", "face_type",
    "face_ratio_vector", "golden_ratio_score", "landmark_data", "created_at",
    "backend_analysis_id", "backend_client_ref_id", "backend_designer_ref_id",
    "backend_capture_record_id", "processed_path", "filename", "status", "face_count",
    "error_note", "updated_at_ts", "deidentified_path", "capture_landmark_snapshot",
    "privacy_snapshot", "analysis_image_url", "analysis_landmark_snapshot",
}
LEGACY_RESULT_MODEL_COLUMNS = {
    "result_id", "analysis_id", "client_id", "selected_hairstyle_id", "selected_image_url",
    "is_confirmed", "created_at", "updated_at", "backend_selection_id", "backend_consultation_id",
    "backend_client_ref_id", "backend_admin_ref_id", "backend_designer_ref_id", "source",
    "survey_snapshot", "analysis_data_snapshot", "status", "is_active", "is_read",
    "closed_at", "selected_recommendation_id",
}
LEGACY_RESULT_DETAIL_MODEL_COLUMNS = {
    "detail_id", "result_id", "hairstyle_id", "rank", "similarity_score", "final_score",
    "simulated_image_url", "recommendation_reason", "backend_recommendation_id",
    "backend_client_ref_id", "backend_capture_record_id", "batch_id", "source",
    "style_name_snapshot", "style_description_snapshot", "keywords_json", "sample_image_url",
    "regeneration_snapshot", "reasoning_snapshot", "is_chosen", "chosen_at",
    "is_sent_to_admin", "sent_at", "created_at_ts",
}
LEGACY_HAIRSTYLE_MODEL_COLUMNS = {
    "hairstyle_id", "chroma_id", "style_name", "image_url", "created_at",
    "backend_style_id", "name", "vibe", "description",
}

LEGACY_ANALYSIS_ONLY_FIELDS = (
    "analysis_id",
    "backend_analysis_id",
    "original_image_url",
    "processed_path",
    "analysis_image_url",
    "face_type",
    "golden_ratio_score",
    "landmark_data",
    "analysis_landmark_snapshot",
    "created_at",
    "updated_at_ts",
)

LEGACY_CAPTURE_ONLY_FIELDS = (
    "analysis_id",
    "backend_capture_record_id",
    "original_image_url",
    "processed_path",
    "filename",
    "status",
    "face_count",
    "capture_landmark_snapshot",
    "deidentified_path",
    "privacy_snapshot",
    "error_note",
    "created_at",
    "updated_at_ts",
)

LEGACY_ANALYSIS_CAPTURE_FIELDS = tuple(
    dict.fromkeys((*LEGACY_ANALYSIS_ONLY_FIELDS, *LEGACY_CAPTURE_ONLY_FIELDS))
)


def _normalize_phone(value: str | None) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def _normalize_person_name(value: str | None) -> str:
    return "".join(str(value or "").split()).casefold()


@lru_cache(maxsize=None)
def _table_columns(table_name: str) -> frozenset[str]:
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table_name)
    return frozenset(column.name for column in description)


def _has_table(table_name: str) -> bool:
    return table_name in _existing_legacy_tables()


def _has_columns(table_name: str, required: set[str]) -> bool:
    if not _has_table(table_name):
        return False
    return required.issubset(_table_columns(table_name))


def has_legacy_shop_source() -> bool:
    return _has_columns("shop", LEGACY_SHOP_MODEL_COLUMNS)


def has_legacy_client_source() -> bool:
    return _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS)


def has_legacy_survey_source() -> bool:
    return _has_columns("client_survey", LEGACY_SURVEY_MODEL_COLUMNS)


def has_legacy_survey_metadata() -> bool:
    return _has_columns("client_survey", LEGACY_SURVEY_METADATA_COLUMNS)


def has_legacy_analysis_source() -> bool:
    return _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS)


def has_legacy_result_source() -> bool:
    return _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS) and _has_columns(
        "client_result_detail",
        LEGACY_RESULT_DETAIL_MODEL_COLUMNS,
    )


def _fetch_one_dict(sql: str, params: Iterable | None = None) -> dict | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, list(params or []))
        if not cursor.description:
            return None
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [column[0] for column in cursor.description]
        return dict(zip(columns, row))


def _fetch_all_dicts(sql: str, params: Iterable | None = None) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, list(params or []))
        if not cursor.description:
            return []
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_admin_by_phone(*, phone: str) -> AdminAccount | None:
    normalized_phone = _normalize_phone(phone)
    if not normalized_phone:
        return None

    if _has_columns("shop", LEGACY_SHOP_MODEL_COLUMNS):
        legacy_shop = (
            LegacyShop.objects.filter(
                Q(phone=normalized_phone)
                | Q(login_id=normalized_phone)
                | Q(owner_phone=normalized_phone)
            )
            .order_by("-backend_admin_id", "shop_id")
            .first()
        )
        if legacy_shop is not None:
            return _admin_from_legacy_row(legacy_shop)
    return None


def admin_exists_by_phone(*, phone: str) -> bool:
    normalized_phone = _normalize_phone(phone)
    if not normalized_phone:
        return False

    if _has_columns("shop", LEGACY_SHOP_MODEL_COLUMNS):
        if LegacyShop.objects.filter(
            Q(phone=normalized_phone)
            | Q(login_id=normalized_phone)
            | Q(owner_phone=normalized_phone)
        ).exists():
            return True

    return False


def admin_exists_by_business_number(*, business_numbers: Iterable[str]) -> bool:
    candidates = {str(value or "").strip() for value in business_numbers if str(value or "").strip()}
    if not candidates:
        return False

    if _has_columns("shop", LEGACY_SHOP_MODEL_COLUMNS):
        if LegacyShop.objects.filter(
            Q(business_number__in=candidates) | Q(biz_number__in=candidates)
        ).exists():
            return True

    return False


def get_admin_by_legacy_id(*, legacy_admin_id: str | None) -> AdminAccount | None:
    legacy_admin_id = str(legacy_admin_id or "").strip()
    if not legacy_admin_id:
        return None

    if _has_columns("shop", LEGACY_SHOP_MODEL_COLUMNS):
        try:
            legacy_shop = LegacyShop.objects.filter(shop_id=legacy_admin_id).first()
        except DataError:
            return None
        if legacy_shop is not None:
            return _admin_from_legacy_row(legacy_shop)
    return None


def get_admin_by_identifier(*, identifier: str | int | None) -> AdminAccount | None:
    if identifier in (None, ""):
        return None
    text = str(identifier).strip()
    if text.isdigit():
        if _has_columns("shop", LEGACY_SHOP_MODEL_COLUMNS):
            legacy_shop = LegacyShop.objects.filter(backend_admin_id=int(text)).first()
            if legacy_shop is not None:
                return _admin_from_legacy_row(legacy_shop)
    legacy_first = get_admin_by_legacy_id(legacy_admin_id=text)
    if legacy_first is not None:
        return legacy_first
    return None


def get_client_by_phone(*, phone: str) -> Client | None:
    normalized_phone = _normalize_phone(phone)
    if not normalized_phone:
        return None

    if _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS):
        legacy_client = (
            LegacyClient.objects.filter(phone=normalized_phone)
            .order_by("-backend_client_id", "client_id")
            .first()
        )
        if legacy_client is not None:
            return _client_from_legacy_row(legacy_client)
    return None


def get_client_by_legacy_id(*, legacy_client_id: str | None) -> Client | None:
    legacy_client_id = str(legacy_client_id or "").strip()
    if not legacy_client_id:
        return None

    if _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS):
        try:
            legacy_client = LegacyClient.objects.filter(client_id=legacy_client_id).first()
        except DataError:
            return None
        if legacy_client is not None:
            return _client_from_legacy_row(legacy_client)
    return None


def get_client_by_identifier(*, identifier: str | int | None) -> Client | None:
    if identifier in (None, ""):
        return None
    text = str(identifier).strip()
    if text.isdigit():
        if _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS):
            legacy_client = LegacyClient.objects.filter(backend_client_id=int(text)).first()
            if legacy_client is not None:
                return _client_from_legacy_row(legacy_client)
    return get_client_by_legacy_id(legacy_client_id=text)


def _parse_jsonish(value, fallback=None):
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback if fallback is not None else value


def _coerce_datetime(value):
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value
    text = str(value)
    parsed = parse_datetime(text)
    if parsed is not None:
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
    return value


def _next_backend_ref_id(model, field_name: str) -> int:
    latest = model.objects.aggregate(max_value=Max(field_name)).get("max_value")
    return int(latest or 0) + 1


def _cache_lookup(cache: dict[str, object] | None, *, backend_id=None, legacy_id=None):
    if cache is None:
        return None
    if backend_id not in (None, "", 0):
        cached = cache.get(f"backend:{backend_id}")
        if cached is not None:
            return cached
    if legacy_id not in (None, ""):
        return cache.get(f"legacy:{legacy_id}")
    return None


def _cache_store(cache: dict[str, object] | None, value, *, backend_id=None, legacy_id=None):
    if cache is None or value is None:
        return value
    if backend_id not in (None, "", 0):
        cache[f"backend:{backend_id}"] = value
    if legacy_id not in (None, ""):
        cache[f"legacy:{legacy_id}"] = value
    return value


def _admin_from_legacy_row(row: LegacyShop | None, *, admin_cache: dict[str, object] | None = None):
    if row is None:
        return None
    cached = _cache_lookup(admin_cache, backend_id=row.backend_admin_id, legacy_id=row.shop_id)
    if cached is not None:
        return cached
    runtime_id = int(row.backend_admin_id or 0)
    if runtime_id <= 0:
        runtime_id = _next_backend_ref_id(LegacyShop, "backend_admin_id")
        row.backend_admin_id = runtime_id
        row.save(update_fields=["backend_admin_id"])
    phone = _normalize_phone(row.phone or row.owner_phone or row.login_id)
    admin = SimpleNamespace(
        id=runtime_id,
        legacy_admin_id=row.shop_id,
        name=row.name or row.shop_name,
        store_name=row.store_name or row.shop_name,
        role=row.role or "owner",
        phone=phone,
        business_number=(row.business_number or row.biz_number or ""),
        password_hash=row.password_hash or row.password or "",
        admin_pin=row.admin_pin or "0000",
        is_active=(True if row.is_active is None else bool(row.is_active)),
        consent_snapshot=row.consent_snapshot or {},
        consented_at=row.consented_at,
        created_at=_coerce_datetime(row.created_at) or row.consented_at,
        backend_admin_id=runtime_id,
    )
    return _cache_store(admin_cache, admin, backend_id=runtime_id, legacy_id=row.shop_id)


def _designer_from_legacy_row(
    row: LegacyDesigner | None,
    *,
    admin_cache: dict[str, object] | None = None,
    designer_cache: dict[str, object] | None = None,
):
    if row is None:
        return None
    cached = _cache_lookup(designer_cache, backend_id=row.backend_designer_id, legacy_id=row.designer_id)
    if cached is not None:
        return cached
    runtime_id = int(row.backend_designer_id or 0)
    if runtime_id <= 0:
        runtime_id = _next_backend_ref_id(LegacyDesigner, "backend_designer_id")
        row.backend_designer_id = runtime_id
        row.save(update_fields=["backend_designer_id"])
    admin = _cache_lookup(admin_cache, backend_id=row.backend_shop_ref_id, legacy_id=row.shop_id)
    if admin is None:
        admin = get_admin_by_legacy_id(legacy_admin_id=row.shop_id)
        admin = _cache_store(
            admin_cache,
            admin,
            backend_id=getattr(admin, "id", None),
            legacy_id=getattr(admin, "legacy_admin_id", None),
        )
    designer = SimpleNamespace(
        id=runtime_id,
        legacy_designer_id=row.designer_id,
        shop=admin,
        shop_id=(admin.id if admin is not None else row.backend_shop_ref_id),
        name=row.name or row.designer_name,
        phone=_normalize_phone(row.phone or row.login_id),
        pin_hash=row.pin_hash or row.password or "",
        is_active=bool(row.is_active),
        created_at=_coerce_datetime(row.created_at),
        backend_designer_id=runtime_id,
    )
    return _cache_store(designer_cache, designer, backend_id=runtime_id, legacy_id=row.designer_id)


def _client_from_legacy_row(
    row: LegacyClient | None,
    *,
    admin_cache: dict[str, object] | None = None,
    designer_cache: dict[str, object] | None = None,
):
    if row is None:
        return None
    runtime_id = int(row.backend_client_id or 0)
    if runtime_id <= 0:
        runtime_id = _next_backend_ref_id(LegacyClient, "backend_client_id")
        row.backend_client_id = runtime_id
        row.save(update_fields=["backend_client_id"])
    admin = _cache_lookup(admin_cache, backend_id=row.backend_shop_ref_id, legacy_id=row.shop_id)
    if admin is None:
        admin = get_admin_by_legacy_id(legacy_admin_id=row.shop_id)
        admin = _cache_store(
            admin_cache,
            admin,
            backend_id=getattr(admin, "id", None),
            legacy_id=getattr(admin, "legacy_admin_id", None),
        )
    designer = None
    if row.backend_designer_ref_id:
        designer = _cache_lookup(designer_cache, backend_id=row.backend_designer_ref_id)
        if designer is None:
            designer = get_designer_by_identifier(identifier=row.backend_designer_ref_id)
            designer = _cache_store(
                designer_cache,
                designer,
                backend_id=getattr(designer, "id", None),
                legacy_id=getattr(designer, "legacy_designer_id", None),
            )
    return SimpleNamespace(
        id=runtime_id,
        legacy_client_id=row.client_id,
        shop=admin,
        shop_id=(admin.id if admin is not None else row.backend_shop_ref_id),
        designer=designer,
        designer_id=(designer.id if designer is not None else row.backend_designer_ref_id),
        name=row.name or row.client_name,
        phone=_normalize_phone(row.phone),
        gender=row.gender,
        age_input=row.age_input,
        birth_year_estimate=row.birth_year_estimate,
        assigned_at=row.assigned_at,
        assignment_source=row.assignment_source,
        created_at=_coerce_datetime(row.created_at),
        backend_client_id=runtime_id,
    )


def get_designers_for_admin(*, admin: AdminAccount) -> list[Designer]:
    if _has_columns("designer", LEGACY_DESIGNER_MODEL_COLUMNS):
        backend_admin_id = get_backend_admin_id(admin=admin)
        legacy_id = get_legacy_admin_id(admin=admin)
        scope_filter = Q()
        if legacy_id:
            scope_filter |= Q(shop_id=legacy_id)
        if backend_admin_id is not None:
            scope_filter |= Q(backend_shop_ref_id=backend_admin_id)
        if not scope_filter:
            return []

        rows = list(
            LegacyDesigner.objects.filter(scope_filter, is_active=True)
            .order_by("backend_designer_id", "designer_id")
        )
        resolved_admin = (
            get_admin_by_identifier(identifier=backend_admin_id)
            if backend_admin_id is not None
            else None
        ) or admin
        admin_cache: dict[str, object] = {}
        resolved_backend_admin_id = get_backend_admin_id(admin=resolved_admin)
        if resolved_backend_admin_id is not None:
            admin_cache[f"backend:{resolved_backend_admin_id}"] = resolved_admin
        if legacy_id:
            admin_cache[f"legacy:{legacy_id}"] = resolved_admin
        return [
            designer
            for designer in (
                _designer_from_legacy_row(row, admin_cache=admin_cache)
                for row in rows
            )
            if designer is not None
        ]
    return []


def get_designer_by_legacy_id(*, legacy_designer_id: str | None) -> Designer | None:
    legacy_designer_id = str(legacy_designer_id or "").strip()
    if not legacy_designer_id:
        return None

    if _has_columns("designer", LEGACY_DESIGNER_MODEL_COLUMNS):
        try:
            legacy_designer = LegacyDesigner.objects.filter(designer_id=legacy_designer_id).first()
        except DataError:
            return None
        if legacy_designer is not None:
            return _designer_from_legacy_row(legacy_designer)
    return None


def get_designer_by_identifier(*, identifier: str | int | None) -> Designer | None:
    if identifier in (None, ""):
        return None
    text = str(identifier).strip()
    if text.isdigit():
        if _has_columns("designer", LEGACY_DESIGNER_MODEL_COLUMNS):
            legacy_designer = LegacyDesigner.objects.filter(backend_designer_id=int(text), is_active=True).first()
            if legacy_designer is not None:
                return _designer_from_legacy_row(legacy_designer)
    return get_designer_by_legacy_id(legacy_designer_id=text)


def get_backend_admin_id(*, admin: AdminAccount | None) -> int | None:
    if admin is None:
        return None

    for attr_name in ("backend_admin_id", "id"):
        value = getattr(admin, attr_name, None)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def get_backend_designer_id(*, designer: Designer | None) -> int | None:
    if designer is None:
        return None

    for attr_name in ("backend_designer_id", "id"):
        value = getattr(designer, attr_name, None)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def get_designer_for_admin(*, admin: AdminAccount, designer_id: int | str) -> Designer | None:
    backend_admin_id = get_backend_admin_id(admin=admin)
    legacy_admin_id = get_legacy_admin_id(admin=admin)

    try:
        designer_id = int(designer_id)
    except (TypeError, ValueError):
        designer = get_designer_by_legacy_id(legacy_designer_id=str(designer_id))
        if designer is None or not designer.is_active:
            return None
        if backend_admin_id is not None and designer.shop_id == backend_admin_id:
            return designer
        if legacy_admin_id and get_legacy_admin_id(admin=getattr(designer, "shop", None)) == legacy_admin_id:
            return designer
        return None

    if _has_columns("designer", LEGACY_DESIGNER_MODEL_COLUMNS):
        scope_filter = Q()
        if backend_admin_id is not None:
            scope_filter |= Q(backend_shop_ref_id=backend_admin_id)
        if legacy_admin_id:
            scope_filter |= Q(shop_id=legacy_admin_id)
        if not scope_filter:
            return None

        legacy_designer = (
            LegacyDesigner.objects.filter(
                scope_filter,
                backend_designer_id=designer_id,
                is_active=True,
            )
            .first()
        )
        if legacy_designer is not None:
            return _designer_from_legacy_row(legacy_designer)
    return None


def get_legacy_admin_id(*, admin: AdminAccount | None) -> str | None:
    if admin is None:
        return None
    explicit = getattr(admin, "legacy_admin_id", None)
    if explicit:
        return str(explicit)
    return str(_legacy_uuid("shop", admin.id))


def get_legacy_designer_id(*, designer: Designer | None) -> str | None:
    if designer is None:
        return None
    explicit = getattr(designer, "legacy_designer_id", None)
    if explicit:
        return str(explicit)
    return str(_legacy_uuid("designer", designer.id))


def get_legacy_client_id(*, client: Client | None) -> str | None:
    if client is None:
        return None
    explicit = getattr(client, "legacy_client_id", None)
    if explicit:
        return str(explicit)
    return str(_legacy_uuid("client", client.id))


def _ensure_capture_fallback_designer(*, client: Client):
    if not _has_columns("designer", LEGACY_DESIGNER_MODEL_COLUMNS):
        return None

    admin = getattr(client, "shop", None)
    backend_shop_ref_id = getattr(client, "shop_id", None) or getattr(admin, "id", None)
    legacy_shop_id = getattr(admin, "legacy_admin_id", None)
    if admin is None and backend_shop_ref_id:
        admin = get_admin_by_identifier(identifier=backend_shop_ref_id)
        legacy_shop_id = getattr(admin, "legacy_admin_id", None)
    if not backend_shop_ref_id or not legacy_shop_id:
        return None

    legacy_designer_id = str(_legacy_uuid("capture_unassigned_designer", int(backend_shop_ref_id)))
    row = LegacyDesigner.objects.filter(designer_id=legacy_designer_id).first()
    if row is None:
        created_at = timezone.now().isoformat()
        row = LegacyDesigner.objects.create(
            designer_id=legacy_designer_id,
            shop_id=legacy_shop_id,
            designer_name="Unassigned Capture",
            login_id=f"capture-unassigned-{backend_shop_ref_id}",
            password="",
            is_active=False,
            created_at=created_at,
            updated_at=created_at,
            backend_designer_id=None,
            backend_shop_ref_id=backend_shop_ref_id,
            name="Unassigned Capture",
            phone="",
            pin_hash="",
        )

    return SimpleNamespace(
        id=None,
        legacy_designer_id=row.designer_id,
        shop=admin,
        shop_id=backend_shop_ref_id,
        name=row.name or row.designer_name,
        phone=row.phone or "",
        pin_hash=row.pin_hash or row.password or "",
        is_active=False,
        backend_designer_id=None,
    )


def _resolve_capture_designer(*, client: Client):
    designer = getattr(client, "designer", None)
    if designer is not None:
        return designer
    return _ensure_capture_fallback_designer(client=client)


def get_scoped_client_ids(
    *,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
) -> list[int] | None:
    if not _has_table("client"):
        return None

    columns = _table_columns("client")
    required = {"backend_client_id", "backend_shop_ref_id", "backend_designer_ref_id"}
    if not required.issubset(columns):
        return None

    if designer is not None:
        backend_designer_id = get_backend_designer_id(designer=designer)
        if backend_designer_id is None:
            return []
        rows = LegacyClient.objects.filter(backend_designer_ref_id=backend_designer_id).order_by("backend_client_id")
        return [int(row.backend_client_id) for row in rows if row.backend_client_id]

    if admin is not None:
        backend_admin_id = get_backend_admin_id(admin=admin)
        legacy_admin_id = get_legacy_admin_id(admin=admin)
        scope_filter = Q()
        if backend_admin_id is not None:
            scope_filter |= Q(backend_shop_ref_id=backend_admin_id)
        if legacy_admin_id:
            scope_filter |= Q(shop_id=legacy_admin_id)
        if not scope_filter:
            return []
        rows = LegacyClient.objects.filter(scope_filter).order_by("backend_client_id")
        return [int(row.backend_client_id) for row in rows if row.backend_client_id]

    return None


def create_admin_record(*, name: str, store_name: str, role: str, phone: str, business_number: str, password_hash: str, consent_snapshot: dict | None = None, consented_at=None):
    if not _has_columns("shop", LEGACY_SHOP_MODEL_COLUMNS):
        raise RuntimeError("Legacy shop table is required.")
    created_at = timezone.now()
    backend_admin_id = _next_backend_ref_id(LegacyShop, "backend_admin_id")
    legacy_admin_id = str(_legacy_uuid("shop", backend_admin_id))
    row = LegacyShop.objects.create(
        shop_id=legacy_admin_id,
        login_id=_normalize_phone(phone),
        shop_name=store_name,
        biz_number=business_number,
        owner_phone=_normalize_phone(phone),
        password=password_hash,
        admin_pin=make_password("0000"),
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
        backend_admin_id=backend_admin_id,
        name=name,
        store_name=store_name,
        role=role,
        phone=_normalize_phone(phone),
        business_number=business_number,
        password_hash=password_hash,
        is_active=True,
        consent_snapshot=consent_snapshot or {},
        consented_at=consented_at or created_at,
    )
    return _admin_from_legacy_row(row)


def create_designer_record(*, admin, name: str, phone: str, pin_hash: str):
    if not _has_columns("designer", LEGACY_DESIGNER_MODEL_COLUMNS):
        raise RuntimeError("Legacy designer table is required.")
    created_at = timezone.now()
    backend_designer_id = _next_backend_ref_id(LegacyDesigner, "backend_designer_id")
    legacy_designer_id = str(_legacy_uuid("designer", backend_designer_id))
    row = LegacyDesigner.objects.create(
        designer_id=legacy_designer_id,
        shop_id=get_legacy_admin_id(admin=admin) or "",
        designer_name=name,
        login_id=_normalize_phone(phone),
        password=pin_hash,
        is_active=True,
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
        backend_designer_id=backend_designer_id,
        backend_shop_ref_id=admin.id,
        name=name,
        phone=_normalize_phone(phone),
        pin_hash=pin_hash,
    )
    return _designer_from_legacy_row(row)


def upsert_client_record(*, phone: str, name: str, gender: str | None = None, age_input: int | None = None, birth_year_estimate: int | None = None, shop=None, designer=None, assignment_source: str | None = None):
    if not _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS):
        raise RuntimeError("Legacy client table is required.")
    normalized_phone = _normalize_phone(phone)
    normalized_name = _normalize_person_name(name)
    created_at = timezone.now()
    row = None
    candidate_queryset = LegacyClient.objects.filter(phone=normalized_phone)
    backend_shop_id = getattr(shop, "id", None)
    legacy_shop_id = get_legacy_admin_id(admin=shop) if shop is not None else None
    if backend_shop_id is not None or legacy_shop_id:
        shop_scope = Q()
        if backend_shop_id is not None:
            shop_scope |= Q(backend_shop_ref_id=backend_shop_id)
        if legacy_shop_id:
            shop_scope |= Q(shop_id=legacy_shop_id)
        candidate_queryset = candidate_queryset.filter(shop_scope)

    for candidate in candidate_queryset.order_by("-backend_client_id", "client_id"):
        candidate_name = _normalize_person_name(getattr(candidate, "name", None) or getattr(candidate, "client_name", None))
        if candidate_name == normalized_name:
            row = candidate
            break
    if row is None:
        backend_client_id = _next_backend_ref_id(LegacyClient, "backend_client_id")
        legacy_client_id = str(_legacy_uuid("client", backend_client_id))
        row = LegacyClient(
            client_id=legacy_client_id,
            created_at=created_at.isoformat(),
            backend_client_id=backend_client_id,
        )
    row.shop_id = get_legacy_admin_id(admin=shop) or getattr(row, "shop_id", "") or ""
    row.client_name = name
    row.phone = normalized_phone
    row.gender = _legacy_gender(gender or getattr(row, "gender", "") or "")
    row.updated_at = created_at.isoformat()
    row.backend_shop_ref_id = getattr(shop, "id", None)
    row.backend_designer_ref_id = getattr(designer, "id", None)
    row.name = name
    next_assignment_source = assignment_source or getattr(row, "assignment_source", None)
    if next_assignment_source == "shop_manual_assignment_pending":
        row.assigned_at = None
    elif designer is not None or next_assignment_source in {
        "designer_session",
        "auto_single_designer",
        "auto_shop_only",
        "shop_manual_assignment",
    }:
        row.assigned_at = created_at
    row.assignment_source = next_assignment_source
    row.age_input = age_input
    row.birth_year_estimate = birth_year_estimate
    row.save()
    return _client_from_legacy_row(row)


def update_designer_active_state(*, designer, is_active: bool):
    legacy_designer_id = get_legacy_designer_id(designer=designer)
    row = LegacyDesigner.objects.filter(designer_id=legacy_designer_id).first()
    if row is None:
        raise ValueError("Designer could not be found.")
    row.is_active = bool(is_active)
    row.updated_at = timezone.now().isoformat()
    row.save(update_fields=["is_active", "updated_at"])
    return _designer_from_legacy_row(row)


def get_latest_legacy_survey(*, client: Client):
    if not _has_columns("client_survey", LEGACY_SURVEY_MODEL_COLUMNS):
        return None

    legacy_client_id = str(_legacy_uuid("client", client.id))
    row = (
        LegacyClientSurvey.objects.filter(
            Q(backend_client_ref_id=client.id) | Q(client_id=legacy_client_id)
        )
        .order_by("-backend_survey_id", "-survey_id")
        .first()
    )
    if not row:
        return None

    metadata = get_legacy_survey_metadata(survey_id=row.survey_id)

    return SimpleNamespace(
        id=row.backend_survey_id or row.survey_id,
        client=client,
        client_id=client.id,
        target_length=row.target_length or row.hair_length,
        target_vibe=row.target_vibe or row.hair_mood,
        scalp_type=row.scalp_type or row.hair_condition,
        hair_colour=row.hair_colour or row.hair_color,
        budget_range=row.budget_range or row.budget,
        preference_vector=_parse_jsonish(
            row.preference_vector_json if row.preference_vector_json is not None else row.preference_vector,
            fallback=[],
        ),
        question_answers=dict(metadata.get("question_answers") or {}),
        survey_profile=dict(metadata.get("survey_profile") or {}),
        gender_branch=metadata.get("gender_branch"),
        created_at=_coerce_datetime(row.created_at_ts or row.updated_at),
    )


def get_legacy_survey_metadata(*, survey_id: int) -> dict:
    if not has_legacy_survey_metadata():
        return {}

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT question_answers, survey_profile, gender_branch
            FROM client_survey
            WHERE survey_id = %s
            """,
            [survey_id],
        )
        row = cursor.fetchone()

    if not row:
        return {}
    return {
        "question_answers": _parse_jsonish(row[0], fallback={}),
        "survey_profile": _parse_jsonish(row[1], fallback={}),
        "gender_branch": (str(row[2]).strip() if row[2] not in (None, "") else None),
    }


def update_legacy_survey_metadata(
    *,
    survey_id: int,
    question_answers: dict | None,
    survey_profile: dict | None,
    gender_branch: str | None,
) -> None:
    if not has_legacy_survey_metadata():
        return

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE client_survey
            SET question_answers = %s,
                survey_profile = %s,
                gender_branch = %s
            WHERE survey_id = %s
            """,
            [
                json.dumps(dict(question_answers or {}), ensure_ascii=False),
                json.dumps(dict(survey_profile or {}), ensure_ascii=False),
                (str(gender_branch).strip() if gender_branch else None),
                survey_id,
            ],
        )


def _legacy_client_q(*, client: Client) -> Q:
    legacy_client_id = str(_legacy_uuid("client", client.id))
    return Q(backend_client_ref_id=client.id) | Q(client_id=legacy_client_id)


def _legacy_result_queryset(
    *,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
    client: Client | None = None,
):
    queryset = LegacyClientResult.objects.all()
    if client is not None:
        queryset = queryset.filter(_legacy_client_q(client=client))
    if admin is not None:
        backend_admin_id = get_backend_admin_id(admin=admin)
        legacy_admin_id = get_legacy_admin_id(admin=admin)
        client_scope = Q()
        if backend_admin_id is not None:
            client_scope |= Q(backend_shop_ref_id=backend_admin_id)
        if legacy_admin_id:
            client_scope |= Q(shop_id=legacy_admin_id)

        admin_filter = Q()
        if backend_admin_id is not None:
            admin_filter |= Q(backend_admin_ref_id=backend_admin_id)
        if client_scope:
            scoped_clients = LegacyClient.objects.filter(client_scope)
            admin_filter |= Q(backend_client_ref_id__in=scoped_clients.values("backend_client_id"))
            admin_filter |= Q(client_id__in=scoped_clients.values("client_id"))
        if admin_filter:
            queryset = queryset.filter(admin_filter)
    if designer is not None:
        backend_designer_id = get_backend_designer_id(designer=designer)
        legacy_designer_id = get_legacy_designer_id(designer=designer)
        designer_filter = Q()
        if backend_designer_id is not None:
            scoped_clients = LegacyClient.objects.filter(backend_designer_ref_id=backend_designer_id)
            designer_filter |= Q(backend_designer_ref_id=backend_designer_id)
            designer_filter |= Q(backend_client_ref_id__in=scoped_clients.values("backend_client_id"))
            designer_filter |= Q(client_id__in=scoped_clients.values("client_id"))
        if legacy_designer_id:
            designer_filter |= Q(
                analysis_id__in=LegacyClientAnalysis.objects.filter(designer_id=legacy_designer_id).values("analysis_id")
            )
        if designer_filter:
            queryset = queryset.filter(designer_filter)
    return queryset


def _style_from_legacy(style_id: int):
    if _has_columns("hairstyle", LEGACY_HAIRSTYLE_MODEL_COLUMNS):
        style = (
            LegacyHairstyle.objects.filter(
                Q(hairstyle_id=style_id) | Q(backend_style_id=style_id)
            )
            .order_by("-backend_style_id", "-hairstyle_id")
            .first()
        )
        if style is not None:
            return style
    return None


def get_style_record(*, style_id: int):
    return _style_from_legacy(style_id)


def get_style_record_by_name(*, style_name: str):
    normalized_name = str(style_name or "").strip()
    if not normalized_name:
        return None
    if _has_columns("hairstyle", LEGACY_HAIRSTYLE_MODEL_COLUMNS):
        style = (
            LegacyHairstyle.objects.filter(
                Q(name=normalized_name) | Q(style_name=normalized_name)
            )
            .order_by("-backend_style_id", "-hairstyle_id")
            .first()
        )
        if style is not None:
            return style
    return None


def get_latest_legacy_analysis(*, client: Client):
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return None

    row = (
        LegacyClientAnalysis.objects.filter(_legacy_client_q(client=client))
        .only(*LEGACY_ANALYSIS_ONLY_FIELDS)
        .order_by("-backend_analysis_id", "-analysis_id")
        .first()
    )
    if not row:
        return None
    return _build_legacy_analysis_namespace(row=row, client=client)


def get_latest_legacy_analysis_capture_bundle(*, client: Client) -> tuple[SimpleNamespace | None, SimpleNamespace | None]:
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return None, None

    row = (
        LegacyClientAnalysis.objects.filter(_legacy_client_q(client=client))
        .only(*LEGACY_ANALYSIS_CAPTURE_FIELDS)
        .order_by("-updated_at_ts", "-analysis_id")
        .first()
    )
    if not row:
        return None, None
    return _build_legacy_analysis_namespace(row=row, client=client), _build_legacy_capture_namespace(row=row, client=client)


def get_legacy_analysis_history(*, client: Client, limit: int = 20) -> list[SimpleNamespace]:
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return []

    rows = list(
        LegacyClientAnalysis.objects.filter(_legacy_client_q(client=client))
        .only(*LEGACY_ANALYSIS_ONLY_FIELDS)
        .order_by("-updated_at_ts", "-analysis_id")[: int(limit)]
    )
    return [_build_legacy_analysis_namespace(row=row, client=client) for row in rows]


def get_latest_legacy_capture(*, client: Client):
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return None

    row = (
        LegacyClientAnalysis.objects.filter(_legacy_client_q(client=client))
        .only(*LEGACY_CAPTURE_ONLY_FIELDS)
        .order_by("-updated_at_ts", "-analysis_id")
        .first()
    )
    if not row:
        return None
    return _build_legacy_capture_namespace(row=row, client=client)


def get_legacy_analysis_capture_history(*, client: Client, limit: int = 20) -> tuple[list[SimpleNamespace], list[SimpleNamespace]]:
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return [], []

    rows = list(
        LegacyClientAnalysis.objects.filter(_legacy_client_q(client=client))
        .only(*LEGACY_ANALYSIS_CAPTURE_FIELDS)
        .order_by("-updated_at_ts", "-analysis_id")[: int(limit)]
    )
    analysis_history = [_build_legacy_analysis_namespace(row=row, client=client) for row in rows]
    capture_history = [_build_legacy_capture_namespace(row=row, client=client) for row in rows]
    return analysis_history, capture_history


def _build_legacy_capture_namespace(*, row: LegacyClientAnalysis, client: Client) -> SimpleNamespace:
    image_path = row.processed_path or row.original_image_url
    return SimpleNamespace(
        id=row.backend_capture_record_id or row.analysis_id,
        client=client,
        client_id=client.id,
        original_path=row.original_image_url,
        processed_path=image_path,
        filename=row.filename,
        status=row.status or "DONE",
        face_count=row.face_count or 1,
        landmark_snapshot=_parse_jsonish(row.capture_landmark_snapshot, fallback={}),
        deidentified_path=row.deidentified_path,
        privacy_snapshot=_parse_jsonish(row.privacy_snapshot, fallback={}),
        error_note=row.error_note,
        created_at=_coerce_datetime(row.created_at),
        updated_at=row.updated_at_ts or _coerce_datetime(row.created_at),
    )


def get_legacy_capture_by_identifier(*, identifier: str | int | None):
    text = str(identifier or "").strip()
    if not text:
        return None
    if _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        rows = LegacyClientAnalysis.objects.all()
        if text.isdigit():
            numeric_id = int(text)
            row = rows.filter(
                Q(backend_capture_record_id=numeric_id) | Q(analysis_id=numeric_id)
            ).order_by("-updated_at_ts", "-analysis_id").first()
        else:
            row = rows.filter(client_id=text).order_by("-updated_at_ts", "-analysis_id").first()

        if row is not None:
            client_identifier = row.backend_client_ref_id or row.client_id
            client = get_client_by_identifier(identifier=client_identifier)
            if client is None:
                return None
            return _build_legacy_capture_namespace(row=row, client=client)
    return None


def get_legacy_capture_history(*, client: Client, limit: int = 20) -> list[SimpleNamespace]:
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return []

    rows = list(
        LegacyClientAnalysis.objects.filter(_legacy_client_q(client=client))
        .only(*LEGACY_CAPTURE_ONLY_FIELDS)
        .order_by("-updated_at_ts", "-analysis_id")[: int(limit)]
    )
    history: list[SimpleNamespace] = []
    for row in rows:
        history.append(_build_legacy_capture_namespace(row=row, client=client))
    return history


def get_legacy_analysis_count(*, client: Client) -> int:
    return get_legacy_analysis_capture_count(client=client)


def get_legacy_capture_count(*, client: Client) -> int:
    return get_legacy_analysis_capture_count(client=client)


def get_legacy_analysis_capture_count(*, client: Client) -> int:
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return 0
    return int(LegacyClientAnalysis.objects.filter(_legacy_client_q(client=client)).count())


def create_legacy_capture_upload_record(
    *,
    client: Client,
    original_path: str | None,
    processed_path: str | None,
    filename: str | None,
    status: str,
    face_count: int | None,
    landmark_snapshot: dict | None,
    deidentified_path: str | None,
    privacy_snapshot: dict | None,
    error_note: str | None,
):
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        raise RuntimeError("Legacy client_analysis table is required.")

    now = timezone.now()
    analysis_id = _next_backend_ref_id(LegacyClientAnalysis, "analysis_id")
    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        raise RuntimeError(f"Legacy client id is required for capture upload: client={getattr(client, 'id', None)}")

    capture_designer = _resolve_capture_designer(client=client)
    legacy_designer_id = get_legacy_designer_id(designer=capture_designer)
    if not legacy_designer_id:
        raise RuntimeError(
            f"Legacy designer id is required for capture upload: client={getattr(client, 'id', None)} shop={getattr(client, 'shop_id', None)}"
        )

    row = LegacyClientAnalysis.objects.create(
        analysis_id=analysis_id,
        client_id=legacy_client_id,
        designer_id=legacy_designer_id,
        original_image_url=original_path,
        face_type=None,
        face_ratio_vector=_legacy_preference_vector_storage([]),
        golden_ratio_score=None,
        landmark_data=json.dumps({}, ensure_ascii=False),
        created_at=now.isoformat(),
        backend_analysis_id=None,
        backend_client_ref_id=client.id,
        backend_designer_ref_id=getattr(capture_designer, "id", None) or getattr(client, "designer_id", None),
        backend_capture_record_id=analysis_id,
        processed_path=processed_path,
        filename=filename,
        status=status,
        face_count=face_count,
        error_note=error_note,
        updated_at_ts=now,
        deidentified_path=deidentified_path,
        capture_landmark_snapshot=landmark_snapshot or {},
        privacy_snapshot=privacy_snapshot or {},
        analysis_image_url=None,
        analysis_landmark_snapshot=None,
    )
    return _build_legacy_capture_namespace(row=row, client=client)


def mark_legacy_capture_processing(*, record_id: int):
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return None
    row = (
        LegacyClientAnalysis.objects.filter(
            Q(analysis_id=record_id) | Q(backend_capture_record_id=record_id)
        )
        .order_by("-updated_at_ts", "-analysis_id")
        .first()
    )
    if row is None:
        return None
    if (row.status or "").upper() != "PENDING":
        client = get_client_by_identifier(identifier=row.backend_client_ref_id or row.client_id)
        return _build_legacy_capture_namespace(row=row, client=client) if client is not None else None
    row.status = "PROCESSING"
    row.updated_at_ts = timezone.now()
    row.save(update_fields=["status", "updated_at_ts"])
    client = get_client_by_identifier(identifier=row.backend_client_ref_id or row.client_id)
    return _build_legacy_capture_namespace(row=row, client=client) if client is not None else None


def complete_legacy_capture_analysis(
    *,
    record_id: int,
    face_shape: str | None,
    golden_ratio_score: float | None,
    landmark_snapshot: dict | None,
    analysis_image_url: str | None = None,
):
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return None, None
    row = (
        LegacyClientAnalysis.objects.filter(
            Q(analysis_id=record_id) | Q(backend_capture_record_id=record_id)
        )
        .order_by("-updated_at_ts", "-analysis_id")
        .first()
    )
    if row is None:
        return None, None
    row.status = "DONE"
    row.face_type = face_shape
    row.golden_ratio_score = golden_ratio_score
    row.analysis_image_url = analysis_image_url or row.processed_path or row.original_image_url
    row.analysis_landmark_snapshot = landmark_snapshot or {}
    row.landmark_data = json.dumps(landmark_snapshot or {}, ensure_ascii=False)
    face_ratio_vector = []
    if isinstance(landmark_snapshot, dict):
        candidate = landmark_snapshot.get("face_ratio_vector")
        if isinstance(candidate, list):
            face_ratio_vector = candidate
    row.face_ratio_vector = _legacy_preference_vector_storage(face_ratio_vector)
    row.updated_at_ts = timezone.now()
    row.save(
        update_fields=[
            "status",
            "face_type",
            "golden_ratio_score",
            "analysis_image_url",
            "analysis_landmark_snapshot",
            "landmark_data",
            "face_ratio_vector",
            "updated_at_ts",
        ]
    )
    client = get_client_by_identifier(identifier=row.backend_client_ref_id or row.client_id)
    if client is None:
        return None, None
    capture = _build_legacy_capture_namespace(row=row, client=client)
    analysis = get_latest_legacy_analysis(client=client)
    return capture, analysis


def fail_legacy_capture_processing(*, record_id: int, error_note: str):
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return False
    row = (
        LegacyClientAnalysis.objects.filter(
            Q(analysis_id=record_id) | Q(backend_capture_record_id=record_id)
        )
        .order_by("-updated_at_ts", "-analysis_id")
        .first()
    )
    if row is None:
        return False
    row.status = "FAILED"
    row.error_note = error_note
    row.updated_at_ts = timezone.now()
    row.save(update_fields=["status", "error_note", "updated_at_ts"])
    return True


def get_legacy_former_recommendation_items(*, client: Client) -> list[dict]:
    if not (
        _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS)
        and _has_columns("client_result_detail", LEGACY_RESULT_DETAIL_MODEL_COLUMNS)
    ):
        return []

    result_row = (
        LegacyClientResult.objects.filter(_legacy_client_q(client=client))
        # 상담 신청이 시작된 과거 row라도, 재촬영 후 생성된 최신 Top-5 배치를
        # 현재 추천으로 우선 노출해야 한다.
        .order_by("-result_id")
        .first()
    )
    if not result_row:
        return []

    detail_rows = list(
        LegacyClientResultDetail.objects.filter(result_id=result_row.result_id)
        .order_by("rank", "detail_id")
    )
    items: list[dict] = []
    selected_style_id = result_row.selected_hairstyle_id
    for detail in detail_rows:
        style_id = int(detail.hairstyle_id)
        style = _style_from_legacy(style_id)
        style_name = getattr(style, "name", None) or getattr(style, "style_name", None)
        style_desc = getattr(style, "description", None) or ""
        style_image_url = getattr(style, "image_url", None)
        style_vibe = getattr(style, "vibe", None)
        items.append(
            {
                "recommendation_id": detail.backend_recommendation_id or detail.detail_id,
                "batch_id": detail.batch_id or f"legacy-result-{result_row.result_id}",
                "source": detail.source or result_row.source or "legacy_result",
                "client_id": result_row.backend_client_ref_id,
                "legacy_client_id": result_row.client_id,
                "designer_id": result_row.backend_designer_ref_id,
                "analysis_id": result_row.analysis_id,
                "style_id": style_id,
                "style_name": detail.style_name_snapshot or style_name or f"Style {style_id}",
                "style_description": detail.style_description_snapshot or style_desc,
                "keywords": _parse_jsonish(detail.keywords_json, fallback=([style_vibe] if style_vibe else [])),
                "sample_image_url": style_image_url,
                "reference_images": (
                    [{"image_url": style_image_url, "description": style_desc}]
                    if style_image_url else []
                ),
                "simulation_image_url": resolve_storage_reference(detail.simulated_image_url),
                "synthetic_image_url": resolve_storage_reference(detail.simulated_image_url),
                "llm_explanation": detail.recommendation_reason or "",
                "reasoning": detail.recommendation_reason or "",
                "reasoning_snapshot": _parse_jsonish(detail.reasoning_snapshot, fallback={"summary": detail.recommendation_reason or ""}),
                "match_score": float(detail.final_score or detail.similarity_score or 0.0),
                "rank": int(detail.rank or 0),
                "is_chosen": bool(style_id == selected_style_id),
                "survey_snapshot": _parse_jsonish(result_row.survey_snapshot, fallback={}),
                "created_at": detail.created_at_ts or _coerce_datetime(result_row.updated_at) or _coerce_datetime(result_row.created_at),
            }
        )
    return items


def find_legacy_recommendation_context(*, recommendation_id: int) -> tuple[Client | None, dict | None]:
    if not (
        _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS)
        and _has_columns("client_result_detail", LEGACY_RESULT_DETAIL_MODEL_COLUMNS)
    ):
        return None, None

    detail = (
        LegacyClientResultDetail.objects.filter(
            Q(backend_recommendation_id=recommendation_id) | Q(detail_id=recommendation_id)
        )
        .order_by("-detail_id")
        .first()
    )
    if detail is None:
        return None, None

    result_row = LegacyClientResult.objects.filter(result_id=detail.result_id).first()
    if result_row is None:
        return None, None

    client_identifier = result_row.backend_client_ref_id or result_row.client_id
    client = get_client_by_identifier(identifier=client_identifier)
    if client is None:
        return None, None

    item = next(
        (
            row
            for row in get_legacy_former_recommendation_items(client=client)
            if str(row.get("recommendation_id")) == str(recommendation_id)
        ),
        None,
    )
    return client, item


def _select_column(columns: set[str], column_name: str, expression: str, alias: str) -> str:
    return f"{expression} AS {alias}" if column_name in columns else f"NULL AS {alias}"


def _legacy_scope_sql(
    *,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
    client: Client | None = None,
    client_columns: set[str],
    result_columns: set[str],
) -> tuple[list[str], list]:
    conditions: list[str] = []
    params: list = []
    backend_admin_id = get_backend_admin_id(admin=admin)
    legacy_admin_id = get_legacy_admin_id(admin=admin)
    backend_designer_id = get_backend_designer_id(designer=designer)
    legacy_designer_id = get_legacy_designer_id(designer=designer)

    if client is not None:
        if "backend_client_ref_id" in result_columns:
            conditions.append("r.backend_client_ref_id = %s")
            params.append(client.id)
        else:
            conditions.append("r.client_id = %s")
            params.append(str(_legacy_uuid("client", client.id)))

    if admin is not None:
        if "backend_shop_ref_id" in client_columns and backend_admin_id is not None:
            conditions.append("c.backend_shop_ref_id = %s")
            params.append(backend_admin_id)
        elif legacy_admin_id:
            conditions.append("c.shop_id = %s")
            params.append(legacy_admin_id)

    if designer is not None:
        if "backend_designer_ref_id" in result_columns and backend_designer_id is not None:
            conditions.append("r.backend_designer_ref_id = %s")
            params.append(backend_designer_id)
        elif "backend_designer_ref_id" in client_columns and backend_designer_id is not None:
            conditions.append("c.backend_designer_ref_id = %s")
            params.append(backend_designer_id)
        elif legacy_designer_id:
            conditions.append("a.designer_id = %s")
            params.append(legacy_designer_id)

    return conditions, params


def _build_legacy_age_profile(row: dict) -> dict | None:
    age_input = row.get("age_input")
    birth_year_estimate = row.get("birth_year_estimate")
    if age_input is not None:
        try:
            age_input = int(age_input)
        except (TypeError, ValueError):
            age_input = None
    if birth_year_estimate is not None:
        try:
            birth_year_estimate = int(birth_year_estimate)
        except (TypeError, ValueError):
            birth_year_estimate = None
    return build_age_profile(age=age_input, birth_year_estimate=birth_year_estimate, reference_date=timezone.localdate())


def _build_legacy_analysis_namespace(*, row: LegacyClientAnalysis, client: Client) -> SimpleNamespace:
    return SimpleNamespace(
        id=row.backend_analysis_id or row.analysis_id,
        client=client,
        client_id=client.id,
        face_shape=row.face_type,
        golden_ratio_score=row.golden_ratio_score,
        image_url=row.analysis_image_url or row.processed_path or row.original_image_url,
        status=row.status or "DONE",
        landmark_snapshot=_parse_jsonish(
            row.analysis_landmark_snapshot if row.analysis_landmark_snapshot is not None else row.landmark_data,
            fallback={},
        ),
        created_at=_coerce_datetime(row.created_at) or row.updated_at_ts,
    )



def get_legacy_client_visit_summary_map(
    *,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
    client: Client | None = None,
) -> dict[str, dict]:
    if not _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS):
        return {}

    rows = list(_legacy_result_queryset(admin=admin, designer=designer, client=client))
    if not rows:
        return {}

    grouped: dict[str, dict] = {}
    for row in rows:
        legacy_client_id = str(row.client_id or "").strip()
        backend_client_id = str(row.backend_client_ref_id or "").strip()
        identity = legacy_client_id or backend_client_id
        if not identity:
            continue

        summary = grouped.setdefault(
            identity,
            {
                "legacy_client_id": legacy_client_id,
                "backend_client_id": backend_client_id,
                "visit_count": 0,
                "last_visit_date": None,
            },
        )
        if legacy_client_id and not summary["legacy_client_id"]:
            summary["legacy_client_id"] = legacy_client_id
        if backend_client_id and not summary["backend_client_id"]:
            summary["backend_client_id"] = backend_client_id

        summary["visit_count"] += 1
        event_at = _coerce_datetime(row.updated_at) or _coerce_datetime(row.created_at)
        if event_at is not None and (
            summary["last_visit_date"] is None or event_at > summary["last_visit_date"]
        ):
            summary["last_visit_date"] = event_at

    lookup: dict[str, dict] = {}
    for summary in grouped.values():
        payload = {
            "visit_count": int(summary["visit_count"] or 0),
            "last_visit_date": summary["last_visit_date"],
        }
        for key in {
            str(summary["legacy_client_id"] or "").strip(),
            str(summary["backend_client_id"] or "").strip(),
        }:
            if key:
                lookup[key] = payload
    return lookup
def get_legacy_active_consultation_items(
    *,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
    client: Client | None = None,
) -> list[dict] | None:
    if not (
        _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS)
        and _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS)
    ):
        return None

    def _row_is_active(row) -> bool:
        status_text = str(row.status or "").upper()
        return bool(row.is_active) or (
            bool(row.is_confirmed)
            and status_text not in {"CLOSED", "CANCELLED"}
        )

    rows = [
        row
        for row in _legacy_result_queryset(admin=admin, designer=designer, client=client)
        if _row_is_active(row)
    ]
    rows.sort(
        key=lambda row: (
            _coerce_datetime(row.updated_at) or _coerce_datetime(row.created_at) or timezone.make_aware(timezone.datetime.min),
            row.result_id,
        ),
        reverse=True,
    )

    legacy_client_ids = {row.client_id for row in rows}
    client_map = {
        item.client_id: item
        for item in LegacyClient.objects.filter(client_id__in=legacy_client_ids)
    }
    analysis_ids = {row.analysis_id for row in rows if row.analysis_id is not None}
    analysis_map = {
        item.analysis_id: item
        for item in LegacyClientAnalysis.objects.filter(analysis_id__in=analysis_ids)
    } if _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS) else {}
    legacy_designer_ids = {item.designer_id for item in analysis_map.values() if item.designer_id}
    designer_map = {
        item.designer_id: item
        for item in LegacyDesigner.objects.filter(designer_id__in=legacy_designer_ids)
    } if _has_columns("designer", LEGACY_DESIGNER_MODEL_COLUMNS) else {}
    style_ids = {row.selected_hairstyle_id for row in rows if row.selected_hairstyle_id}
    style_map = {
        item.hairstyle_id: item
        for item in LegacyHairstyle.objects.filter(hairstyle_id__in=style_ids).only(
            "hairstyle_id",
            "name",
            "style_name",
            "image_url",
            "description",
        )
    } if _has_columns("hairstyle", LEGACY_HAIRSTYLE_MODEL_COLUMNS) else {}
    result_map = {row.result_id: row for row in rows}
    detail_counts: dict[int, int] = {}
    selected_detail_map: dict[int, LegacyClientResultDetail] = {}
    if _has_columns("client_result_detail", LEGACY_RESULT_DETAIL_MODEL_COLUMNS):
        detail_rows = list(
            LegacyClientResultDetail.objects.filter(result_id__in=[row.result_id for row in rows])
            .only(
                "detail_id",
                "result_id",
                "hairstyle_id",
                "backend_recommendation_id",
                "is_chosen",
                "created_at_ts",
                "final_score",
                "similarity_score",
                "style_description_snapshot",
                "sample_image_url",
                "simulated_image_url",
            )
            .order_by("-created_at_ts", "-detail_id")
        )
        for detail in detail_rows:
            detail_counts[detail.result_id] = int(detail_counts.get(detail.result_id, 0)) + 1
            if detail.result_id in selected_detail_map:
                continue
            parent_row = result_map.get(detail.result_id)
            if parent_row is None:
                continue
            if detail.hairstyle_id == parent_row.selected_hairstyle_id or bool(detail.is_chosen):
                selected_detail_map[detail.result_id] = detail
        for detail in detail_rows:
            selected_detail_map.setdefault(detail.result_id, detail)

    items: list[dict] = []
    seen_clients: set[str] = set()
    for row in rows:
        legacy_client_id = str(row.client_id or "").strip()
        if not legacy_client_id or legacy_client_id in seen_clients:
            continue
        seen_clients.add(legacy_client_id)

        legacy_client = client_map.get(legacy_client_id)
        legacy_analysis = analysis_map.get(row.analysis_id)
        legacy_designer = designer_map.get(legacy_analysis.designer_id) if legacy_analysis and legacy_analysis.designer_id else None
        legacy_style = style_map.get(row.selected_hairstyle_id) if row.selected_hairstyle_id else None
        backend_client_id = row.backend_client_ref_id
        backend_designer_id = row.backend_designer_ref_id or (legacy_client.backend_designer_ref_id if legacy_client else None)
        status_text = str(row.status or "").upper()
        is_active = _row_is_active(row)
        is_read = bool(row.is_read) if row.is_read is not None else False
        selected_detail = selected_detail_map.get(row.result_id)
        selected_style_name = (
            getattr(legacy_style, "name", None)
            or getattr(legacy_style, "style_name", None)
        )
        selected_style_image_url = None
        selected_style_candidates = [
            (getattr(selected_detail, "simulated_image_url", None) if selected_detail is not None else None),
            (getattr(selected_detail, "sample_image_url", None) if selected_detail is not None else None),
            getattr(legacy_style, "image_url", None),
        ]
        for selected_style_reference in selected_style_candidates:
            if selected_style_reference in (None, ""):
                continue
            resolved_selected_style_reference = resolve_storage_reference(selected_style_reference)
            if resolved_selected_style_reference not in (None, ""):
                selected_style_image_url = resolved_selected_style_reference
                break
        raw_selected_score = None
        if selected_detail is not None:
            raw_selected_score = selected_detail.final_score
            if raw_selected_score in (None, ""):
                raw_selected_score = selected_detail.similarity_score
        selected_style_score = None
        if raw_selected_score not in (None, ""):
            try:
                selected_style_score = float(raw_selected_score)
            except (TypeError, ValueError):
                selected_style_score = None
        selected_style_description = (
            (getattr(selected_detail, "style_description_snapshot", None) if selected_detail is not None else None)
            or getattr(legacy_style, "description", None)
        )
        age_profile = _build_legacy_age_profile(
            {
                "age_input": (legacy_client.age_input if legacy_client else None),
                "birth_year_estimate": (legacy_client.birth_year_estimate if legacy_client else None),
            }
        )

        items.append(
            {
                "result_id": row.result_id,
                "consultation_id": row.backend_consultation_id or row.result_id,
                "client_id": backend_client_id,
                "legacy_client_id": legacy_client_id,
                "client_name": (legacy_client.client_name if legacy_client else None),
                "phone": (legacy_client.phone if legacy_client else None),
                "status": row.status or ("PENDING" if is_active else "CLOSED"),
                "has_unread_consultation": not is_read,
                "designer_id": backend_designer_id,
                "legacy_designer_id": (legacy_analysis.designer_id if legacy_analysis else None),
                "designer_name": (
                    getattr(legacy_designer, "name", None)
                    or getattr(legacy_designer, "designer_name", None)
                ),
                "selected_style_id": row.selected_hairstyle_id,
                "selected_style_name": selected_style_name,
                "selected_style_image_url": selected_style_image_url,
                "selected_style_score": selected_style_score,
                "selected_style_description": selected_style_description,
                "selected_recommendation_id": (
                    (selected_detail.backend_recommendation_id or selected_detail.detail_id)
                    if selected_detail is not None
                    else None
                ),
                "recommendation_count": int(detail_counts.get(row.result_id, 0)),
                "last_activity_at": _coerce_datetime(row.updated_at) or _coerce_datetime(row.created_at),
                "is_active": bool(is_active),
                "age_profile": age_profile,
            }
        )

    return items


def get_legacy_active_consultation_count(
    *,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
    client: Client | None = None,
) -> int:
    if not _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS):
        return 0

    queryset = _legacy_result_queryset(admin=admin, designer=designer, client=client).only(
        "client_id",
        "backend_client_ref_id",
        "status",
        "is_active",
        "is_confirmed",
    )

    active_clients: set[str] = set()
    for row in queryset:
        status_text = str(row.status or "").upper()
        is_active = bool(row.is_active) or (
            bool(row.is_confirmed)
            and status_text not in {"CLOSED", "CANCELLED"}
        )
        if not is_active:
            continue
        client_key = str(row.backend_client_ref_id or row.client_id or "").strip()
        if client_key:
            active_clients.add(client_key)
    return len(active_clients)


def has_legacy_chosen_consultation(*, client: Client) -> bool:
    if not _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS):
        return False
    result_queryset = _legacy_result_queryset(client=client)
    if result_queryset.filter(selected_hairstyle_id__gt=0).exists():
        return True
    if _has_columns("client_result_detail", LEGACY_RESULT_DETAIL_MODEL_COLUMNS):
        result_ids = list(result_queryset.values_list("result_id", flat=True))
        if result_ids:
            return LegacyClientResultDetail.objects.filter(
                result_id__in=result_ids,
                is_chosen=True,
            ).exists()
    return False


def get_legacy_confirmed_selection_items(
    *,
    since=None,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
    client: Client | None = None,
    compact: bool = False,
) -> list[dict] | None:
    if not (
        _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS)
        and _has_columns("client_result_detail", LEGACY_RESULT_DETAIL_MODEL_COLUMNS)
        and _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS)
    ):
        return None

    result_rows = list(
        _legacy_result_queryset(admin=admin, designer=designer, client=client).only(
            "result_id",
            "analysis_id",
            "client_id",
            "backend_client_ref_id",
            "backend_designer_ref_id",
            "selected_hairstyle_id",
            "updated_at",
            "created_at",
            "survey_snapshot",
            "source",
        )
    )
    result_map = {row.result_id: row for row in result_rows}
    if not result_map:
        return []
    detail_rows = list(
        LegacyClientResultDetail.objects.filter(result_id__in=result_map.keys())
        .only(
            "detail_id",
            "result_id",
            "hairstyle_id",
            "final_score",
            "similarity_score",
            "backend_recommendation_id",
            "source",
            "style_name_snapshot",
            "style_description_snapshot",
            "keywords_json",
            "sample_image_url",
            "is_chosen",
            "created_at_ts",
        )
        .order_by("-created_at_ts", "-detail_id")
    )
    legacy_client_ids = {row.client_id for row in result_rows}
    client_map = {
        item.client_id: item
        for item in LegacyClient.objects.filter(client_id__in=legacy_client_ids).only(
            "client_id",
            "client_name",
            "phone",
            "age_input",
            "birth_year_estimate",
        )
    }
    analysis_ids = {row.analysis_id for row in result_rows if row.analysis_id is not None}
    analysis_map = (
        {
            item.analysis_id: item
            for item in LegacyClientAnalysis.objects.filter(analysis_id__in=analysis_ids).only(
                "analysis_id",
                "designer_id",
            )
        }
        if (not compact and _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS))
        else {}
    )
    style_ids = {row.hairstyle_id for row in detail_rows}
    style_map = (
        {
            item.hairstyle_id: item
            for item in LegacyHairstyle.objects.filter(hairstyle_id__in=style_ids).only(
                "hairstyle_id",
                "name",
                "style_name",
                "description",
                "image_url",
            )
        }
        if (not compact and _has_columns("hairstyle", LEGACY_HAIRSTYLE_MODEL_COLUMNS))
        else {}
    )

    items: list[dict] = []
    for detail in detail_rows:
        result_row = result_map.get(detail.result_id)
        if result_row is None:
            continue
        if detail.hairstyle_id != result_row.selected_hairstyle_id and not bool(detail.is_chosen):
            continue
        created_at = detail.created_at_ts or _coerce_datetime(result_row.updated_at) or _coerce_datetime(result_row.created_at)
        if since is not None and created_at is not None and created_at < since:
            continue
        legacy_client = client_map.get(result_row.client_id)
        legacy_analysis = analysis_map.get(result_row.analysis_id)
        legacy_style = style_map.get(detail.hairstyle_id)
        age_profile = _build_legacy_age_profile(
            {
                "age_input": (legacy_client.age_input if legacy_client else None),
                "birth_year_estimate": (legacy_client.birth_year_estimate if legacy_client else None),
            }
        )
        score = detail.final_score
        if score in (None, ""):
            score = detail.similarity_score
        try:
            score = float(score or 0.0)
        except (TypeError, ValueError):
            score = 0.0

        item = {
            "recommendation_id": detail.backend_recommendation_id or detail.detail_id,
            "result_id": result_row.result_id,
            "client_id": result_row.backend_client_ref_id,
            "legacy_client_id": result_row.client_id,
            "designer_id": result_row.backend_designer_ref_id,
            "legacy_designer_id": (legacy_analysis.designer_id if legacy_analysis else None),
            "style_id": int(detail.hairstyle_id),
            "style_name": detail.style_name_snapshot or getattr(legacy_style, "name", None) or getattr(legacy_style, "style_name", None) or f"Style {detail.hairstyle_id}",
            "match_score": score,
            "created_at": created_at,
            "survey_snapshot": _parse_jsonish(result_row.survey_snapshot, fallback={}),
            "age_profile": age_profile,
            "client_name": (legacy_client.client_name if legacy_client else None),
            "phone": (legacy_client.phone if legacy_client else None),
            "source": detail.source or result_row.source or "legacy_result",
        }
        if not compact:
            item.update(
                {
                    "style_description": detail.style_description_snapshot or getattr(legacy_style, "description", None) or "",
                    "image_url": detail.sample_image_url or getattr(legacy_style, "image_url", None),
                    "keywords": _parse_jsonish(detail.keywords_json, fallback=[]),
                }
            )
        items.append(item)

    return items


def get_legacy_activity_client_map_by_day(
    *,
    start_date,
    days: int,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
    client: Client | None = None,
) -> dict[str, set[str]] | None:
    if not _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS):
        return None

    activity_by_day: dict[str, set[str]] = {
        (start_date + timezone.timedelta(days=offset)).isoformat(): set()
        for offset in range(days)
    }
    if _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        backend_admin_id = get_backend_admin_id(admin=admin)
        backend_designer_id = get_backend_designer_id(designer=designer)
        legacy_designer_id = get_legacy_designer_id(designer=designer)
        analysis_rows = list(LegacyClientAnalysis.objects.all())
        if client is not None:
            analysis_rows = [row for row in analysis_rows if row.backend_client_ref_id == client.id or row.client_id == str(_legacy_uuid("client", client.id))]
        if admin is not None:
            client_scope = Q()
            if backend_admin_id is not None:
                client_scope |= Q(backend_shop_ref_id=backend_admin_id)
            legacy_admin_id = get_legacy_admin_id(admin=admin)
            if legacy_admin_id:
                client_scope |= Q(shop_id=legacy_admin_id)
            if not client_scope:
                return activity_by_day
            allowed_backend = set(LegacyClient.objects.filter(client_scope).values_list("backend_client_id", flat=True))
            allowed_legacy = set(LegacyClient.objects.filter(client_scope).values_list("client_id", flat=True))
            analysis_rows = [row for row in analysis_rows if row.backend_client_ref_id in allowed_backend or row.client_id in allowed_legacy]
        if designer is not None:
            analysis_rows = [
                row
                for row in analysis_rows
                if (
                    (backend_designer_id is not None and row.backend_designer_ref_id == backend_designer_id)
                    or (legacy_designer_id and row.designer_id == legacy_designer_id)
                )
            ]
        for row in analysis_rows:
            dt = row.updated_at_ts or _coerce_datetime(row.created_at)
            if dt is None or dt.date() < start_date:
                continue
            activity_date = dt.date().isoformat()
            if activity_date not in activity_by_day:
                continue
            client_key = str(row.backend_client_ref_id or row.client_id or "").strip()
            if client_key:
                activity_by_day[activity_date].add(client_key)

    if _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS):
        result_rows = list(_legacy_result_queryset(admin=admin, designer=designer, client=client))
        for row in result_rows:
            dt = _coerce_datetime(row.updated_at) or _coerce_datetime(row.created_at)
            if dt is None or dt.date() < start_date:
                continue
            activity_date = dt.date().isoformat()
            if activity_date not in activity_by_day:
                continue
            client_key = str(row.backend_client_ref_id or row.client_id or "").strip()
            if client_key:
                activity_by_day[activity_date].add(client_key)

    return activity_by_day


def _sync_admin_row(cursor, admin: AdminAccount) -> None:
    if not _has_table("shop"):
        return

    legacy_id = _legacy_uuid("shop", admin.id)
    cursor.execute("DELETE FROM shop WHERE shop_id = %s", [legacy_id])
    cursor.execute(
        """
        INSERT INTO shop (
            shop_id, login_id, shop_name, biz_number, owner_phone, password, admin_pin, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            legacy_id,
            admin.phone,
            admin.store_name,
            admin.business_number,
            admin.phone,
            admin.password_hash,
            admin.admin_pin,
            admin.created_at,
            admin.created_at,
        ],
    )

    columns = _table_columns("shop")
    required = {
        "backend_admin_id",
        "name",
        "store_name",
        "role",
        "phone",
        "business_number",
        "password_hash",
        "is_active",
        "consent_snapshot",
        "consented_at",
    }
    if required.issubset(columns):
        cursor.execute(
            """
            UPDATE shop
            SET backend_admin_id=%s,
                name=%s,
                store_name=%s,
                role=%s,
                phone=%s,
                business_number=%s,
                password_hash=%s,
                is_active=%s,
                consent_snapshot=%s,
                consented_at=%s
            WHERE shop_id=%s
            """,
            [
                admin.id,
                admin.name,
                admin.store_name,
                admin.role,
                admin.phone,
                admin.business_number,
                admin.password_hash,
                admin.is_active,
                json.dumps(admin.consent_snapshot or {}, ensure_ascii=False),
                admin.consented_at,
                legacy_id,
            ],
        )


def _sync_designer_row(cursor, designer: Designer) -> None:
    if not _has_table("designer"):
        return

    legacy_id = _legacy_uuid("designer", designer.id)
    shop_legacy_id = _legacy_uuid("shop", designer.shop_id)
    cursor.execute("DELETE FROM designer WHERE designer_id = %s", [legacy_id])
    cursor.execute(
        """
        INSERT INTO designer (
            designer_id, shop_id, designer_name, login_id, password, is_active, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            legacy_id,
            shop_legacy_id,
            designer.name,
            designer.phone,
            designer.pin_hash,
            designer.is_active,
            designer.created_at,
            designer.created_at,
        ],
    )

    columns = _table_columns("designer")
    required = {"backend_designer_id", "backend_shop_ref_id", "name", "phone", "pin_hash"}
    if required.issubset(columns):
        cursor.execute(
            """
            UPDATE designer
            SET backend_designer_id=%s,
                backend_shop_ref_id=%s,
                name=%s,
                phone=%s,
                pin_hash=%s
            WHERE designer_id=%s
            """,
            [
                designer.id,
                designer.shop_id,
                designer.name,
                designer.phone,
                designer.pin_hash,
                legacy_id,
            ],
        )


def _sync_client_row(cursor, client: Client) -> None:
    if not _has_table("client"):
        return

    legacy_id = _legacy_uuid("client", client.id)
    cursor.execute("DELETE FROM client WHERE client_id = %s", [legacy_id])
    cursor.execute(
        """
        INSERT INTO client (
            client_id, shop_id, client_name, phone, gender, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            legacy_id,
            (_legacy_uuid("shop", client.shop_id) if client.shop_id else None),
            client.name,
            client.phone,
            _legacy_gender(client.gender),
            client.created_at,
            client.created_at,
        ],
    )

    columns = _table_columns("client")
    required = {
        "backend_client_id",
        "backend_shop_ref_id",
        "backend_designer_ref_id",
        "name",
        "assigned_at",
        "assignment_source",
        "age_input",
        "birth_year_estimate",
    }
    if required.issubset(columns):
        cursor.execute(
            """
            UPDATE client
            SET backend_client_id=%s,
                backend_shop_ref_id=%s,
                backend_designer_ref_id=%s,
                name=%s,
                assigned_at=%s,
                assignment_source=%s,
                age_input=%s,
                birth_year_estimate=%s
            WHERE client_id=%s
            """,
            [
                client.id,
                client.shop_id,
                client.designer_id,
                client.name,
                client.assigned_at,
                client.assignment_source,
                client.age_input,
                client.birth_year_estimate,
                legacy_id,
            ],
        )


def _sync_survey_row(cursor, survey: Survey) -> None:
    if not _has_table("client_survey"):
        return

    client_legacy_id = _legacy_uuid("client", survey.client_id)
    cursor.execute("DELETE FROM client_survey WHERE survey_id = %s", [survey.id])
    cursor.execute(
        """
        INSERT INTO client_survey (
            survey_id, client_id, hair_length, hair_mood, hair_condition, hair_color, budget, preference_vector, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            survey.id,
            client_legacy_id,
            survey.target_length,
            survey.target_vibe,
            survey.scalp_type,
            survey.hair_colour,
            survey.budget_range,
            _legacy_preference_vector_storage(survey.preference_vector),
            survey.created_at,
        ],
    )

    columns = _table_columns("client_survey")
    required = {
        "backend_survey_id",
        "backend_client_ref_id",
        "target_length",
        "target_vibe",
        "scalp_type",
        "hair_colour",
        "budget_range",
        "preference_vector_json",
        "created_at_ts",
    }
    if required.issubset(columns):
        cursor.execute(
            """
            UPDATE client_survey
            SET backend_survey_id=%s,
                backend_client_ref_id=%s,
                target_length=%s,
                target_vibe=%s,
                scalp_type=%s,
                hair_colour=%s,
                budget_range=%s,
                preference_vector_json=%s,
                created_at_ts=%s
            WHERE survey_id=%s
            """,
            [
                survey.id,
                survey.client_id,
                survey.target_length,
                survey.target_vibe,
                survey.scalp_type,
                survey.hair_colour,
                survey.budget_range,
                json.dumps(survey.preference_vector or [], ensure_ascii=False),
                survey.created_at,
                survey.id,
            ],
        )


def _sync_style_row(cursor, style: Style) -> None:
    if not _has_table("hairstyle"):
        return

    cursor.execute("DELETE FROM hairstyle WHERE hairstyle_id = %s", [style.id])
    cursor.execute(
        """
        INSERT INTO hairstyle (
            hairstyle_id, chroma_id, style_name, image_url, created_at
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        [
            style.id,
            str(style.id),
            style.name,
            style.image_url,
            style.created_at,
        ],
    )

    columns = _table_columns("hairstyle")
    required = {"backend_style_id", "name", "vibe", "description"}
    if required.issubset(columns):
        cursor.execute(
            """
            UPDATE hairstyle
            SET backend_style_id=%s,
                name=%s,
                vibe=%s,
                description=%s
            WHERE hairstyle_id=%s
            """,
            [style.id, style.name, style.vibe, style.description, style.id],
        )


def _as_legacy_text(value) -> str:
    if value in (None, ""):
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _legacy_preference_vector_storage(preference_vector) -> str:
    values = list(preference_vector or [])
    if connection.vendor == "postgresql":
        return "{" + ",".join(str(float(value)) for value in values) + "}"
    return json.dumps(values, ensure_ascii=False)


def sync_model_team_admin_state(*, admin: AdminAccount) -> bool:
    if not _has_columns("shop", LEGACY_SHOP_MODEL_COLUMNS):
        return False

    phone = _normalize_phone(admin.phone)
    LegacyShop.objects.update_or_create(
        shop_id=get_legacy_admin_id(admin=admin),
        defaults={
            "login_id": phone,
            "shop_name": admin.store_name,
            "biz_number": admin.business_number,
            "owner_phone": phone,
            "password": admin.password_hash,
            "admin_pin": admin.admin_pin,
            "created_at": _as_legacy_text(admin.created_at),
            "updated_at": _as_legacy_text(admin.created_at),
            "backend_admin_id": admin.id,
            "name": admin.name,
            "store_name": admin.store_name,
            "role": admin.role,
            "phone": phone,
            "business_number": admin.business_number,
            "password_hash": admin.password_hash,
            "is_active": admin.is_active,
            "consent_snapshot": admin.consent_snapshot or {},
            "consented_at": admin.consented_at,
        },
    )
    return True


def sync_model_team_designer_state(*, designer: Designer) -> bool:
    if not _has_columns("designer", LEGACY_DESIGNER_MODEL_COLUMNS):
        return False

    sync_model_team_admin_state(admin=designer.shop)
    phone = _normalize_phone(designer.phone)
    LegacyDesigner.objects.update_or_create(
        designer_id=get_legacy_designer_id(designer=designer),
        defaults={
            "shop_id": get_legacy_admin_id(admin=designer.shop) or "",
            "designer_name": designer.name,
            "login_id": phone,
            "password": designer.pin_hash,
            "is_active": designer.is_active,
            "created_at": _as_legacy_text(designer.created_at),
            "updated_at": _as_legacy_text(designer.created_at),
            "backend_designer_id": designer.id,
            "backend_shop_ref_id": designer.shop_id,
            "name": designer.name,
            "phone": phone,
            "pin_hash": designer.pin_hash,
        },
    )
    return True


def sync_model_team_client_state(*, client: Client) -> bool:
    if not _has_columns("client", LEGACY_CLIENT_MODEL_COLUMNS):
        return False

    if client.shop_id:
        sync_model_team_admin_state(admin=client.shop)
    if client.designer_id:
        sync_model_team_designer_state(designer=client.designer)

    LegacyClient.objects.update_or_create(
        client_id=get_legacy_client_id(client=client),
        defaults={
            "shop_id": (get_legacy_admin_id(admin=client.shop) or ""),
            "client_name": client.name,
            "phone": _normalize_phone(client.phone),
            "gender": _legacy_gender(client.gender),
            "created_at": _as_legacy_text(client.created_at),
            "updated_at": _as_legacy_text(client.created_at),
            "backend_client_id": client.id,
            "backend_shop_ref_id": client.shop_id,
            "backend_designer_ref_id": client.designer_id,
            "name": client.name,
            "assigned_at": client.assigned_at,
            "assignment_source": client.assignment_source,
            "age_input": client.age_input,
            "birth_year_estimate": client.birth_year_estimate,
        },
    )
    return True


def sync_model_team_survey_state(*, survey: Survey) -> bool:
    if not _has_columns("client_survey", LEGACY_SURVEY_MODEL_COLUMNS):
        return False

    sync_model_team_client_state(client=survey.client)
    LegacyClientSurvey.objects.update_or_create(
        survey_id=survey.id,
        defaults={
            "client_id": get_legacy_client_id(client=survey.client) or "",
            "hair_length": survey.target_length,
            "hair_mood": survey.target_vibe,
            "hair_condition": survey.scalp_type,
            "hair_color": survey.hair_colour,
            "budget": survey.budget_range,
            "preference_vector": _legacy_preference_vector_storage(survey.preference_vector),
            "updated_at": _as_legacy_text(survey.created_at),
            "backend_survey_id": survey.id,
            "backend_client_ref_id": survey.client_id,
            "target_length": survey.target_length,
            "target_vibe": survey.target_vibe,
            "scalp_type": survey.scalp_type,
            "hair_colour": survey.hair_colour,
            "budget_range": survey.budget_range,
            "preference_vector_json": survey.preference_vector or [],
            "created_at_ts": survey.created_at,
        },
    )
    return True


def sync_model_team_style_state(*, style: Style) -> bool:
    if not _has_columns("hairstyle", LEGACY_HAIRSTYLE_MODEL_COLUMNS):
        return False

    LegacyHairstyle.objects.update_or_create(
        hairstyle_id=style.id,
        defaults={
            "chroma_id": str(style.id),
            "style_name": style.name,
            "image_url": style.image_url or "",
            "created_at": _as_legacy_text(style.created_at),
            "backend_style_id": style.id,
            "name": style.name,
            "vibe": style.vibe,
            "description": style.description,
        },
    )
    return True


def sync_model_team_analysis_state(*, client: Client) -> bool:
    if not _has_columns("client_analysis", LEGACY_ANALYSIS_MODEL_COLUMNS):
        return False
    return get_latest_legacy_analysis(client=client) is not None


def sync_model_team_result_state(*, client: Client) -> bool:
    if not _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS):
        return False
    if not _has_columns("client_result_detail", LEGACY_RESULT_DETAIL_MODEL_COLUMNS):
        return False
    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        return False
    return LegacyClientResult.objects.filter(client_id=legacy_client_id).exists()


def sync_model_team_rows(
    *,
    admin: AdminAccount | None = None,
    designer: Designer | None = None,
    client: Client | None = None,
    survey: Survey | None = None,
    style: Style | None = None,
) -> bool:
    if not _existing_legacy_tables():
        return False

    handled = False
    pending_admin = admin
    pending_designer = designer
    pending_client = client
    pending_survey = survey
    pending_style = style

    if admin is not None and sync_model_team_admin_state(admin=admin):
        handled = True
        pending_admin = None
    if designer is not None and sync_model_team_designer_state(designer=designer):
        handled = True
        pending_designer = None
    if client is not None and sync_model_team_client_state(client=client):
        handled = True
        pending_client = None
    if survey is not None and sync_model_team_survey_state(survey=survey):
        handled = True
        pending_survey = None
    if style is not None and sync_model_team_style_state(style=style):
        handled = True
        pending_style = None

    if all(item is None for item in (pending_admin, pending_designer, pending_client, pending_survey, pending_style)):
        return handled

    with transaction.atomic():
        with connection.cursor() as cursor:
            if pending_admin is not None:
                _sync_admin_row(cursor, pending_admin)
            if pending_designer is not None:
                _sync_admin_row(cursor, pending_designer.shop)
                _sync_designer_row(cursor, pending_designer)
            if pending_client is not None:
                if pending_client.shop_id:
                    _sync_admin_row(cursor, pending_client.shop)
                if pending_client.designer_id:
                    _sync_designer_row(cursor, pending_client.designer)
                _sync_client_row(cursor, pending_client)
            if pending_survey is not None:
                client_for_survey = pending_survey.client
                if client_for_survey.shop_id:
                    _sync_admin_row(cursor, client_for_survey.shop)
                if client_for_survey.designer_id:
                    _sync_designer_row(cursor, client_for_survey.designer)
                _sync_client_row(cursor, client_for_survey)
                _sync_survey_row(cursor, pending_survey)
            if pending_style is not None:
                _sync_style_row(cursor, pending_style)
    return True


def sync_model_team_runtime_state(*, client: Client | None = None) -> bool:
    """Mirror runtime writes back into model-team tables.

    When a client is provided we now update only that client's legacy analysis
    and result rows directly, instead of re-syncing every legacy table. The
    full canonical -> legacy sync remains as a fallback for broader maintenance
    paths where no single client scope is available.
    """

    if client is not None and _existing_legacy_tables():
        sync_model_team_rows(client=client)
        sync_model_team_analysis_state(client=client)
        sync_model_team_result_state(client=client)
        return True

    summary = sync_legacy_model_tables_if_present()
    return summary is not None
