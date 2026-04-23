import json
import logging
import os
import re
import time
from collections import Counter
from typing import TYPE_CHECKING
from urllib import error, request

from django.apps import apps as django_apps
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, OperationalError, ProgrammingError, connection
from django.db.models import Count, Q
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from app.api.v1.admin_auth import issue_admin_token_pair
from app.api.v1.recommendation_logic import STYLE_CATALOG
from app.api.v1.services_django import (
    _build_legacy_retry_recommendation_meta,
    ensure_catalog_styles,
    get_latest_analysis,
    get_latest_capture,
    get_latest_survey,
    serialize_recommendation_row,
)
from app.models_model_team import (
    LegacyClient,
    LegacyClientAnalysis,
    LegacyClientResult,
    LegacyClientSurvey,
    LegacyDesigner,
    LegacyHairstyle,
)
from app.services.age_profile import build_client_age_profile
from app.services.ai_facade import get_ai_health
from app.services.model_team_bridge import (
    _client_from_legacy_row,
    _designer_from_legacy_row,
    admin_exists_by_business_number,
    admin_exists_by_phone,
    create_admin_record,
    get_admin_by_identifier,
    get_backend_admin_id,
    get_backend_designer_id,
    get_client_by_identifier,
    get_admin_by_phone,
    get_designer_by_identifier,
    get_designer_for_admin,
    get_legacy_active_consultation_count,
    get_legacy_active_consultation_items,
    get_legacy_admin_id,
    get_legacy_activity_client_map_by_day,
    get_legacy_analysis_capture_count,
    get_legacy_analysis_capture_history,
    get_latest_legacy_analysis_capture_bundle,
    get_legacy_analysis_history,
    get_legacy_analysis_count,
    get_legacy_capture_history,
    get_legacy_capture_count,
    get_legacy_client_id,
    get_legacy_client_visit_summary_map,
    get_legacy_confirmed_selection_items,
    get_legacy_designer_id,
    get_legacy_former_recommendation_items,
    has_legacy_analysis_source,
    has_legacy_result_source,
    get_scoped_client_ids,
    get_style_record,
    sync_model_team_runtime_state,
    upsert_client_record,
)
from app.services.runtime_cache import (
    build_partner_cache_key,
    cache_timeout,
    get_cached_payload,
    invalidate_partner_client_cache,
    set_cached_payload,
)
from app.services.storage_service import build_storage_snapshot, resolve_storage_reference

if TYPE_CHECKING:
    from app.models_django import (
        AdminAccount,
        CaptureRecord,
        Client,
        ClientProfileNote,
        ConsultationRequest,
        Designer,
        DesignerDiagnosisCard,
        FormerRecommendation,
        StyleSelection,
    )


logger = logging.getLogger(__name__)
STYLE_PROFILE_BY_ID = {profile.style_id: profile for profile in STYLE_CATALOG}


def _get_runtime_model(model_name: str):
    try:
        model = django_apps.get_model("mirrai_app", model_name)
    except LookupError:
        model = None
    if model is not None:
        return model

    from app import models_django

    return getattr(models_django, model_name)


def _partner_lookup_cache_key(
    name: str,
    *,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
    client: "Client | None" = None,
    **extras,
) -> str:
    normalized_extras = {key: value for key, value in extras.items() if value not in (None, "", [], {}, ())}
    return build_partner_cache_key(
        name,
        admin=admin,
        designer=designer,
        client=client,
        extras=normalized_extras,
    )


def _partner_cache_get(key: str):
    return get_cached_payload(key)


def _partner_cache_set(key: str, payload, *, timeout_setting: str, default_timeout: int):
    return set_cached_payload(
        key,
        payload,
        timeout=cache_timeout(timeout_setting, default_timeout),
    )


def _invalidate_partner_client_payloads(
    *,
    client: "Client | None" = None,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> None:
    invalidate_partner_client_cache(client=client, admin=admin, designer=designer)


def _required_field_message(label: str) -> str:
    if not label:
        return "필수 정보입니다."
    last_char = label[-1]
    code = ord(last_char)
    if 0xAC00 <= code <= 0xD7A3:
        has_batchim = (code - 0xAC00) % 28 != 0
        return f"{label}{'은' if has_batchim else '는'} 필수 정보입니다."
    return f"{label}는 필수 정보입니다."


def _normalize_phone(value: str) -> str:
    return value.replace("-", "").strip()


def _is_valid_mobile_phone(value: str) -> bool:
    normalized = _normalize_phone(value)
    return bool(re.fullmatch(r"010\d{8}", normalized))


def _normalize_business_number(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _format_business_number(value: str) -> str:
    return f"{value[:3]}-{value[3:5]}-{value[5:]}"


def _is_valid_business_number(value: str) -> bool:
    if len(value) != 10 or not value.isdigit():
        return False

    digits = [int(char) for char in value]
    weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    checksum = sum(digit * weight for digit, weight in zip(digits[:9], weights))
    checksum += (digits[8] * 5) // 10
    expected = (10 - (checksum % 10)) % 10
    return digits[9] == expected


def _business_number_variants(value: str) -> set[str]:
    normalized = _normalize_business_number(value)
    if len(normalized) != 10:
        return {value}
    return {normalized, _format_business_number(normalized)}


def _ai_health() -> dict:
    return {
        **get_ai_health(),
        "checked_at": timezone.now(),
    }


def _serialize_survey(survey) -> dict | None:
    if not survey:
        return None
    question_answers = getattr(survey, "question_answers", None)
    survey_profile = getattr(survey, "survey_profile", None)
    if question_answers is None and isinstance(survey_profile, dict):
        question_answers = survey_profile.get("question_answers")
    if not isinstance(question_answers, dict):
        question_answers = {}
    if not isinstance(survey_profile, dict):
        survey_profile = {}
    gender_branch = getattr(survey, "gender_branch", None) or survey_profile.get("gender_branch")
    return {
        "target_length": survey.target_length,
        "target_vibe": survey.target_vibe,
        "scalp_type": survey.scalp_type,
        "hair_colour": survey.hair_colour,
        "budget_range": survey.budget_range,
        "question_answers": question_answers,
        "survey_profile": survey_profile,
        "gender_branch": gender_branch,
        "preference_vector": survey.preference_vector or [],
        "created_at": survey.created_at,
    }


def _record_value(record, key: str, default=None):
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _jsonish(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _serialize_analysis(analysis) -> dict | None:
    if not analysis:
        return None
    image_url = (
        _record_value(analysis, "image_url")
        or _record_value(analysis, "processed_path")
        or _record_value(analysis, "original_image_url")
    )
    landmark_snapshot = (
        _record_value(analysis, "landmark_snapshot")
        or _jsonish(_record_value(analysis, "analysis_landmark_snapshot"), {})
        or _jsonish(_record_value(analysis, "landmark_data"), {})
    )
    return {
        "face_shape": _record_value(analysis, "face_shape") or _record_value(analysis, "face_type"),
        "golden_ratio_score": _record_value(analysis, "golden_ratio_score"),
        "image_url": resolve_storage_reference(image_url),
        "landmark_snapshot": landmark_snapshot,
        "created_at": _record_value(analysis, "created_at"),
    }


def _serialize_capture(record) -> dict:
    privacy_snapshot = _record_value(record, "privacy_snapshot", {}) or {}
    client = _record_value(record, "client")
    original_path = _record_value(record, "original_path")
    processed_path = _record_value(record, "processed_path")
    deidentified_path = _record_value(record, "deidentified_path")
    return {
        "record_id": _record_value(record, "id"),
        "client_id": _record_value(record, "client_id"),
        "legacy_client_id": _record_value(record, "legacy_client_id") or get_legacy_client_id(client=client),
        "status": _record_value(record, "status"),
        "face_count": _record_value(record, "face_count"),
        "landmark_snapshot": _record_value(record, "landmark_snapshot"),
        "deidentified_image_url": resolve_storage_reference(deidentified_path),
        "privacy_snapshot": privacy_snapshot,
        "image_storage_policy": privacy_snapshot.get("storage_policy", "asset_store"),
        "error_note": _record_value(record, "error_note"),
        "original_image_url": resolve_storage_reference(original_path),
        "processed_image_url": resolve_storage_reference(processed_path),
        "storage_snapshot": build_storage_snapshot(
            original_path=original_path,
            processed_path=processed_path,
            deidentified_path=deidentified_path,
        ),
        "created_at": _record_value(record, "created_at"),
        "updated_at": _record_value(record, "updated_at"),
    }


DESIGNER_DIAGNOSIS_HAIR_TEXTURE_CHOICES = {"fine", "medium", "coarse"}
DESIGNER_DIAGNOSIS_DAMAGE_LEVEL_CHOICES = {"level1", "level2", "level3", "level4"}
DESIGNER_DIAGNOSIS_SPECIAL_NOTE_CHOICES = {
    "bleach_history",
    "black_red_cover",
    "natural_curl",
    "self_coloring",
    "head_shape_density",
}


def _default_designer_diagnosis_payload(*, storage_ready: bool = True) -> dict:
    return {
        "hair_texture": "",
        "damage_level": "",
        "special_notes": [],
        "special_memo": "",
        "has_content": False,
        "updated_at": None,
        "updated_by": None,
        "storage_ready": storage_ready,
    }


def _normalize_designer_diagnosis_payload(payload: dict | None) -> dict:
    payload = payload or {}
    hair_texture = str(payload.get("hair_texture") or payload.get("hairTexture") or "").strip()
    damage_level = str(payload.get("damage_level") or payload.get("damageLevel") or "").strip()
    raw_notes = payload.get("special_notes")
    if raw_notes is None:
        raw_notes = payload.get("specialNotes")
    special_notes: list[str] = []
    if isinstance(raw_notes, list):
        for value in raw_notes:
            normalized = str(value or "").strip()
            if normalized in DESIGNER_DIAGNOSIS_SPECIAL_NOTE_CHOICES and normalized not in special_notes:
                special_notes.append(normalized)
    if hair_texture not in DESIGNER_DIAGNOSIS_HAIR_TEXTURE_CHOICES:
        hair_texture = ""
    if damage_level not in DESIGNER_DIAGNOSIS_DAMAGE_LEVEL_CHOICES:
        damage_level = ""
    special_memo = str(payload.get("special_memo") or payload.get("specialMemo") or "").strip()
    return {
        "hair_texture": hair_texture,
        "damage_level": damage_level,
        "special_notes": special_notes,
        "special_memo": special_memo,
    }


def _has_designer_diagnosis_content(payload: dict | None) -> bool:
    diagnosis = _normalize_designer_diagnosis_payload(payload)
    return bool(
        diagnosis["hair_texture"]
        or diagnosis["damage_level"]
        or diagnosis["special_notes"]
        or diagnosis["special_memo"]
    )


def _serialize_designer_diagnosis_editor(*, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict | None:
    if designer is not None:
        return {
            "role": "designer",
            "id": designer.id,
            "legacy_id": get_legacy_designer_id(designer=designer),
            "name": designer.name,
        }
    if admin is not None:
        return {
            "role": "admin",
            "id": admin.id,
            "legacy_id": get_legacy_admin_id(admin=admin),
            "name": admin.name,
        }
    return None


def _fetch_designer_diagnosis_card(*, client: "Client") -> tuple["DesignerDiagnosisCard | None", bool]:
    DesignerDiagnosisCard = _get_runtime_model("DesignerDiagnosisCard")

    client_ref_id = getattr(client, "id", client)
    legacy_client_ref_id = get_legacy_client_id(client=client)
    filters = Q(client_ref_id=client_ref_id)
    if legacy_client_ref_id:
        filters |= Q(legacy_client_ref_id=legacy_client_ref_id)
    try:
        card = DesignerDiagnosisCard.objects.filter(filters).first()
    except (OperationalError, ProgrammingError):
        return None, False
    return card, True


def _serialize_designer_diagnosis_card(card: "DesignerDiagnosisCard | None", *, storage_ready: bool = True) -> dict:
    if not storage_ready:
        return _default_designer_diagnosis_payload(storage_ready=False)
    if card is None:
        return _default_designer_diagnosis_payload()
    admin = get_admin_by_identifier(identifier=card.admin_ref_id) if card.admin_ref_id else None
    designer = get_designer_by_identifier(identifier=card.designer_ref_id) if card.designer_ref_id else None
    return {
        "hair_texture": card.hair_texture or "",
        "damage_level": card.damage_level or "",
        "special_notes": list(card.special_notes or []),
        "special_memo": card.special_memo or "",
        "has_content": _has_designer_diagnosis_content({
            "hair_texture": card.hair_texture,
            "damage_level": card.damage_level,
            "special_notes": card.special_notes,
            "special_memo": card.special_memo,
        }),
        "updated_at": card.updated_at,
        "updated_by": _serialize_designer_diagnosis_editor(admin=admin, designer=designer),
        "storage_ready": True,
    }


def _default_customer_profile_note_payload(*, storage_ready: bool = True) -> dict:
    return {
        "content": "",
        "has_content": False,
        "updated_at": None,
        "updated_by": None,
        "storage_ready": storage_ready,
    }


def _has_customer_profile_note_content(payload: dict | None) -> bool:
    payload = payload or {}
    content = str(payload.get("content") or "").strip()
    return bool(content)


def _fetch_customer_profile_note(*, client: "Client") -> tuple["ClientProfileNote | None", bool]:
    ClientProfileNote = _get_runtime_model("ClientProfileNote")

    client_ref_id = getattr(client, "id", client)
    legacy_client_ref_id = get_legacy_client_id(client=client)
    filters = Q(client_ref_id=client_ref_id)
    if legacy_client_ref_id:
        filters |= Q(legacy_client_ref_id=legacy_client_ref_id)
    try:
        note = ClientProfileNote.objects.filter(filters).first()
    except (OperationalError, ProgrammingError):
        return None, False
    return note, True


def _serialize_customer_profile_note(note: "ClientProfileNote | None", *, storage_ready: bool = True) -> dict:
    if not storage_ready:
        return _default_customer_profile_note_payload(storage_ready=False)
    if note is None:
        return _default_customer_profile_note_payload()
    admin = get_admin_by_identifier(identifier=note.admin_ref_id) if note.admin_ref_id else None
    designer = get_designer_by_identifier(identifier=note.designer_ref_id) if note.designer_ref_id else None
    return {
        "content": note.content or "",
        "has_content": _has_customer_profile_note_content({"content": note.content}),
        "updated_at": note.updated_at,
        "updated_by": _serialize_designer_diagnosis_editor(admin=admin, designer=designer),
        "storage_ready": True,
    }


def _serialize_active_consultation_payload(
    *,
    client: "Client",
    latest_consultation: "ConsultationRequest | None" = None,
    legacy_active_consultation: dict | None = None,
) -> dict | None:
    selected_style_payload = {
        "selected_style_id": None,
        "selected_style_name": None,
        "selected_style_image_url": None,
        "selected_style_score": None,
        "selected_style_description": None,
        "selected_recommendation_id": None,
    }
    if latest_consultation is not None:
        return {
            "consultation_id": latest_consultation.id,
            "legacy_client_id": get_legacy_client_id(client=client),
            "status": latest_consultation.status,
            "is_active": latest_consultation.is_active,
            "is_read": latest_consultation.is_read,
            "source": latest_consultation.source,
            "designer_id": latest_consultation.designer_id,
            "legacy_designer_id": (
                get_legacy_designer_id(designer=latest_consultation.designer)
                if latest_consultation.designer_id and latest_consultation.designer
                else None
            ),
            "designer_name": (
                latest_consultation.designer.name
                if latest_consultation.designer_id and latest_consultation.designer
                else None
            ),
            "created_at": latest_consultation.created_at,
            "closed_at": latest_consultation.closed_at,
            **selected_style_payload,
        }
    if legacy_active_consultation is not None:
        return {
            "consultation_id": legacy_active_consultation["consultation_id"],
            "legacy_client_id": legacy_active_consultation.get("legacy_client_id"),
            "status": legacy_active_consultation["status"],
            "is_active": legacy_active_consultation["is_active"],
            "is_read": not legacy_active_consultation["has_unread_consultation"],
            "source": None,
            "designer_id": legacy_active_consultation["designer_id"],
            "legacy_designer_id": legacy_active_consultation.get("legacy_designer_id"),
            "designer_name": legacy_active_consultation["designer_name"],
            "created_at": legacy_active_consultation["last_activity_at"],
            "closed_at": None,
            "selected_style_id": legacy_active_consultation.get("selected_style_id"),
            "selected_style_name": legacy_active_consultation.get("selected_style_name"),
            "selected_style_image_url": legacy_active_consultation.get("selected_style_image_url"),
            "selected_style_score": legacy_active_consultation.get("selected_style_score"),
            "selected_style_description": legacy_active_consultation.get("selected_style_description"),
            "selected_recommendation_id": legacy_active_consultation.get("selected_recommendation_id"),
        }
    return None


def _build_session_status_payload(*, is_active: bool, diagnosis_storage_ready: bool = True) -> dict:
    return {
        "is_active": bool(is_active),
        "can_write_designer_diagnosis": bool(diagnosis_storage_ready),
        "customer_note_scope": "client",
    }


def _reanalysis_block_message(reason: str | None) -> str | None:
    if not reason:
        return None
    messages = {
        "reusable_preference_missing": "재분석에 필요한 정보가 아직 준비되지 않았습니다.",
        "consultation_started": "상담이 시작된 고객은 다시 분석할 수 없습니다.",
        "recommendation_already_selected": "이미 추천이 선택된 고객입니다. 먼저 선택 상태를 정리해 주세요.",
        "retry_already_used": "재분석 재시도는 이미 사용되었습니다.",
        "initial_recommendations_missing": "초기 추천이 아직 준비되지 않았습니다.",
        "legacy_result_only": "이 고객은 이전 결과만 있어 재분석할 수 없습니다.",
    }
    return messages.get(reason) or "현재 상태에서는 재분석할 수 없습니다."


def get_client_designer_diagnosis(*, client: "Client", admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    client_ref_id = getattr(client, "id", client)
    legacy_client_ref_id = get_legacy_client_id(client=client)
    _ensure_client_in_scope(client=client, admin=admin, designer=designer)

    cache_key = _partner_lookup_cache_key(
        "partner-client-diagnosis",
        admin=admin,
        designer=designer,
        client=client,
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    card, storage_ready = _fetch_designer_diagnosis_card(client=client)
    has_active_consultation = bool(get_legacy_active_consultation_items(admin=admin, designer=designer, client=client))
    payload = {
        "status": "ready",
        "client_id": client_ref_id,
        "legacy_client_id": legacy_client_ref_id,
        "designer_diagnosis": _serialize_designer_diagnosis_card(card, storage_ready=storage_ready),
        "session_status": _build_session_status_payload(
            is_active=has_active_consultation,
            diagnosis_storage_ready=storage_ready,
        ),
    }
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_LOOKUP_CACHE_SECONDS",
        default_timeout=45,
    )


def upsert_client_designer_diagnosis(*, client: "Client", diagnosis_state: dict | None, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    client_ref_id = getattr(client, "id", client)
    legacy_client_ref_id = get_legacy_client_id(client=client)
    _ensure_client_in_scope(client=client, admin=admin, designer=designer)

    normalized_state = _normalize_designer_diagnosis_payload(diagnosis_state)
    DesignerDiagnosisCard = _get_runtime_model("DesignerDiagnosisCard")

    filters = Q(client_ref_id=client_ref_id)
    if legacy_client_ref_id:
        filters |= Q(legacy_client_ref_id=legacy_client_ref_id)
    try:
        card = DesignerDiagnosisCard.objects.filter(filters).first()
    except (OperationalError, ProgrammingError) as exc:
        raise ValueError("디자이너 진단 카드 저장 테이블이 아직 준비되지 않았습니다. 마이그레이션 적용 후 다시 시도해 주세요.") from exc

    if not _has_designer_diagnosis_content(normalized_state):
        if card is not None:
            card.delete()
        _invalidate_partner_client_payloads(client=client, admin=admin, designer=designer)
        return {
            "status": "success",
            "client_id": client_ref_id,
            "legacy_client_id": legacy_client_ref_id,
            "designer_diagnosis": _serialize_designer_diagnosis_card(None),
        }

    editor_admin = admin or (designer.shop if designer is not None else None)
    editor_designer = designer
    editor_admin_id = getattr(editor_admin, "id", editor_admin) if editor_admin is not None else None
    editor_designer_id = getattr(editor_designer, "id", editor_designer) if editor_designer is not None else None

    if card is None:
        card = DesignerDiagnosisCard.objects.create(
            client_ref_id=client_ref_id,
            legacy_client_ref_id=legacy_client_ref_id,
            admin_ref_id=editor_admin_id,
            designer_ref_id=editor_designer_id,
            hair_texture=normalized_state["hair_texture"],
            damage_level=normalized_state["damage_level"],
            special_notes=normalized_state["special_notes"],
            special_memo=normalized_state["special_memo"],
        )
    else:
        card.client_ref_id = client_ref_id
        card.legacy_client_ref_id = legacy_client_ref_id
        card.admin_ref_id = editor_admin_id
        card.designer_ref_id = editor_designer_id
        card.hair_texture = normalized_state["hair_texture"]
        card.damage_level = normalized_state["damage_level"]
        card.special_notes = normalized_state["special_notes"]
        card.special_memo = normalized_state["special_memo"]
        card.save()

    _invalidate_partner_client_payloads(client=client, admin=admin, designer=designer)
    has_active_consultation = bool(get_legacy_active_consultation_items(admin=admin, designer=designer, client=client))
    return {
        "status": "success",
        "client_id": client_ref_id,
        "legacy_client_id": legacy_client_ref_id,
        "designer_diagnosis": _serialize_designer_diagnosis_card(card),
        "session_status": _build_session_status_payload(
            is_active=has_active_consultation,
            diagnosis_storage_ready=True,
        ),
    }


def _report_image_url(reference: str | None) -> str | None:
    text = str(reference or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://", "/", "data:image/")):
        return text
    return None


def _style_record_map(style_ids) -> dict[int, object]:
    normalized_ids: set[int] = set()
    for style_id in style_ids or []:
        try:
            normalized_ids.add(int(style_id))
        except (TypeError, ValueError):
            continue

    if not normalized_ids:
        return {}

    rows = (
        LegacyHairstyle.objects.filter(
            Q(hairstyle_id__in=normalized_ids) | Q(backend_style_id__in=normalized_ids)
        )
        .only(
            "hairstyle_id",
            "backend_style_id",
            "style_name",
            "name",
            "image_url",
            "description",
            "vibe",
        )
        .order_by("-backend_style_id", "-hairstyle_id")
    )
    records: dict[int, object] = {}
    for row in rows:
        candidate_ids = []
        for candidate in (getattr(row, "backend_style_id", None), getattr(row, "hairstyle_id", None)):
            if candidate in (None, ""):
                continue
            try:
                candidate_ids.append(int(candidate))
            except (TypeError, ValueError):
                continue
        for candidate_id in candidate_ids:
            if candidate_id in normalized_ids and candidate_id not in records:
                records[candidate_id] = row
    return records


def _style_snapshot(style_id: int, *, resolve_image: bool = True, style=None) -> dict:
    profile = STYLE_PROFILE_BY_ID.get(style_id)
    style = style or get_style_record(style_id=style_id)

    if not style and not profile:
        return {
            "style_id": style_id,
            "style_name": f"Style {style_id}",
            "image_url": None,
            "description": "",
            "keywords": [],
        }

    style_name = (
        getattr(style, "name", None)
        or getattr(style, "style_name", None)
        or (profile.fallback_name if profile else None)
        or f"Style {style_id}"
    )
    style_image_url = getattr(style, "image_url", None) or (profile.fallback_sample_image_url if profile else None)
    style_description = getattr(style, "description", None) or (profile.fallback_description if profile else "") or ""
    style_vibe = getattr(style, "vibe", None)
    normalized_style_id = (
        getattr(style, "backend_style_id", None)
        or getattr(style, "hairstyle_id", None)
        or getattr(style, "id", None)
        or style_id
    )
    keywords = list(profile.keywords) if profile else ([style_vibe] if style_vibe else [])
    return {
        "style_id": normalized_style_id,
        "style_name": style_name,
        "image_url": (
            resolve_storage_reference(style_image_url)
            if resolve_image
            else _report_image_url(style_image_url)
        ),
        "description": style_description,
        "keywords": keywords,
    }


def _serialize_recommendation(row: "FormerRecommendation | dict") -> dict:
    if isinstance(row, dict):
        reasoning_snapshot = row.get("reasoning_snapshot") or {}
        return {
            "recommendation_id": row.get("recommendation_id") or row.get("id"),
            "client_id": row.get("client_id"),
            "legacy_client_id": row.get("legacy_client_id"),
            "batch_id": row.get("batch_id"),
            "source": row.get("source"),
            "style_id": row.get("style_id"),
            "style_name": row.get("style_name"),
            "style_description": row.get("style_description") or "",
            "keywords": list(row.get("keywords") or []),
            "sample_image_url": row.get("sample_image_url"),
            "simulation_image_url": row.get("simulation_image_url"),
            "synthetic_image_url": row.get("synthetic_image_url") or row.get("simulation_image_url"),
            "llm_explanation": row.get("llm_explanation") or "",
            "reasoning": row.get("reasoning") or reasoning_snapshot.get("summary") or row.get("llm_explanation") or "",
            "reasoning_snapshot": reasoning_snapshot,
            "image_policy": row.get("image_policy") or "legacy_asset_store",
            "can_regenerate_simulation": bool(row.get("can_regenerate_simulation")),
            "regeneration_remaining_count": int(row.get("regeneration_remaining_count") or 0),
            "regeneration_policy": row.get("regeneration_policy"),
            "match_score": row.get("match_score"),
            "rank": row.get("rank"),
            "is_chosen": bool(row.get("is_chosen")),
            "created_at": row.get("created_at"),
        }
    payload = serialize_recommendation_row(row)
    payload["client_id"] = row.client_id
    payload["legacy_client_id"] = get_legacy_client_id(client=row.client)
    return payload


def _serialize_style_selection(selection: "StyleSelection | dict") -> dict:
    if isinstance(selection, dict):
        style_id = int(selection.get("style_id") or 0)
        style_snapshot = _style_snapshot(style_id)
        return {
            "selection_id": (
                selection.get("selection_id")
                or selection.get("backend_selection_id")
                or selection.get("result_id")
                or selection.get("selected_recommendation_id")
            ),
            "client_id": selection.get("client_id"),
            "legacy_client_id": selection.get("legacy_client_id"),
            "style_id": style_id,
            "style_name": selection.get("style_name") or style_snapshot["style_name"],
            "image_url": selection.get("image_url") or style_snapshot["image_url"],
            "description": selection.get("style_description") or style_snapshot["description"],
            "source": selection.get("source"),
            "match_score": selection.get("match_score") or selection.get("selection_count"),
            "is_sent_to_admin": bool(
                selection.get("is_sent_to_admin")
                or selection.get("is_active")
                or selection.get("is_confirmed")
            ),
            "created_at": selection.get("created_at") or selection.get("last_activity_at"),
        }

    style_snapshot = _style_snapshot(selection.style_id)
    return {
        "selection_id": selection.id,
        "client_id": selection.client_id,
        "legacy_client_id": get_legacy_client_id(client=selection.client),
        "style_id": selection.style_id,
        "style_name": style_snapshot["style_name"],
        "image_url": style_snapshot["image_url"],
        "description": style_snapshot["description"],
        "source": selection.source,
        "match_score": selection.match_score,
        "is_sent_to_admin": selection.is_sent_to_admin,
        "created_at": selection.created_at,
    }


def _serialize_consultation_like(row: "ConsultationRequest | dict") -> dict:
    if isinstance(row, dict):
        return {
            "consultation_id": row.get("consultation_id") or row.get("id"),
            "client_id": row.get("client_id"),
            "legacy_client_id": row.get("legacy_client_id"),
            "client_name": row.get("client_name"),
            "phone": row.get("phone"),
            "status": row.get("status"),
            "has_unread_consultation": bool(row.get("has_unread_consultation")),
            "designer_id": row.get("designer_id"),
            "legacy_designer_id": row.get("legacy_designer_id"),
            "designer_name": row.get("designer_name"),
            "selected_style_name": row.get("selected_style_name"),
            "recommendation_count": row.get("recommendation_count"),
            "created_at": row.get("created_at") or row.get("last_activity_at"),
            "last_activity_at": row.get("last_activity_at") or row.get("created_at"),
            "closed_at": row.get("closed_at"),
            "is_active": bool(row.get("is_active")),
            "is_read": not bool(row.get("has_unread_consultation")),
            "source": row.get("source"),
        }

    recommendation_count = 1 if row.selected_recommendation else 0
    created_at = row.created_at
    return {
        "consultation_id": row.id,
        "client_id": row.client_id,
        "legacy_client_id": get_legacy_client_id(client=row.client),
        "client_name": row.client.name,
        "phone": row.client.phone,
        "status": row.status,
        "has_unread_consultation": not row.is_read,
        "designer_id": row.designer_id or row.client.designer_id,
        "legacy_designer_id": (
            get_legacy_designer_id(designer=row.designer)
            if row.designer_id and row.designer
            else (
                get_legacy_designer_id(designer=row.client.designer)
                if row.client.designer_id and row.client.designer
                else None
            )
        ),
        "designer_name": (
            row.designer.name
            if row.designer_id and row.designer
            else (row.client.designer.name if row.client.designer_id and row.client.designer else None)
        ),
        "selected_style_name": row.selected_style.name if row.selected_style else None,
        "recommendation_count": recommendation_count,
        "created_at": created_at,
        "last_activity_at": created_at,
        "closed_at": row.closed_at,
        "is_active": row.is_active,
        "is_read": row.is_read,
        "source": row.source,
    }


def _sync_legacy_consultation_status(
    *,
    client: "Client",
    consultation_id: int,
    status_value: str,
    is_active: bool,
    is_read: bool,
    closed_at=None,
) -> None:
    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        return
    LegacyClientResult.objects.filter(
        client_id=legacy_client_id,
    ).filter(
        Q(backend_consultation_id=consultation_id) | Q(result_id=consultation_id)
    ).update(
        status=status_value,
        is_active=is_active,
        is_read=is_read,
        closed_at=closed_at,
    )


def _serialize_admin_profile(admin: "AdminAccount") -> dict:
    formatted_business_number = (
        _format_business_number(admin.business_number)
        if len(admin.business_number) == 10 and admin.business_number.isdigit()
        else admin.business_number
    )
    return {
        "admin_id": admin.id,
        "legacy_admin_id": get_legacy_admin_id(admin=admin),
        "name": admin.name,
        "store_name": admin.store_name,
        "role": admin.role,
        "phone": admin.phone,
        "business_number": formatted_business_number,
        "consent_snapshot": admin.consent_snapshot or {},
        "consented_at": admin.consented_at,
        "is_active": admin.is_active,
        "created_at": admin.created_at,
    }


def _serialize_designer_profile(designer: "Designer | None") -> dict | None:
    if designer is None:
        return None
    return {
        "designer_id": designer.id,
        "legacy_designer_id": get_legacy_designer_id(designer=designer),
        "name": designer.name,
        "shop_id": designer.shop_id,
        "shop_name": designer.shop.store_name,
        "phone": designer.phone,
        "is_active": designer.is_active,
        "created_at": designer.created_at,
    }


def _client_age_fields(client: "Client") -> dict:
    profile = build_client_age_profile(client) or {}
    return {
        "age": profile.get("current_age"),
        "age_decade": profile.get("age_decade"),
        "age_segment": profile.get("age_segment"),
        "age_group": profile.get("age_group"),
    }


def _serialize_client_summary(client: "Client") -> dict:
    return {
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "name": client.name,
        "gender": client.gender,
        "phone": client.phone,
        "shop_id": client.shop_id,
        "shop_name": client.shop.store_name if client.shop_id and client.shop else None,
        "designer": _serialize_designer_profile(client.designer),
        **_client_age_fields(client),
        "created_at": client.created_at,
    }


def _prime_scope_caches(
    *,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> tuple[dict[str, object], dict[str, object]]:
    admin_cache: dict[str, object] = {}
    designer_cache: dict[str, object] = {}

    if admin is not None:
        backend_admin_id = get_backend_admin_id(admin=admin)
        if backend_admin_id is not None:
            admin_cache[f"backend:{backend_admin_id}"] = admin
        legacy_admin_id = get_legacy_admin_id(admin=admin)
        if legacy_admin_id:
            admin_cache[f"legacy:{legacy_admin_id}"] = admin

    if designer is not None:
        backend_designer_id = get_backend_designer_id(designer=designer)
        if backend_designer_id is not None:
            designer_cache[f"backend:{backend_designer_id}"] = designer
        legacy_designer_id = get_legacy_designer_id(designer=designer)
        if legacy_designer_id:
            designer_cache[f"legacy:{legacy_designer_id}"] = designer
        if getattr(designer, "shop", None) is not None:
            designer_shop_backend_id = get_backend_admin_id(admin=designer.shop)
            if designer_shop_backend_id is not None:
                admin_cache[f"backend:{designer_shop_backend_id}"] = designer.shop
            legacy_admin_id = get_legacy_admin_id(admin=designer.shop)
            if legacy_admin_id:
                admin_cache[f"legacy:{legacy_admin_id}"] = designer.shop

    return admin_cache, designer_cache


def _client_is_in_scope(
    *,
    client: "Client",
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> bool:
    if designer is not None:
        backend_designer_id = get_backend_designer_id(designer=designer)
        return backend_designer_id is not None and getattr(client, "designer_id", None) == backend_designer_id
    if admin is not None:
        backend_admin_id = get_backend_admin_id(admin=admin)
        return backend_admin_id is not None and getattr(client, "shop_id", None) == backend_admin_id
    return True


def _ensure_client_in_scope(
    *,
    client: "Client",
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> None:
    if _client_is_in_scope(client=client, admin=admin, designer=designer):
        return

    client_id = getattr(client, "id", None)
    scoped_client_ids = _scoped_client_ids(admin=admin, designer=designer)
    if client_id is not None and client_id in scoped_client_ids:
        return

    raise ValueError("Client is outside the current admin scope.")


def _scoped_client_records(
    *,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
    query: str = "",
    limit: int | None = None,
) -> list["Client"]:
    if designer is not None:
        backend_designer_id = get_backend_designer_id(designer=designer)
        rows = (
            LegacyClient.objects.filter(backend_designer_ref_id=backend_designer_id)
            if backend_designer_id is not None
            else LegacyClient.objects.none()
        )
    elif admin is not None:
        backend_admin_id = get_backend_admin_id(admin=admin)
        legacy_admin_id = get_legacy_admin_id(admin=admin)
        scope_filter = Q()
        if backend_admin_id is not None:
            scope_filter |= Q(backend_shop_ref_id=backend_admin_id)
        if legacy_admin_id:
            scope_filter |= Q(shop_id=legacy_admin_id)
        rows = LegacyClient.objects.filter(scope_filter) if scope_filter else LegacyClient.objects.none()
    else:
        rows = LegacyClient.objects.all()

    if query:
        rows = rows.filter(
            Q(name__icontains=query)
            | Q(client_name__icontains=query)
            | Q(phone__icontains=query)
        )

    rows = rows.order_by("name", "client_name", "client_id")
    if limit is not None:
        rows = rows[: max(1, int(limit))]

    rows = list(rows)
    admin_cache, designer_cache = _prime_scope_caches(admin=admin, designer=designer)
    designer_ids = sorted(
        {
            int(row.backend_designer_ref_id)
            for row in rows
            if row.backend_designer_ref_id
        }
    )
    if designer_ids:
        for designer_row in LegacyDesigner.objects.filter(
            backend_designer_id__in=designer_ids,
            is_active=True,
        ):
            _designer_from_legacy_row(
                designer_row,
                admin_cache=admin_cache,
                designer_cache=designer_cache,
            )

    records: list["Client"] = []
    for row in rows:
        client = _client_from_legacy_row(
            row,
            admin_cache=admin_cache,
            designer_cache=designer_cache,
        )
        if client is not None:
            records.append(client)
    return records


def _scoped_client_ids(*, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> set[int]:
    scoped_ids = get_scoped_client_ids(admin=admin, designer=designer)
    if scoped_ids is not None:
        return set(scoped_ids)
    return {client.id for client in _scoped_client_records(admin=admin, designer=designer)}


def get_admin_profile(*, admin: "AdminAccount") -> dict:
    return {
        "status": "success",
        "admin": _serialize_admin_profile(admin),
    }


def register_admin(*, payload: dict) -> dict:
    phone = _normalize_phone(payload["phone"])
    business_number = _normalize_business_number(payload["business_number"])
    consent_snapshot = {
        "agree_terms": bool(payload.get("agree_terms")),
        "agree_privacy": bool(payload.get("agree_privacy")),
        "agree_third_party_sharing": bool(payload.get("agree_third_party_sharing")),
        "agree_marketing": bool(payload.get("agree_marketing", False)),
    }

    required_values = [
        ("대표자 성함", (payload.get("name") or "").strip()),
        ("매장명", (payload.get("store_name") or "").strip()),
        ("관리자 연락처", phone),
        ("사업자등록번호", business_number),
        ("비밀번호", payload.get("password") or ""),
    ]
    for label, value in required_values:
        if not value:
            raise ValueError(_required_field_message(label))

    if not _is_valid_mobile_phone(phone):
        raise ValueError("관리자 연락처는 휴대폰 번호(010-0000-0000)로 입력해 주세요.")

    required_consents = [
        ("이용약관 동의", consent_snapshot["agree_terms"]),
        ("개인정보 수집 및 이용 동의", consent_snapshot["agree_privacy"]),
        ("제3자 제공 동의", consent_snapshot["agree_third_party_sharing"]),
    ]
    for label, is_checked in required_consents:
        if not is_checked:
            raise ValueError(_required_field_message(label))

    if admin_exists_by_phone(phone=phone):
        raise ValueError("이미 등록된 관리자 연락처입니다.")
    if not _is_valid_business_number(business_number):
        raise ValueError("유효하지 않은 사업자등록번호입니다.")
    if admin_exists_by_business_number(business_numbers=_business_number_variants(business_number)):
        raise ValueError("이미 등록된 사업자등록번호입니다.")

    admin = create_admin_record(
        name=payload["name"],
        store_name=payload["store_name"],
        role=payload.get("role", "owner"),
        phone=phone,
        business_number=business_number,
        password_hash=make_password(payload["password"]),
        consent_snapshot=consent_snapshot,
        consented_at=timezone.now(),
    )
    return {
        "status": "success",
        "admin_id": admin.id,
        "admin": _serialize_admin_profile(admin),
        **issue_admin_token_pair(admin=admin),
    }


def login_admin(*, phone: str, password: str) -> dict:
    phone = _normalize_phone(phone)
    admin = get_admin_by_phone(phone=phone)
    if not admin or not check_password(password, admin.password_hash):
        raise ValueError("관리자 계정 정보를 다시 확인해 주세요.")
    return {
        "status": "success",
        "admin": _serialize_admin_profile(admin),
        **issue_admin_token_pair(admin=admin),
    }


def _today_client_ids(*, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> set[int]:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    legacy_activity = get_legacy_activity_client_map_by_day(
        start_date=start.date(),
        days=1,
        admin=admin,
        designer=designer,
    )
    if legacy_activity is not None:
        ids: set[int] = set()
        for client_identifier in legacy_activity.get(start.date().isoformat(), set()):
            resolved = get_client_by_identifier(identifier=client_identifier)
            if resolved is not None:
                ids.add(resolved.id)
        return ids
    return set()


def _latest_active_consultations(*, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> list["ConsultationRequest"]:
    return get_legacy_active_consultation_items(admin=admin, designer=designer) or []


def _consultation_bridge_payload(
    *,
    consultation_id: int,
    client: "Client",
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
    legacy_item: dict | None = None,
    created_at=None,
) -> dict:
    legacy_item = legacy_item or {}
    resolved_admin = admin or client.shop
    resolved_designer = designer
    if resolved_designer is None and legacy_item.get("designer_id"):
        resolved_designer = get_designer_by_identifier(identifier=legacy_item["designer_id"])
    if resolved_designer is None:
        resolved_designer = client.designer
    created_at = created_at or legacy_item.get("last_activity_at") or timezone.now()
    return {
        "id": int(consultation_id),
        "consultation_id": int(consultation_id),
        "client": client,
        "client_id": client.id,
        "admin": resolved_admin,
        "admin_id": getattr(resolved_admin, "id", None),
        "designer": resolved_designer,
        "designer_id": getattr(resolved_designer, "id", None),
        "source": legacy_item.get("source") or "legacy_result",
        "survey_snapshot": json.loads(
            json.dumps(_serialize_survey(get_latest_survey(client)), ensure_ascii=False, default=str)
        ),
        "analysis_data_snapshot": json.loads(
            json.dumps(_serialize_analysis(get_latest_analysis(client)), ensure_ascii=False, default=str)
        ),
        "status": legacy_item.get("status") or "PENDING",
        "is_active": bool(legacy_item.get("is_active", True)),
        "is_read": not bool(legacy_item.get("has_unread_consultation")),
        "closed_at": legacy_item.get("closed_at"),
        "created_at": created_at,
    }


def _ensure_consultation_bridge_row(*, consultation_payload: dict) -> None:
    return None


def _ensure_admin_bridge_row(*, admin: "AdminAccount | None") -> None:
    return None


def _ensure_designer_bridge_row(*, designer: "Designer | None") -> None:
    return None


def _ensure_client_bridge_row(*, client: "Client") -> None:
    return None


def _update_consultation_bridge_row(*, consultation_id: int, **fields) -> None:
    return None


def _fetch_note_rows(*, client: "Client", limit: int = 20) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, consultation_id, admin_id, designer_id, content, created_at
            FROM client_session_notes
            WHERE client_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            [client.id, int(limit)],
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _count_note_rows(*, client: "Client") -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM client_session_notes
            WHERE client_id = %s
            """,
            [client.id],
        )
        row = cursor.fetchone()
    return int(row[0] if row else 0)


def _create_note_row(
    *,
    consultation_id: int,
    client_id: int,
    admin_id: int | None,
    designer_id: int | None,
    content: str,
) -> int:
    returning_supported = connection.vendor == "postgresql"
    with connection.cursor() as cursor:
        if returning_supported:
            cursor.execute(
                """
                INSERT INTO client_session_notes (
                    consultation_id, client_id, admin_id, designer_id, content, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [consultation_id, client_id, admin_id, designer_id, content, timezone.now()],
            )
            row = cursor.fetchone()
            return int(row[0])

        cursor.execute(
            """
            INSERT INTO client_session_notes (
                consultation_id, client_id, admin_id, designer_id, content, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [consultation_id, client_id, admin_id, designer_id, content, timezone.now()],
        )
        cursor.execute("SELECT last_insert_rowid()")
        row = cursor.fetchone()
        return int(row[0])


def _measure_step(timings: dict[str, int], key: str, callback):
    started_at = time.perf_counter()
    result = callback()
    timings[key] = max(0, int((time.perf_counter() - started_at) * 1000))
    return result


def _serialize_note_rows(notes: list[dict]) -> list[dict]:
    return [
        {
            "note_id": note["id"],
            "consultation_id": note["consultation_id"],
            "admin_id": note["admin_id"],
            "admin_name": (
                getattr(get_admin_by_identifier(identifier=note["admin_id"]), "name", None)
                if note.get("admin_id") else None
            ),
            "designer_id": note["designer_id"],
            "designer_name": (
                getattr(get_designer_by_identifier(identifier=note["designer_id"]), "name", None)
                if note.get("designer_id") else None
            ),
            "content": note["content"],
            "created_at": note["created_at"],
        }
        for note in notes
    ]


def _get_client_history_payload(
    *,
    client: "Client",
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
    limit: int = 20,
) -> dict:
    timings_ms: dict[str, int] = {}
    note_rows = _measure_step(
        timings_ms,
        "notes",
        lambda: _fetch_note_rows(client=client, limit=limit),
    )
    analysis_history, capture_history = _measure_step(
        timings_ms,
        "analysis_capture_history",
        lambda: get_legacy_analysis_capture_history(client=client, limit=limit),
    )
    style_selection_history = _measure_step(
        timings_ms,
        "style_selection_history",
        lambda: get_legacy_confirmed_selection_items(admin=admin, designer=designer, client=client) or [],
    )
    legacy_recommendation_items = _measure_step(
        timings_ms,
        "recommendation_history",
        lambda: get_legacy_former_recommendation_items(client=client) or [],
    )
    chosen_recommendation_history = [
        _serialize_recommendation(row)
        for row in legacy_recommendation_items
        if row.get("is_chosen")
    ]
    return {
        "capture_history": [_serialize_capture(record) for record in capture_history],
        "analysis_history": [_serialize_analysis(analysis) for analysis in analysis_history],
        "style_selection_history": [
            _serialize_style_selection(selection)
            for selection in style_selection_history
        ],
        "chosen_recommendation_history": chosen_recommendation_history,
        "notes": _serialize_note_rows(note_rows),
        "history": {
            "deferred": False,
            "source": "legacy_bridge",
            "limit": int(limit),
            "timings_ms": timings_ms,
            "counts": {
                "captures": len(capture_history),
                "analyses": len(analysis_history),
                "notes": len(note_rows),
                "style_selections": len(style_selection_history),
                "chosen_recommendations": len(chosen_recommendation_history),
            },
        },
    }


def _build_consultation_bridge_from_legacy_item(
    *,
    legacy_item: dict,
    client: "Client",
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> "ConsultationRequest":
    consultation_id = legacy_item.get("consultation_id")
    if consultation_id in (None, ""):
        raise ValueError("The consultation session could not be found.")
    payload = _consultation_bridge_payload(
        consultation_id=int(consultation_id),
        client=client,
        admin=admin,
        designer=designer,
        legacy_item=legacy_item,
    )
    _ensure_client_bridge_row(client=client)
    try:
        _ensure_consultation_bridge_row(consultation_payload=payload)
    except IntegrityError:
        pass
    return payload


def _resolve_consultation_bridge(
    *,
    consultation_id: int,
    client: "Client | None" = None,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> "ConsultationRequest | None":
    legacy_items = get_legacy_active_consultation_items(admin=admin, designer=designer, client=client) or []
    for legacy_item in legacy_items:
        if int(legacy_item.get("consultation_id") or 0) != int(consultation_id):
            continue
        resolved_client = client
        if resolved_client is None:
            resolved_client = get_client_by_identifier(
                identifier=legacy_item.get("client_id") or legacy_item.get("legacy_client_id")
            )
        if resolved_client is None:
            return None
        return _build_consultation_bridge_from_legacy_item(
            legacy_item=legacy_item,
            client=resolved_client,
            admin=admin,
            designer=designer,
        )
    return None


def _selection_matches_payload(*, survey_snapshot: dict | None, age_profile: dict | None, filters: dict) -> bool:
    survey_snapshot = survey_snapshot or {}
    age_profile = age_profile or {}
    for key, value in filters.items():
        if value in (None, ""):
            continue
        if key == "age_decade":
            if age_profile.get("age_decade") != value:
                return False
            continue
        if key == "age_segment":
            if age_profile.get("age_segment") != value:
                return False
            continue
        if key == "age_group":
            if age_profile.get("age_group") != value:
                return False
            continue
        if survey_snapshot.get(key) != value:
            return False
    return True


def get_admin_dashboard_summary(*, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    cache_key = _partner_lookup_cache_key(
        "partner-dashboard-summary",
        admin=admin,
        designer=designer,
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    styles_by_id = ensure_catalog_styles()
    legacy_selection_items = get_legacy_confirmed_selection_items(
        since=start,
        admin=admin,
        designer=designer,
    ) or []
    counter = Counter(item["style_id"] for item in legacy_selection_items)
    representative = {}
    for item in legacy_selection_items:
        representative.setdefault(item["style_id"], item)
    top_styles = []
    for style_id, selection_count in counter.most_common(5):
        item = representative.get(style_id, {})
        style = styles_by_id.get(style_id) or get_style_record(style_id=style_id)
        top_styles.append(
            {
                "style_id": style_id,
                "style_name": item.get("style_name") or (getattr(style, "name", None) if style else f"Style {style_id}"),
                "image_url": resolve_storage_reference(item.get("image_url") or (getattr(style, "image_url", None) if style else None)),
                "selection_count": selection_count,
            }
        )
    confirmed_styles_count = len(legacy_selection_items)

    legacy_active_items = get_legacy_active_consultation_items(admin=admin, designer=designer) or []
    active_preview = [_serialize_consultation_like(row) for row in legacy_active_items[:5]]
    active_consultation_count = len(legacy_active_items)
    pending_consultation_count = sum(1 for row in legacy_active_items if row["has_unread_consultation"])
    unique_visitors = len(
        {
            row["client_id"] or row["legacy_client_id"]
            for row in [*legacy_active_items, *legacy_selection_items]
            if row.get("client_id") or row.get("legacy_client_id")
        }
    )

    payload = {
        "status": "ready",
        "ai_engine": _ai_health(),
        "today_metrics": {
            "unique_visitors": unique_visitors,
            "active_clients": active_consultation_count,
            "pending_consultations": pending_consultation_count,
            "confirmed_styles": confirmed_styles_count,
        },
        "top_styles_today": top_styles,
        "active_clients_preview": active_preview,
    }
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_DASHBOARD_CACHE_SECONDS",
        default_timeout=30,
    )


def get_active_client_sessions(*, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    cache_key = _partner_lookup_cache_key(
        "partner-active-client-sessions",
        admin=admin,
        designer=designer,
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    legacy_items = get_legacy_active_consultation_items(admin=admin, designer=designer) or []
    payload = {"status": "ready", "items": [_serialize_consultation_like(item) for item in legacy_items]}
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_DASHBOARD_CACHE_SECONDS",
        default_timeout=30,
    )


def _build_active_consultation_client_map(items: list[dict] | None) -> dict[str, dict]:
    consultation_map: dict[str, dict] = {}
    for item in items or []:
        keys = {
            str(item.get("client_id") or "").strip(),
            str(item.get("legacy_client_id") or "").strip(),
        }
        for key in keys:
            if key:
                consultation_map[key] = item
    return consultation_map


def get_all_clients(*, query: str = "", admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    cache_key = _partner_lookup_cache_key(
        "partner-clients",
        admin=admin,
        designer=designer,
        query=(query or "").strip().lower(),
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    clients = _scoped_client_records(admin=admin, designer=designer, query=query, limit=100)
    legacy_active_items = get_legacy_active_consultation_items(admin=admin, designer=designer) or []
    legacy_active_by_client = _build_active_consultation_client_map(legacy_active_items)
    legacy_visit_summary_by_client = get_legacy_client_visit_summary_map(admin=admin, designer=designer)
    legacy_client_ids = {
        str(get_legacy_client_id(client=client) or "").strip()
        for client in clients
    }
    legacy_client_ids.discard("")

    survey_completed_keys: set[str] = set()
    photo_captured_keys: set[str] = set()
    consultation_requested_keys: set[str] = set()

    if legacy_client_ids:
        survey_completed_keys = {
            str(value).strip()
            for value in LegacyClientSurvey.objects.filter(client_id__in=legacy_client_ids).values_list("client_id", flat=True)
            if value
        }
        photo_captured_keys = {
            str(value).strip()
            for value in LegacyClientAnalysis.objects.filter(client_id__in=legacy_client_ids).values_list("client_id", flat=True)
            if value
        }
        confirmed_items = get_legacy_confirmed_selection_items(
            admin=admin,
            designer=designer,
            compact=True,
        ) or []
        for confirmed in confirmed_items:
            backend_client_key = str(confirmed.get("client_id") or "").strip()
            legacy_client_key = str(confirmed.get("legacy_client_id") or "").strip()
            if backend_client_key:
                consultation_requested_keys.add(backend_client_key)
            if legacy_client_key:
                consultation_requested_keys.add(legacy_client_key)

    items = []
    for client in clients:
        legacy_client_id = str(get_legacy_client_id(client=client) or "").strip()
        backend_client_id = str(client.id or "").strip()
        legacy_active = (
            legacy_active_by_client.get(backend_client_id)
            or legacy_active_by_client.get(legacy_client_id)
        )
        legacy_visit_summary = (
            legacy_visit_summary_by_client.get(backend_client_id)
            or legacy_visit_summary_by_client.get(legacy_client_id)
        )
        items.append(
            {
                "client_id": client.id,
                "legacy_client_id": get_legacy_client_id(client=client),
                "name": client.name,
                "gender": client.gender,
                "phone": client.phone,
                "shop_id": client.shop_id,
                "shop_name": client.shop.store_name if client.shop_id and client.shop else None,
                "designer_id": client.designer_id,
                "legacy_designer_id": (
                    get_legacy_designer_id(designer=client.designer)
                    if client.designer_id and client.designer else None
                ),
                "designer_name": client.designer.name if client.designer_id and client.designer else None,
                "assigned_at": client.assigned_at,
                "assignment_source": client.assignment_source,
                "is_assignment_pending": client.designer_id is None and bool(client.shop_id),
                **_client_age_fields(client),
                "created_at": client.created_at,
                "last_visit_date": (legacy_visit_summary.get("last_visit_date") if legacy_visit_summary else None),
                "visit_count": int(legacy_visit_summary.get("visit_count") or 0) if legacy_visit_summary else 0,
                "last_consulted_at": (legacy_active.get("last_activity_at") if legacy_active else None),
                "has_active_consultation": bool(legacy_active and legacy_active.get("is_active")),
                "session_active": bool(legacy_active and legacy_active.get("is_active")),
                "can_write_designer_diagnosis": bool(legacy_active and legacy_active.get("is_active")),
                "has_survey_completed": bool(legacy_client_id and legacy_client_id in survey_completed_keys),
                "has_photo_captured": bool(legacy_client_id and legacy_client_id in photo_captured_keys),
                "has_consultation_requested": bool(
                    (backend_client_id and backend_client_id in consultation_requested_keys)
                    or (legacy_client_id and legacy_client_id in consultation_requested_keys)
                ),
            }
        )
    payload = {"status": "ready", "items": items}
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_LIST_CACHE_SECONDS",
        default_timeout=60,
    )


def assign_client_to_designer(
    *,
    client: "Client",
    designer_id: int | str,
    admin: "AdminAccount",
) -> dict:
    designer = get_designer_for_admin(admin=admin, designer_id=designer_id)
    if designer is None:
        raise ValueError("해당 매장 소속의 활성 디자이너를 찾을 수 없습니다.")

    if client.shop_id not in (None, admin.id) and client.designer_id is None:
        raise ValueError("현재 매장 범위를 벗어난 고객입니다.")

    if client.shop_id is None:
        client.shop = admin

    assigned_at = timezone.now()
    if hasattr(client, "save"):
        client.designer = designer
        client.assigned_at = assigned_at
        client.assignment_source = "shop_manual_assignment"
        client.save(update_fields=["shop", "designer", "assigned_at", "assignment_source"])
        sync_model_team_runtime_state(client=client)
    else:
        client = upsert_client_record(
            phone=client.phone,
            name=client.name,
            gender=getattr(client, "gender", None),
            age_input=getattr(client, "age_input", None),
            birth_year_estimate=getattr(client, "birth_year_estimate", None),
            shop=admin,
            designer=designer,
            assignment_source="shop_manual_assignment",
        )
        client.assigned_at = assigned_at

    _invalidate_partner_client_payloads(client=client, admin=admin, designer=designer)
    return {
        "status": "success",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "designer_id": designer.id,
        "legacy_designer_id": get_legacy_designer_id(designer=designer),
        "designer_name": designer.name,
        "assigned_at": client.assigned_at,
        "assignment_source": client.assignment_source,
    }


def get_client_detail(
    *,
    client: "Client",
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
    include_history: bool = False,
    history_limit: int = 20,
) -> dict:
    _ensure_client_in_scope(client=client, admin=admin, designer=designer)

    cache_key = _partner_lookup_cache_key(
        "partner-client-detail",
        admin=admin,
        designer=designer,
        client=client,
        include_history=include_history,
        history_limit=int(history_limit),
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    latest_survey = get_latest_survey(client)
    latest_analysis, latest_capture = get_latest_legacy_analysis_capture_bundle(client=client)
    legacy_items = get_legacy_active_consultation_items(admin=admin, designer=designer, client=client) or []
    legacy_active_consultation = legacy_items[0] if legacy_items else None
    latest_consultation = None
    customer_note, customer_note_storage_ready = _fetch_customer_profile_note(client=client)
    designer_diagnosis_card, diagnosis_storage_ready = _fetch_designer_diagnosis_card(client=client)
    legacy_recommendation_items = get_legacy_former_recommendation_items(client=client) or []
    has_active_consultation = bool(latest_consultation or legacy_active_consultation)
    active_consultation_payload = _serialize_active_consultation_payload(
        client=client,
        latest_consultation=latest_consultation,
        legacy_active_consultation=legacy_active_consultation,
    )
    retry_meta = _build_legacy_retry_recommendation_meta(
        items=legacy_recommendation_items,
        has_active_consultation=has_active_consultation,
    )
    has_reusable_preference = bool(latest_survey and latest_analysis)
    can_keep_preference = bool(has_reusable_preference and retry_meta["can_retry_recommendations"])
    keep_preference_block_reason = None
    if not has_reusable_preference:
        keep_preference_block_reason = "reusable_preference_missing"
    elif not can_keep_preference:
        keep_preference_block_reason = retry_meta.get("retry_block_reason")

    can_choose_again = not has_active_consultation
    choose_again_block_reason = None if can_choose_again else "consultation_started"
    if has_active_consultation:
        reanalysis_state = "consultation_locked"
    elif can_keep_preference:
        reanalysis_state = "keep_preference_available"
    elif can_choose_again:
        reanalysis_state = "choose_again_available"
    else:
        reanalysis_state = "blocked"

    if include_history:
        history_payload = _get_client_history_payload(
            client=client,
            admin=admin,
            designer=designer,
            limit=history_limit,
        )
    else:
        analysis_capture_count = get_legacy_analysis_capture_count(client=client)
        history_payload = {
            "capture_history": ([_serialize_capture(latest_capture)] if latest_capture else []),
            "analysis_history": ([_serialize_analysis(latest_analysis)] if latest_analysis else []),
            "style_selection_history": [],
            "chosen_recommendation_history": [],
            "notes": [],
            "history": {
                "deferred": True,
                "source": "legacy_bridge",
                "history_url": f"/api/v1/customers/{client.id}/history/",
                "limit": int(history_limit),
                "counts": {
                    "captures": analysis_capture_count,
                    "analyses": analysis_capture_count,
                    "notes": _count_note_rows(client=client),
                },
            },
        }

    payload = {
        "status": "ready",
        "client": _serialize_client_summary(client),
        "latest_survey": _serialize_survey(latest_survey),
        "latest_analysis": _serialize_analysis(latest_analysis),
        "latest_capture": _serialize_capture(latest_capture) if latest_capture else None,
        "capture_history": history_payload["capture_history"],
        "analysis_history": history_payload["analysis_history"],
        "style_selection_history": history_payload["style_selection_history"],
        "chosen_recommendation_history": history_payload["chosen_recommendation_history"],
        "reanalysis": {
            "state": reanalysis_state,
            "reason_code": (
                keep_preference_block_reason
                or choose_again_block_reason
                or retry_meta.get("retry_block_reason")
            ),
            "user_message": _reanalysis_block_message(
                keep_preference_block_reason
                or choose_again_block_reason
                or retry_meta.get("retry_block_reason")
            ),
            "start_url": f"/partner/customer-detail/{client.id}/reanalysis/",
            "has_reusable_preference": has_reusable_preference,
            "can_keep_preference": can_keep_preference,
            "can_choose_again": can_choose_again,
            "keep_preference_block_reason": keep_preference_block_reason,
            "choose_again_block_reason": choose_again_block_reason,
            "retry_state": retry_meta.get("retry_state"),
            "retry_block_reason": retry_meta.get("retry_block_reason"),
            "consultation_locked": has_active_consultation,
            "debug": {
                "legacy_reason_fields": {
                    "keep_preference_block_reason": keep_preference_block_reason,
                    "choose_again_block_reason": choose_again_block_reason,
                    "retry_block_reason": retry_meta.get("retry_block_reason"),
                    "consultation_locked": has_active_consultation,
                }
            },
        },
        "designer_diagnosis": _serialize_designer_diagnosis_card(
            designer_diagnosis_card,
            storage_ready=diagnosis_storage_ready,
        ),
        "active_consultation": active_consultation_payload,
        "session_status": _build_session_status_payload(
            is_active=has_active_consultation,
            diagnosis_storage_ready=diagnosis_storage_ready,
        ),
        "customer_note": _serialize_customer_profile_note(
            customer_note,
            storage_ready=customer_note_storage_ready,
        ),
        "notes": history_payload["notes"],
        "history": history_payload["history"],
    }
    timeout_setting = "PARTNER_HISTORY_CACHE_SECONDS" if include_history else "PARTNER_DETAIL_CACHE_SECONDS"
    default_timeout = 30 if include_history else 45
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting=timeout_setting,
        default_timeout=default_timeout,
    )


def get_client_history_detail(
    *,
    client: "Client",
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
    history_limit: int = 20,
) -> dict:
    _ensure_client_in_scope(client=client, admin=admin, designer=designer)

    cache_key = _partner_lookup_cache_key(
        "partner-client-history",
        admin=admin,
        designer=designer,
        client=client,
        history_limit=int(history_limit),
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    history_payload = _get_client_history_payload(
        client=client,
        admin=admin,
        designer=designer,
        limit=history_limit,
    )
    payload = {
        "status": "ready",
        "client": _serialize_client_summary(client),
        "history": history_payload["history"],
        "analysis_history": history_payload["analysis_history"],
        "capture_history": history_payload["capture_history"],
        "style_selection_history": history_payload["style_selection_history"],
        "chosen_recommendation_history": history_payload["chosen_recommendation_history"],
        "notes": history_payload["notes"],
    }
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_HISTORY_CACHE_SECONDS",
        default_timeout=30,
    )


def get_client_recommendation_report(*, client: "Client", admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    _ensure_client_in_scope(client=client, admin=admin, designer=designer)

    cache_key = _partner_lookup_cache_key(
        "partner-client-recommendations",
        admin=admin,
        designer=designer,
        client=client,
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    latest_analysis = get_latest_analysis(client)
    latest_survey = get_latest_survey(client)
    legacy_rows = get_legacy_former_recommendation_items(client=client) or []
    legacy_final_selected = next((row for row in legacy_rows if row.get("is_chosen")), None)

    payload = {
        "status": "ready",
        "client": {
            "client_id": client.id,
            "legacy_client_id": get_legacy_client_id(client=client),
            "name": client.name,
            "phone": client.phone,
            **_client_age_fields(client),
        },
        "latest_survey": _serialize_survey(latest_survey),
        "latest_analysis": _serialize_analysis(latest_analysis),
        "final_selected_style": (_serialize_recommendation(legacy_final_selected) if legacy_final_selected else None),
        "latest_generated_batch": {
            "batch_id": (legacy_rows[0]["batch_id"] if legacy_rows else None),
            "items": [_serialize_recommendation(row) for row in legacy_rows],
        },
    }
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_LOOKUP_CACHE_SECONDS",
        default_timeout=45,
    )


def create_client_note(*, client: "Client", consultation_id: int, content: str, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    consultation = _resolve_consultation_bridge(
        consultation_id=consultation_id,
        client=client,
        admin=admin,
        designer=designer,
    )
    if not consultation:
        raise ValueError("The consultation session could not be found.")

    consultation["admin_id"] = consultation.get("admin_id") or getattr(admin, "id", None)
    consultation["designer_id"] = consultation.get("designer_id") or getattr(designer, "id", None)
    _update_consultation_bridge_row(
        consultation_id=consultation["id"],
        admin_id=consultation.get("admin_id"),
        designer_id=consultation.get("designer_id"),
    )

    note_id = _create_note_row(
        consultation_id=consultation["id"],
        client_id=client.id,
        admin_id=getattr(admin, "id", None),
        designer_id=getattr(designer, "id", None),
        content=content.strip(),
    )
    _update_consultation_bridge_row(
        consultation_id=consultation["id"],
        is_read=True,
        status="IN_PROGRESS",
    )
    _sync_legacy_consultation_status(
        client=client,
        consultation_id=consultation["id"],
        status_value="IN_PROGRESS",
        is_active=True,
        is_read=True,
    )
    sync_model_team_runtime_state(client=client)
    _invalidate_partner_client_payloads(client=client, admin=admin, designer=designer)
    return {
        "status": "success",
        "note_id": note_id,
        "consultation_id": consultation["id"],
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "message": "저장 완료되었습니다.",
    }


def get_client_customer_note(*, client: "Client", admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    client_ref_id = getattr(client, "id", client)
    legacy_client_ref_id = get_legacy_client_id(client=client)
    _ensure_client_in_scope(client=client, admin=admin, designer=designer)

    cache_key = _partner_lookup_cache_key(
        "partner-client-note",
        admin=admin,
        designer=designer,
        client=client,
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    note, storage_ready = _fetch_customer_profile_note(client=client)
    payload = {
        "status": "ready",
        "client_id": client_ref_id,
        "legacy_client_id": legacy_client_ref_id,
        "customer_note": _serialize_customer_profile_note(note, storage_ready=storage_ready),
    }
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_LOOKUP_CACHE_SECONDS",
        default_timeout=45,
    )


def upsert_client_customer_note(
    *,
    client: "Client",
    content: str,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> dict:
    client_ref_id = getattr(client, "id", client)
    legacy_client_ref_id = get_legacy_client_id(client=client)
    _ensure_client_in_scope(client=client, admin=admin, designer=designer)

    normalized_content = str(content or "").strip()
    ClientProfileNote = _get_runtime_model("ClientProfileNote")

    filters = Q(client_ref_id=client_ref_id)
    if legacy_client_ref_id:
        filters |= Q(legacy_client_ref_id=legacy_client_ref_id)
    try:
        note = ClientProfileNote.objects.filter(filters).first()
    except (OperationalError, ProgrammingError) as exc:
        raise ValueError("고객 메모 저장 테이블이 아직 준비되지 않았습니다. 마이그레이션 적용 후 다시 시도해 주세요.") from exc

    editor_admin = admin or (designer.shop if designer is not None else None)
    editor_designer = designer
    editor_admin_id = getattr(editor_admin, "id", editor_admin) if editor_admin is not None else None
    editor_designer_id = getattr(editor_designer, "id", editor_designer) if editor_designer is not None else None

    if not normalized_content:
        if note is not None:
            note.delete()
        _invalidate_partner_client_payloads(client=client, admin=admin, designer=designer)
        return {
            "status": "success",
            "client_id": client_ref_id,
            "legacy_client_id": legacy_client_ref_id,
            "message": "저장 완료되었습니다.",
            "customer_note": _default_customer_profile_note_payload(),
        }

    if note is None:
        note = ClientProfileNote.objects.create(
            client_ref_id=client_ref_id,
            legacy_client_ref_id=legacy_client_ref_id,
            admin_ref_id=editor_admin_id,
            designer_ref_id=editor_designer_id,
            content=normalized_content,
        )
    else:
        note.client_ref_id = client_ref_id
        note.legacy_client_ref_id = legacy_client_ref_id
        note.admin_ref_id = editor_admin_id
        note.designer_ref_id = editor_designer_id
        note.content = normalized_content
        note.save()

    _invalidate_partner_client_payloads(client=client, admin=admin, designer=designer)
    return {
        "status": "success",
        "client_id": client_ref_id,
        "legacy_client_id": legacy_client_ref_id,
        "message": "저장 완료되었습니다.",
        "customer_note": _serialize_customer_profile_note(note),
    }


def close_consultation_session(
    *,
    consultation_id: int,
    client: "Client | None" = None,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> dict:
    consultation = _resolve_consultation_bridge(
        consultation_id=consultation_id,
        client=client,
        admin=admin,
        designer=designer,
    )
    if not consultation:
        raise ValueError("The consultation session could not be found.")

    consultation["admin_id"] = consultation.get("admin_id") or getattr(admin, "id", None)
    consultation["designer_id"] = consultation.get("designer_id") or getattr(designer, "id", None)
    consultation["is_active"] = False
    consultation["is_read"] = True
    consultation["status"] = "CLOSED"
    consultation["closed_at"] = timezone.now()
    _update_consultation_bridge_row(
        consultation_id=consultation["id"],
        admin_id=consultation.get("admin_id"),
        designer_id=consultation.get("designer_id"),
        is_active=False,
        is_read=True,
        status="CLOSED",
        closed_at=consultation["closed_at"],
    )
    _sync_legacy_consultation_status(
        client=consultation["client"],
        consultation_id=consultation["id"],
        status_value="CLOSED",
        is_active=False,
        is_read=True,
        closed_at=consultation["closed_at"],
    )
    _invalidate_partner_client_payloads(
        client=consultation["client"],
        admin=admin,
        designer=designer,
    )
    return {
        "status": "success",
        "consultation_id": consultation["id"],
        "client_id": consultation["client_id"],
        "legacy_client_id": get_legacy_client_id(client=consultation["client"]),
        "message": "상담 세션을 종료했습니다.",
    }


def _selection_matches_snapshot(selection: "StyleSelection", filters: dict) -> bool:
    snapshot = selection.survey_snapshot or {}
    if not snapshot and hasattr(selection.client, "survey"):
        survey = selection.client.survey
        snapshot = {
            "target_length": survey.target_length,
            "target_vibe": survey.target_vibe,
            "scalp_type": survey.scalp_type,
            "hair_colour": survey.hair_colour,
            "budget_range": survey.budget_range,
        }
    age_profile = build_client_age_profile(selection.client) or snapshot.get("age_profile") or {}
    return _selection_matches_payload(survey_snapshot=snapshot, age_profile=age_profile, filters=filters)


def _build_trend_report_snapshot(*, days: int, filters: dict, admin: "AdminAccount | None", designer: "Designer | None", total_records: int, filtered_records: int, ranking_count: int, unique_clients: int) -> dict:
    return {
        "days": days,
        "filters": filters,
        "admin_scoped": admin is not None,
        "designer_scoped": designer is not None,
        "total_records": total_records,
        "filtered_records": filtered_records,
        "ranking_count": ranking_count,
        "unique_clients": unique_clients,
    }


def _build_style_report_snapshot(*, style_id: int, days: int, admin: "AdminAccount | None", designer: "Designer | None", recent_count: int, chosen_count: int, related_count: int) -> dict:
    return {
        "style_id": style_id,
        "days": days,
        "admin_scoped": admin is not None,
        "designer_scoped": designer is not None,
        "recent_selection_count": recent_count,
        "chosen_count": chosen_count,
        "related_style_count": related_count,
    }


_AGE_DECADE_SORT_ORDER = {
    f"{decade}대": index
    for index, decade in enumerate(range(10, 100, 10), start=1)
}
_AGE_GROUP_SORT_ORDER = {
    f"{decade}대 {segment}": index
    for index, (decade, segment) in enumerate(
        (
            (10, "초반"),
            (10, "중반"),
            (10, "후반"),
            (20, "초반"),
            (20, "중반"),
            (20, "후반"),
            (30, "초반"),
            (30, "중반"),
            (30, "후반"),
            (40, "초반"),
            (40, "중반"),
            (40, "후반"),
            (50, "초반"),
            (50, "중반"),
            (50, "후반"),
            (60, "초반"),
            (60, "중반"),
            (60, "후반"),
            (70, "초반"),
            (70, "중반"),
            (70, "후반"),
            (80, "초반"),
            (80, "중반"),
            (80, "후반"),
            (90, "초반"),
            (90, "중반"),
            (90, "후반"),
        ),
        start=1,
    )
}


def _sort_distribution_rows(rows: list[dict], *, label_key: str) -> list[dict]:
    order_map = _AGE_GROUP_SORT_ORDER if label_key == "age_group" else _AGE_DECADE_SORT_ORDER
    return sorted(
        [row for row in rows if row.get(label_key)],
        key=lambda row: (
            order_map.get(str(row.get(label_key) or "").strip(), 999),
            str(row.get(label_key) or "").strip(),
        ),
    )


def _build_designer_customer_distribution(*, items: list[dict]) -> list[dict]:
    counter = Counter()
    for item in items:
        designer_name = str(item.get("designer_name") or "").strip() or "미배정"
        counter[designer_name] += 1

    return [
        {"designer_name": name, "customer_count": count}
        for name, count in sorted(counter.items(), key=lambda entry: (-entry[1], entry[0]))
    ]


def _local_date_from_value(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            try:
                parsed = timezone.datetime.fromisoformat(value)
            except ValueError:
                return None
        value = parsed
    if hasattr(value, "date"):
        if not hasattr(value, "tzinfo"):
            return value.date()
        if timezone.is_naive(value):
            return value.date()
        return timezone.localtime(value).date()
    return None


def _legacy_dashboard_client_queryset(
    *,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
):
    if designer is not None:
        backend_designer_id = get_backend_designer_id(designer=designer)
        if backend_designer_id is None:
            return LegacyClient.objects.none()
        return LegacyClient.objects.filter(backend_designer_ref_id=backend_designer_id)

    if admin is not None:
        backend_admin_id = get_backend_admin_id(admin=admin)
        legacy_admin_id = get_legacy_admin_id(admin=admin)
        scope_filter = Q()
        if backend_admin_id is not None:
            scope_filter |= Q(backend_shop_ref_id=backend_admin_id)
        if legacy_admin_id:
            scope_filter |= Q(shop_id=legacy_admin_id)
        if not scope_filter:
            return LegacyClient.objects.none()
        return LegacyClient.objects.filter(scope_filter)

    return LegacyClient.objects.all()


def _build_legacy_dashboard_client_metrics(
    *,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> dict:
    rows = list(
        _legacy_dashboard_client_queryset(admin=admin, designer=designer).only(
            "client_id",
            "backend_client_id",
            "created_at",
            "backend_designer_ref_id",
        )
    )
    if not rows:
        return {
            "total_customers": 0,
            "new_today": 0,
            "designer_customer_distribution": [],
        }

    today = timezone.localdate()
    new_today = 0
    designer_counter: Counter[str] = Counter()
    designer_ids: set[int] = set()
    for row in rows:
        if _local_date_from_value(getattr(row, "created_at", None)) == today:
            new_today += 1
        if designer is not None:
            continue
        backend_designer_ref_id = getattr(row, "backend_designer_ref_id", None)
        if backend_designer_ref_id:
            try:
                numeric_designer_id = int(backend_designer_ref_id)
            except (TypeError, ValueError):
                designer_counter["미배정"] += 1
                continue
            designer_ids.add(numeric_designer_id)
            designer_counter[f"backend:{numeric_designer_id}"] += 1
        else:
            designer_counter["미배정"] += 1

    designer_customer_distribution: list[dict] = []
    if designer is None:
        designer_name_map = {
            f"backend:{int(row.backend_designer_id)}": (
                row.name or row.designer_name or f"디자이너 {row.backend_designer_id}"
            )
            for row in LegacyDesigner.objects.filter(backend_designer_id__in=designer_ids).only(
                "backend_designer_id",
                "name",
                "designer_name",
            )
            if row.backend_designer_id
        }
        designer_customer_distribution = [
            {
                "designer_name": designer_name_map.get(
                    key,
                    "미배정" if key == "미배정" else key.replace("backend:", "디자이너 "),
                ),
                "customer_count": count,
            }
            for key, count in sorted(
                designer_counter.items(),
                key=lambda entry: (-entry[1], "zzz" if entry[0] == "미배정" else entry[0]),
            )
        ]

    return {
        "total_customers": len(rows),
        "new_today": new_today,
        "designer_customer_distribution": designer_customer_distribution,
    }


def get_legacy_dashboard_trend_report(
    *,
    days: int = 7,
    admin: "AdminAccount | None" = None,
    designer: "Designer | None" = None,
) -> dict:
    days = max(1, int(days or 7))
    cache_key = _partner_lookup_cache_key(
        "partner-legacy-dashboard-report",
        admin=admin,
        designer=designer,
        days=days,
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    trend_payload = get_admin_trend_report(days=days, filters={}, admin=admin, designer=designer)
    client_metrics = _build_legacy_dashboard_client_metrics(admin=admin, designer=designer)

    start_date = timezone.localdate() - timezone.timedelta(days=days - 1)
    activity_by_day = get_legacy_activity_client_map_by_day(
        start_date=start_date,
        days=days,
        admin=admin,
        designer=designer,
    )
    if activity_by_day is None:
        activity_by_day = {
            (start_date + timezone.timedelta(days=offset)).isoformat(): set()
            for offset in range(days)
        }

    total_customers = client_metrics["total_customers"]
    new_today = client_metrics["new_today"]
    unique_clients = trend_payload["kpi"]["unique_clients"]
    conversion_rate = round(
        (trend_payload["kpi"]["total_confirmations"] / unique_clients) * 100
    ) if unique_clients else 0

    payload = {
        "summary": {
            "total_customers": total_customers,
            "new_today": new_today,
            "conversion_rate": conversion_rate,
        },
        "kpi": trend_payload["kpi"],
        "scope": {
            "admin_scoped": admin is not None,
            "designer_scoped": designer is not None,
            "store_name": (
                getattr(admin, "store_name", None)
                or getattr(getattr(designer, "shop", None), "store_name", None)
            ),
            "designer_name": getattr(designer, "name", None),
        },
        "visitor_stats": [
            {"date": date, "count": len(client_set)}
            for date, client_set in activity_by_day.items()
        ],
        "style_distribution": [
            {"name": item["style_name"], "value": item["selection_count"]}
            for item in trend_payload["distribution"]
        ],
        "style_ranking": trend_payload["ranking"],
        "age_group_distribution": _sort_distribution_rows(
            trend_payload.get("age_group_distribution", []),
            label_key="age_group",
        ),
        "age_decade_distribution": _sort_distribution_rows(
            trend_payload.get("age_decade_distribution", []),
            label_key="age_decade",
        ),
        "designer_customer_distribution": (
            client_metrics["designer_customer_distribution"]
            if designer is None
            else []
        ),
        "report_snapshot": trend_payload.get("report_snapshot", {}),
    }
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_REPORT_CACHE_SECONDS",
        default_timeout=90,
    )


def get_admin_trend_report(*, days: int = 7, filters: dict | None = None, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    filters = filters or {}
    normalized_filters = {
        str(key): value
        for key, value in sorted(filters.items())
        if value not in (None, "", [], {}, ())
    }
    cache_key = _partner_lookup_cache_key(
        "partner-trend-report",
        admin=admin,
        designer=designer,
        days=int(days),
        filters=normalized_filters,
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    cutoff = timezone.now() - timezone.timedelta(days=days)
    legacy_items = get_legacy_confirmed_selection_items(
        since=cutoff,
        admin=admin,
        designer=designer,
        compact=True,
    ) or []
    selections = legacy_items
    filtered = [
        row
        for row in selections
        if _selection_matches_payload(
            survey_snapshot=row.get("survey_snapshot"),
            age_profile=row.get("age_profile"),
            filters=filters,
        )
    ]
    counter = Counter(row["style_id"] for row in filtered)
    representative = {}
    for row in filtered:
        representative.setdefault(row["style_id"], row)
    top_style_ids = [style_id for style_id, _ in counter.most_common(10)]
    style_records = _style_record_map(top_style_ids)
    ranking = []
    for rank, (style_id, count) in enumerate(counter.most_common(10), start=1):
        row = representative.get(style_id, {})
        style_data = _style_snapshot(
            style_id,
            resolve_image=False,
            style=style_records.get(style_id),
        )
        ranking.append(
            {
                "rank": rank,
                "style_id": style_id,
                "style_name": row.get("style_name") or style_data["style_name"],
                "image_url": _report_image_url(row.get("image_url")) or style_data["image_url"],
                "selection_count": count,
                "keywords": row.get("keywords") or style_data["keywords"],
            }
        )
    age_decade_counter = Counter()
    age_group_counter = Counter()
    for row in filtered:
        profile = row.get("age_profile") or {}
        if profile.get("age_decade"):
            age_decade_counter[profile["age_decade"]] += 1
        if profile.get("age_group"):
            age_group_counter[profile["age_group"]] += 1
    unique_clients = len(
        {
            row.get("client_id") or row.get("legacy_client_id")
            for row in filtered
            if row.get("client_id") or row.get("legacy_client_id")
        }
    )

    distribution = [
        {
            "style_id": item["style_id"],
            "style_name": item["style_name"],
            "selection_count": item["selection_count"],
        }
        for item in ranking
    ]
    report_snapshot = _build_trend_report_snapshot(
        days=days,
        filters=filters,
        admin=admin,
        designer=designer,
        total_records=len(selections),
        filtered_records=len(filtered),
        ranking_count=len(ranking),
        unique_clients=unique_clients,
    )
    logger.info(
        "[trend_report] days=%s total=%s filtered=%s ranking=%s admin_scoped=%s designer_scoped=%s",
        days,
        len(selections),
        len(filtered),
        len(ranking),
        admin is not None,
        designer is not None,
    )
    active_consultation_count = get_legacy_active_consultation_count(admin=admin, designer=designer)
    payload = {
        "status": "ready",
        "days": days,
        "filters": filters,
        "kpi": {
            "unique_clients": unique_clients,
            "total_confirmations": len(filtered),
            "active_consultations": active_consultation_count,
        },
        "ranking": ranking,
        "distribution": distribution,
        "age_decade_distribution": [
            {"age_decade": key, "selection_count": count}
            for key, count in age_decade_counter.most_common()
        ],
        "age_group_distribution": [
            {"age_group": key, "selection_count": count}
            for key, count in age_group_counter.most_common()
        ],
        "report_snapshot": report_snapshot,
        "message": (
            "Trend report generated successfully."
            if filtered
            else "No trend selections were found for the requested period."
        ),
    }
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_REPORT_CACHE_SECONDS",
        default_timeout=90,
    )


def get_style_report(*, style_id: int, days: int = 7, admin: "AdminAccount | None" = None, designer: "Designer | None" = None) -> dict:
    cache_key = _partner_lookup_cache_key(
        "partner-style-report",
        admin=admin,
        designer=designer,
        style_id=int(style_id),
        days=int(days),
    )
    cached_payload = _partner_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    style_data = _style_snapshot(style_id)
    cutoff = timezone.now() - timezone.timedelta(days=days)
    legacy_items = get_legacy_confirmed_selection_items(
        since=cutoff,
        admin=admin,
        designer=designer,
    ) or []
    filtered_items = [row for row in legacy_items if row["style_id"] == style_id]
    recent_count = len(filtered_items)
    chosen_count = len(filtered_items)

    related = []
    target_profile = next((item for item in STYLE_CATALOG if item.style_id == style_id), None)
    if target_profile:
        scored = []
        for profile in STYLE_CATALOG:
            if profile.style_id == style_id:
                continue
            score = len(set(target_profile.keywords) & set(profile.keywords))
            if set(target_profile.vibe_tags) & set(profile.vibe_tags):
                score += 1
            scored.append((score, profile.style_id))
        for _, related_style_id in sorted(scored, key=lambda item: (-item[0], item[1]))[:5]:
            related.append(_style_snapshot(related_style_id))

    report_snapshot = _build_style_report_snapshot(
        style_id=style_id,
        days=days,
        admin=admin,
        designer=designer,
        recent_count=recent_count,
        chosen_count=chosen_count,
        related_count=len(related),
    )
    logger.info(
        "[style_report] style_id=%s days=%s recent=%s chosen=%s related=%s admin_scoped=%s designer_scoped=%s",
        style_id,
        days,
        recent_count,
        chosen_count,
        len(related),
        admin is not None,
        designer is not None,
    )
    payload = {
        "status": "ready",
        "style": {
            **style_data,
            "recent_selection_count": recent_count,
            "chosen_count": chosen_count,
        },
        "related_styles": related,
        "report_snapshot": report_snapshot,
    }
    return _partner_cache_set(
        cache_key,
        payload,
        timeout_setting="PARTNER_REPORT_CACHE_SECONDS",
        default_timeout=90,
    )
