import base64
import logging
import time
import uuid
from collections import Counter
from types import SimpleNamespace
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from app.api.v1.recommendation_runtime import (
    RecommendationWaitPolicy,
    build_runtime_state,
    prepare_recommendation_assets,
    runtime_requires_wait_for_recommendations,
    wait_for_runtime_state,
)
from app.api.v1.recommendation_logic import (
    RETRY_SCORING_WEIGHTS,
    STYLE_CATALOG,
    build_preference_vector,
    canonical_face_shape,
)
from app.models_model_team import (
    LegacyClientResult,
    LegacyClientResultDetail,
    LegacyHairstyle,
    LegacyClientSurvey,
)
from app.services.age_profile import build_client_age_profile, client_matches_age_profile
from app.services.capture_validation import infer_capture_reason_code
from app.services.ai_facade import (
    analyze_face_with_runpod,
    generate_recommendation_batch,
    get_ai_runtime_config_snapshot,
    simulate_face_analysis,
)
from app.services.model_team_bridge import (
    LEGACY_ANALYSIS_MODEL_COLUMNS,
    LEGACY_HAIRSTYLE_MODEL_COLUMNS,
    LEGACY_RESULT_DETAIL_MODEL_COLUMNS,
    LEGACY_RESULT_MODEL_COLUMNS,
    LEGACY_SURVEY_MODEL_COLUMNS,
    _has_columns,
    complete_legacy_capture_analysis,
    fail_legacy_capture_processing,
    find_legacy_recommendation_context,
    get_admin_by_identifier,
    get_legacy_active_consultation_items,
    get_legacy_client_id,
    get_legacy_confirmed_selection_items,
    get_style_record,
    get_style_record_by_name,
    get_latest_legacy_analysis,
    get_latest_legacy_capture,
    get_latest_legacy_survey,
    get_legacy_former_recommendation_items,
    has_legacy_analysis_source,
    has_legacy_result_source,
    mark_legacy_capture_processing,
    sync_model_team_rows,
    sync_model_team_runtime_state,
)
from app.services.runtime_client import RuntimeClient as Client
from app.services.storage_service import (
    build_storage_snapshot,
    persist_analysis_input_image_reference,
    persist_simulation_image_reference,
    resolve_storage_reference,
)
from app.trend_pipeline.style_collection import load_hairstyles

if TYPE_CHECKING:
    from app.models_django import (
        AdminAccount,
        CaptureRecord,
        ConsultationRequest,
        FaceAnalysis,
        FormerRecommendation,
        Style,
        StyleSelection,
        Survey,
    )


logger = logging.getLogger(__name__)


REGENERATION_MAX_ATTEMPTS = 1
REGENERATION_POLICY = {
    "mode": "single_retry",
    "seed_strategy": "vary_seed",
    "selection_bias": "face_ratio_preference_boost",
    "trend_bias": "reduced",
}

RETRY_RECOMMENDATION_MAX_ATTEMPTS = 1
RETRY_RECOMMENDATION_POLICY = {
    "mode": "single_retry",
    "trend_included": False,
    "preference_weight": 70,
    "face_shape_weight": 20,
    "ratio_weight": 10,
    "face_total_weight": 30,
    "selection_bias": "preference_dominant",
}

CURRENT_RECOMMENDATION_WAIT_POLICY = RecommendationWaitPolicy(
    timeout_seconds=45.0,
    interval_seconds=3.0,
)
FAILED_RECOMMENDATION_INPUT_STATUSES = {"FAILED", "NEEDS_RETAKE", "ERROR"}


def _coerce_iso_datetime(value):
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = timezone.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        return parsed.isoformat() if parsed is not None else normalized
    return str(value)


def _seed_trend_styles(limit: int = 5) -> list[dict]:
    try:
        styles = load_hairstyles()
    except FileNotFoundError:
        return []
    return styles[:limit]


def build_default_survey_context(client_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        client_id=client_id,
        target_length=None,
        target_vibe=None,
        scalp_type=None,
        hair_colour=None,
        budget_range=None,
        preference_vector=[0.0] * 20,
    )


def ensure_catalog_styles() -> dict[int, object]:
    styles_by_id: dict[int, object] = {
        profile.style_id: get_style_record(style_id=profile.style_id)
        for profile in STYLE_CATALOG
        if get_style_record(style_id=profile.style_id) is not None
    }
    for profile in STYLE_CATALOG:
        if profile.style_id in styles_by_id:
            continue
        style_defaults = {
            "chroma_id": str(profile.style_id),
            "style_name": profile.fallback_name,
            "image_url": profile.fallback_sample_image_url,
            "created_at": timezone.now().isoformat(),
            "backend_style_id": profile.style_id,
            "name": profile.fallback_name,
            "vibe": (profile.vibe_tags[0] if profile.vibe_tags else "natural").title(),
            "description": profile.fallback_description,
        }
        if _has_columns("hairstyle", LEGACY_HAIRSTYLE_MODEL_COLUMNS):
            LegacyHairstyle.objects.update_or_create(
                hairstyle_id=profile.style_id,
                defaults=style_defaults,
            )
            styles_by_id[profile.style_id] = get_style_record(style_id=profile.style_id)
            continue
        styles_by_id[profile.style_id] = SimpleNamespace(
            hairstyle_id=profile.style_id,
            backend_style_id=profile.style_id,
            name=profile.fallback_name,
            style_name=profile.fallback_name,
            vibe=style_defaults["vibe"],
            description=profile.fallback_description,
            image_url=profile.fallback_sample_image_url,
        )
    return styles_by_id


def _style_reference(style_id: int, *, styles_by_id: "dict[int, Style] | None" = None) -> dict:
    styles_by_id = styles_by_id or ensure_catalog_styles()
    style = styles_by_id.get(style_id) or get_style_record(style_id=style_id)
    profile = next((item for item in STYLE_CATALOG if item.style_id == style_id), None)
    sample_image_url = None
    if style and style.image_url:
        sample_image_url = style.image_url
    elif profile:
        sample_image_url = profile.fallback_sample_image_url

    return {
        "sample_image_url": sample_image_url,
        "style_name": (style.name if style else (profile.fallback_name if profile else f"Style {style_id}")),
        "style_description": (style.description if style else (profile.fallback_description if profile else "")) or "",
        "keywords": list(profile.keywords) if profile else ([style.vibe] if style and style.vibe else []),
    }


def get_latest_survey(client: "Client"):
    legacy_survey = get_latest_legacy_survey(client=client)
    if legacy_survey is not None:
        return legacy_survey
    return None


def get_latest_analysis(client: "Client"):
    legacy_analysis = get_latest_legacy_analysis(client=client)
    if legacy_analysis is not None:
        return legacy_analysis
    return None


def get_latest_capture(client: "Client"):
    legacy_capture = get_latest_legacy_capture(client=client)
    if legacy_capture is not None:
        return legacy_capture
    return None


def _load_current_recommendation_runtime(client: "Client"):
    return build_runtime_state(
        latest_capture_attempt=get_latest_capture_attempt(client),
        latest_survey=get_latest_survey(client),
        latest_capture=get_latest_capture(client),
        latest_analysis=get_latest_analysis(client),
        legacy_items=(get_legacy_former_recommendation_items(client=client) or []),
    )


def _prepare_current_legacy_assets(*, latest_analysis, legacy_items: list[dict]):
    return prepare_recommendation_assets(
        items=legacy_items,
        latest_analysis=latest_analysis,
        persist_reference=persist_simulation_image_reference,
    )


def _legacy_survey_writable() -> bool:
    return _has_columns("client_survey", LEGACY_SURVEY_MODEL_COLUMNS)


def _legacy_result_writable() -> bool:
    return (
        _has_columns("client_result", LEGACY_RESULT_MODEL_COLUMNS)
        and _has_columns("client_result_detail", LEGACY_RESULT_DETAIL_MODEL_COLUMNS)
    )


def _next_legacy_pk(model, field_name: str) -> int:
    latest = model.objects.aggregate(max_value=Max(field_name)).get("max_value")
    return int(latest or 0) + 1


def _legacy_survey_namespace(*, survey_id: int, client: "Client", normalized_payload: dict, preference_vector: list[float], created_at) -> SimpleNamespace:
    return SimpleNamespace(
        id=survey_id,
        client=client.id,
        client_id=client.id,
        target_length=normalized_payload.get("target_length"),
        target_vibe=normalized_payload.get("target_vibe"),
        scalp_type=normalized_payload.get("scalp_type"),
        hair_colour=normalized_payload.get("hair_colour"),
        budget_range=normalized_payload.get("budget_range"),
        preference_vector=preference_vector,
        created_at=created_at,
    )


def _legacy_preference_vector_storage(preference_vector: list[float]) -> str:
    if connection.vendor == "postgresql":
        return "{" + ",".join(str(float(value)) for value in preference_vector) + "}"
    return str(preference_vector)


def _persist_legacy_survey(*, client: "Client", normalized_payload: dict, preference_vector: list[float]) -> SimpleNamespace | None:
    if not _legacy_survey_writable():
        return None

    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        return None

    existing = (
        LegacyClientSurvey.objects.filter(client_id=legacy_client_id)
        .order_by("-created_at_ts", "-survey_id")
        .first()
    )
    created_at = timezone.now()
    survey_id = existing.survey_id if existing is not None else _next_legacy_pk(LegacyClientSurvey, "survey_id")
    LegacyClientSurvey.objects.update_or_create(
        survey_id=survey_id,
        defaults={
            "client_id": legacy_client_id,
            "hair_length": normalized_payload.get("target_length"),
            "hair_mood": normalized_payload.get("target_vibe"),
            "hair_condition": normalized_payload.get("scalp_type"),
            "hair_color": normalized_payload.get("hair_colour"),
            "budget": normalized_payload.get("budget_range"),
            "preference_vector": _legacy_preference_vector_storage(preference_vector),
            "updated_at": created_at.isoformat(),
            "backend_survey_id": None,
            "backend_client_ref_id": client.id,
            "target_length": normalized_payload.get("target_length"),
            "target_vibe": normalized_payload.get("target_vibe"),
            "scalp_type": normalized_payload.get("scalp_type"),
            "hair_colour": normalized_payload.get("hair_colour"),
            "budget_range": normalized_payload.get("budget_range"),
            "preference_vector_json": preference_vector,
            "created_at_ts": created_at,
        },
    )
    return _legacy_survey_namespace(
        survey_id=survey_id,
        client=client,
        normalized_payload=normalized_payload,
        preference_vector=preference_vector,
        created_at=created_at,
    )


def _legacy_result_detail_candidates(*, recommendation_id: int) -> list[LegacyClientResultDetail]:
    return list(
        LegacyClientResultDetail.objects.filter(
            Q(detail_id=recommendation_id) | Q(backend_recommendation_id=recommendation_id)
        ).order_by("-detail_id")
    )


def _legacy_result_and_detail_for_recommendation(*, client: "Client", recommendation_id: int) -> tuple[LegacyClientResult | None, LegacyClientResultDetail | None]:
    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        return None, None
    for detail in _legacy_result_detail_candidates(recommendation_id=recommendation_id):
        result_row = LegacyClientResult.objects.filter(result_id=detail.result_id, client_id=legacy_client_id).first()
        if result_row is not None:
            return result_row, detail
    return None, None


def _legacy_result_and_detail_for_style(*, client: "Client", style_id: int) -> tuple[LegacyClientResult | None, LegacyClientResultDetail | None]:
    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        return None, None
    result_rows = list(
        LegacyClientResult.objects.filter(client_id=legacy_client_id).order_by("-updated_at", "-result_id")
    )
    for result_row in result_rows:
        detail = (
            LegacyClientResultDetail.objects.filter(result_id=result_row.result_id, hairstyle_id=style_id)
            .order_by("rank", "detail_id")
            .first()
        )
        if detail is not None:
            return result_row, detail
    return None, None


def _legacy_recommendation_namespace(
    *,
    client: "Client",
    capture_record,
    batch_id,
    created_at,
    item: dict,
    detail_id: int,
) -> SimpleNamespace:
    style = get_style_record(style_id=item["style_id"])
    return SimpleNamespace(
        id=detail_id,
        client=client,
        client_id=client.id,
        capture_record=_resolve_capture_record_relation(capture_record),
        capture_record_id=getattr(capture_record, "id", None),
        style=style,
        batch_id=batch_id,
        source="generated",
        style_id_snapshot=item["style_id"],
        style_name_snapshot=item["style_name"],
        style_description_snapshot=item.get("style_description", ""),
        keywords=list(item.get("keywords") or []),
        sample_image_url=item.get("sample_image_url"),
        simulation_image_url=item.get("simulation_image_url"),
        regeneration_snapshot=item.get("regeneration_snapshot"),
        llm_explanation=item.get("llm_explanation"),
        reasoning_snapshot=dict(item.get("reasoning_snapshot") or {}),
        match_score=item.get("match_score"),
        rank=item.get("rank", 0),
        is_chosen=False,
        chosen_at=None,
        is_sent_to_admin=False,
        sent_at=None,
        created_at=created_at,
    )


def _persist_legacy_generated_batch(
    *,
    client: "Client",
    capture_record,
    survey,
    analysis,
    items: list[dict],
    regeneration_snapshot: dict,
    recommendation_stage: str,
) -> tuple[str, list[SimpleNamespace]] | None:
    if not _legacy_result_writable():
        return None

    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        return None

    created_at = timezone.now()
    batch_id = uuid.uuid4()
    result_id = _next_legacy_pk(LegacyClientResult, "result_id")
    next_detail_id = _next_legacy_pk(LegacyClientResultDetail, "detail_id")
    analysis_id = getattr(analysis, "id", None) or getattr(analysis, "analysis_id", None) or 0
    survey_snapshot = {
        "target_length": getattr(survey, "target_length", None),
        "target_vibe": getattr(survey, "target_vibe", None),
        "scalp_type": getattr(survey, "scalp_type", None),
        "hair_colour": getattr(survey, "hair_colour", None),
        "budget_range": getattr(survey, "budget_range", None),
        "preference_vector": getattr(survey, "preference_vector", None) or [],
    }
    analysis_snapshot = {
        "face_shape": getattr(analysis, "face_shape", None),
        "golden_ratio": getattr(analysis, "golden_ratio_score", None),
        "image_url": resolve_storage_reference(getattr(analysis, "image_url", None)),
        "landmark_snapshot": getattr(analysis, "landmark_snapshot", None) or {},
        **(
            {"source": getattr(analysis, "analysis_source", None)}
            if getattr(analysis, "analysis_source", None)
            else {}
        ),
    }
    normalized_items = _normalize_persistable_recommendation_items(
        items=items,
        analysis_snapshot=analysis_snapshot,
    )

    LegacyClientResult.objects.create(
        result_id=result_id,
        analysis_id=analysis_id,
        client_id=legacy_client_id,
        selected_hairstyle_id=None,
        selected_image_url=None,
        is_confirmed=False,
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
        backend_selection_id=None,
        backend_consultation_id=None,
        backend_client_ref_id=client.id,
        backend_admin_ref_id=client.shop_id,
        backend_designer_ref_id=client.designer_id,
        source="generated",
        survey_snapshot=survey_snapshot,
        analysis_data_snapshot=analysis_snapshot,
        status="READY",
        is_active=False,
        is_read=True,
        closed_at=None,
        selected_recommendation_id=None,
    )

    rows: list[SimpleNamespace] = []
    for item in normalized_items:
        detail_id = next_detail_id
        next_detail_id += 1
        reasoning_snapshot = dict(item.get("reasoning_snapshot") or {})
        reasoning_snapshot["recommendation_stage"] = recommendation_stage
        persisted_simulation_image_reference = _resolve_persistable_display_image_reference(
            simulation_image_url=item.get("simulation_image_url"),
            sample_image_url=item.get("sample_image_url"),
        )
        LegacyClientResultDetail.objects.create(
            detail_id=detail_id,
            result_id=result_id,
            hairstyle_id=item["style_id"],
            rank=item.get("rank", 0),
            similarity_score=float(item.get("match_score") or 0.0),
            final_score=float(item.get("match_score") or 0.0),
            simulated_image_url=persisted_simulation_image_reference,
            recommendation_reason=reasoning_snapshot.get("summary") or item.get("llm_explanation") or "",
            backend_recommendation_id=None,
            backend_client_ref_id=client.id,
            backend_capture_record_id=getattr(capture_record, "id", None),
            batch_id=batch_id,
            source="generated",
            style_name_snapshot=item["style_name"],
            style_description_snapshot=item.get("style_description", ""),
            keywords_json=list(item.get("keywords") or []),
            sample_image_url=item.get("sample_image_url"),
            regeneration_snapshot=regeneration_snapshot,
            reasoning_snapshot=reasoning_snapshot,
            is_chosen=False,
            chosen_at=None,
            is_sent_to_admin=False,
            sent_at=None,
            created_at_ts=created_at,
        )
        rows.append(
            _legacy_recommendation_namespace(
                client=client,
                capture_record=capture_record,
                batch_id=batch_id,
                created_at=created_at,
                item={
                    **item,
                    "reasoning_snapshot": reasoning_snapshot,
                    "regeneration_snapshot": regeneration_snapshot,
                    "simulation_image_url": persisted_simulation_image_reference,
                    "sample_image_url": item.get("sample_image_url"),
                },
                detail_id=detail_id,
            )
        )

    return str(batch_id), rows


def _legacy_style_label(style_id: int) -> tuple[str, str]:
    reference = _style_reference(style_id)
    return reference["style_name"], reference["style_description"]


def _normalize_runpod_face_shape(value: object) -> str | None:
    canonical = canonical_face_shape(str(value or "").strip())
    if canonical == "unknown":
        return None
    return canonical


def _has_displayable_image_reference(reference: object) -> bool:
    text = str(reference or "").strip()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return True
    if text.startswith(("/media/simulations/", "simulations/")):
        return True
    return False


def _coerce_runpod_golden_ratio_score(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_runpod_analysis_payload(*, reasoning_snapshot: dict, fallback_landmark_snapshot: dict | None) -> dict | None:
    runpod_snapshot = reasoning_snapshot.get("runpod")
    if not isinstance(runpod_snapshot, dict):
        return None

    face_shape = _normalize_runpod_face_shape(runpod_snapshot.get("face_shape_detected"))
    golden_ratio_score = _coerce_runpod_golden_ratio_score(runpod_snapshot.get("golden_ratio_score"))
    if face_shape is None and golden_ratio_score is None:
        return None

    return {
        "face_shape": face_shape,
        "golden_ratio_score": golden_ratio_score,
        "landmark_snapshot": dict(fallback_landmark_snapshot or {}),
        "analysis_source": "runpod_direct_primary",
    }


def _extract_local_fallback_analysis_payload(*, reasoning_snapshot: dict, fallback_landmark_snapshot: dict | None) -> dict | None:
    local_face_shape = reasoning_snapshot.get("face_shape")
    local_ratio_score = reasoning_snapshot.get("ratio_score")
    local_total_score = reasoning_snapshot.get("total_score")
    if local_face_shape is None and local_ratio_score is None and local_total_score is None:
        return None
    return {
        "face_shape": local_face_shape,
        "golden_ratio_score": local_ratio_score if local_ratio_score is not None else local_total_score,
        "landmark_snapshot": dict(fallback_landmark_snapshot or {}),
        "analysis_source": str(reasoning_snapshot.get("source") or "local_scoring_fallback"),
    }


def _analysis_payload_from_items(*, items: list[dict], fallback_landmark_snapshot: dict | None) -> dict | None:
    for item in items:
        reasoning_snapshot = dict(item.get("reasoning_snapshot") or {})
        runpod_payload = _extract_runpod_analysis_payload(
            reasoning_snapshot=reasoning_snapshot,
            fallback_landmark_snapshot=fallback_landmark_snapshot,
        )
        if runpod_payload is not None:
            return runpod_payload

        local_payload = _extract_local_fallback_analysis_payload(
            reasoning_snapshot=reasoning_snapshot,
            fallback_landmark_snapshot=fallback_landmark_snapshot,
        )
        if local_payload is not None:
            return local_payload
    return None


def _coerce_snapshot_source(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _resolve_consistent_snapshot_source(*, analysis_source: object, reasoning_source: object) -> str | None:
    canonical_analysis_source = _coerce_snapshot_source(analysis_source)
    canonical_reasoning_source = _coerce_snapshot_source(reasoning_source)
    if canonical_analysis_source and canonical_reasoning_source and canonical_analysis_source != canonical_reasoning_source:
        raise ValueError(
            f"Snapshot source mismatch: analysis={canonical_analysis_source}, reasoning={canonical_reasoning_source}"
        )
    return canonical_analysis_source or canonical_reasoning_source


def _normalize_persistable_recommendation_items(*, items: list[dict], analysis_snapshot: dict) -> list[dict]:
    normalized_items: list[dict] = []
    canonical_source = _coerce_snapshot_source(analysis_snapshot.get("source"))
    for item in items:
        normalized_item = dict(item)
        reasoning_snapshot = dict(normalized_item.get("reasoning_snapshot") or {})
        canonical_source = _resolve_consistent_snapshot_source(
            analysis_source=canonical_source,
            reasoning_source=reasoning_snapshot.get("source"),
        )
        if canonical_source:
            analysis_snapshot["source"] = canonical_source
            reasoning_snapshot["source"] = canonical_source
        normalized_item["reasoning_snapshot"] = reasoning_snapshot
        normalized_items.append(normalized_item)
    return normalized_items


def _resolve_persistable_display_image_reference(*, simulation_image_url: object, sample_image_url: object) -> str | None:
    persisted_reference = persist_simulation_image_reference(simulation_image_url)
    if not _has_displayable_image_reference(persisted_reference) and sample_image_url:
        persisted_reference = persist_simulation_image_reference(sample_image_url)
    if not _has_displayable_image_reference(persisted_reference):
        persisted_reference = persist_simulation_image_reference("tmp_capture_samples/lena.jpg")
    if not _has_displayable_image_reference(persisted_reference):
        return None
    return persisted_reference


def _runpod_direct_outcome_from_items(*, items: list[dict]) -> dict | None:
    for item in items:
        reasoning_snapshot = dict(item.get("reasoning_snapshot") or {})
        runpod_direct = reasoning_snapshot.get("runpod_direct")
        if not isinstance(runpod_direct, dict):
            continue
        return {
            "status": str(runpod_direct.get("status") or "unknown"),
            "reason": (str(runpod_direct.get("reason")) if runpod_direct.get("reason") else None),
            "invoked": bool(runpod_direct.get("invoked")),
        }
    return None


def _canonical_selected_recommendation_id(
    *,
    selected_detail: "LegacyClientResultDetail | None",
    direct_consultation: bool,
) -> int | None:
    if direct_consultation or selected_detail is None:
        return None
    backend_recommendation_id = getattr(selected_detail, "backend_recommendation_id", None)
    if backend_recommendation_id not in (None, ""):
        try:
            return int(backend_recommendation_id)
        except (TypeError, ValueError):
            pass
    return int(getattr(selected_detail, "detail_id", None) or 0) or None


def _selected_image_url_for_result(
    *,
    selected_detail: "LegacyClientResultDetail | None",
    direct_consultation: bool,
) -> str | None:
    if direct_consultation or selected_detail is None:
        return None
    return getattr(selected_detail, "simulated_image_url", None)


def _normalize_text_value(value: object) -> str:
    return str(value or "").strip()


def _survey_payload_from_gender_questions(*, client: "Client", payload: dict) -> dict | None:
    q1 = _normalize_text_value(payload.get("q1"))
    q2 = _normalize_text_value(payload.get("q2"))
    q3 = _normalize_text_value(payload.get("q3"))
    q4 = _normalize_text_value(payload.get("q4"))
    q5 = _normalize_text_value(payload.get("q5"))
    q6 = _normalize_text_value(payload.get("q6"))

    if not any((q1, q2, q3, q4, q5, q6)):
        return None

    gender = _normalize_text_value(getattr(client, "gender", None)).lower()

    is_male = gender in {"male", "m"}

    if is_male:
        target_length_map = {
            "아주 짧고 깔끔하게": "short",
            "너무 짧지 않게 자연스럽게": "medium",
            "길이감 있게 남기고 싶음": "long",
        }
        target_vibe_map = {
            "단정한": "chic",
            "부드러운": "natural",
            "트렌디한": "chic",
        }
        scalp_type_map = {
            "펌 없이 깔끔하게": "straight",
            "자연스러운 볼륨 정도": "waved",
            "컬감이 느껴지는 스타일": "curly",
        }
    else:
        target_length_map = {
            "짧게": "short",
            "중간 길이": "medium",
            "길게": "long",
            "유지": "medium",
        }
        target_vibe_map = {
            "내추럴한": "natural",
            "세련된": "chic",
            "사랑스러운": "cute",
            "고급스러운": "elegant",
        }
        scalp_type_map = {
            "생머리 느낌": "straight",
            "끝선 위주 자연스러운 컬": "waved",
            "전체적으로 웨이브감": "curly",
        }

    mapped_payload = {
        "target_length": target_length_map.get(q1, "unknown"),
        "target_vibe": target_vibe_map.get(q6 if is_male else q5, "unknown"),
        "scalp_type": scalp_type_map.get(q5 if is_male else q4, "unknown"),
        "hair_colour": "unknown",
        "budget_range": "unknown",
    }

    logger.info(
        "[survey_question_mapping] client_id=%s gender=%s target_length=%s target_vibe=%s scalp_type=%s",
        client.id,
        gender or "unknown",
        mapped_payload["target_length"],
        mapped_payload["target_vibe"],
        mapped_payload["scalp_type"],
    )
    return mapped_payload


def normalize_survey_payload(*, client: "Client", payload: dict) -> dict:
    if any(payload.get(field) for field in ("target_length", "target_vibe", "scalp_type", "hair_colour", "budget_range")):
        return {
            "target_length": payload.get("target_length"),
            "target_vibe": payload.get("target_vibe"),
            "scalp_type": payload.get("scalp_type"),
            "hair_colour": payload.get("hair_colour"),
            "budget_range": payload.get("budget_range"),
        }

    mapped_payload = _survey_payload_from_gender_questions(client=client, payload=payload)
    if mapped_payload is not None:
        return mapped_payload

    return {
        "target_length": payload.get("target_length"),
        "target_vibe": payload.get("target_vibe"),
        "scalp_type": payload.get("scalp_type"),
        "hair_colour": payload.get("hair_colour"),
        "budget_range": payload.get("budget_range"),
    }


def get_latest_capture_attempt(client: "Client"):
    return get_latest_capture(client)


def _resolve_capture_record_relation(capture_record) -> "CaptureRecord | None":
    return capture_record


def build_survey_snapshot(client: "Client") -> dict | None:
    survey = get_latest_survey(client)
    if not survey:
        return None
    return {
        "target_length": survey.target_length,
        "target_vibe": survey.target_vibe,
        "scalp_type": survey.scalp_type,
        "hair_colour": survey.hair_colour,
        "budget_range": survey.budget_range,
        "preference_vector": survey.preference_vector or [],
        "age_profile": build_client_age_profile(client),
        "created_at": survey.created_at.isoformat(),
    }


def build_recommendation_regeneration_snapshot(
    *,
    client: "Client",
    survey,
    analysis: "FaceAnalysis | None",
    source: str,
    capture_record: "CaptureRecord | None" = None,
    recommendation_stage: str = "initial",
) -> dict:
    return {
        "version": "vector-only-v1",
        "source": source,
        "client_id": client.id,
        "recommendation_stage": recommendation_stage,
        "context": {
            "capture_record_id": (capture_record.id if capture_record else None),
            "survey_id": getattr(survey, "id", None),
            "analysis_id": (analysis.id if analysis else None),
        },
        "survey_data": {
            "target_length": getattr(survey, "target_length", None),
            "target_vibe": getattr(survey, "target_vibe", None),
            "scalp_type": getattr(survey, "scalp_type", None),
            "hair_colour": getattr(survey, "hair_colour", None),
            "budget_range": getattr(survey, "budget_range", None),
            "preference_vector": getattr(survey, "preference_vector", None) or [],
            "age_profile": build_client_age_profile(client),
        },
        "analysis_data": (
            {
                "face_shape": analysis.face_shape,
                "golden_ratio_score": analysis.golden_ratio_score,
                "landmark_snapshot": analysis.landmark_snapshot,
            }
            if analysis
            else None
        ),
    }


def upsert_survey(client: "Client", payload: dict) -> "Survey":
    normalized_payload = normalize_survey_payload(client=client, payload=payload)
    preference_vector = build_preference_vector(
        target_length=normalized_payload.get("target_length"),
        target_vibe=normalized_payload.get("target_vibe"),
        scalp_type=normalized_payload.get("scalp_type"),
        hair_colour=normalized_payload.get("hair_colour"),
        budget_range=normalized_payload.get("budget_range"),
    )
    legacy_survey = _persist_legacy_survey(
        client=client,
        normalized_payload=normalized_payload,
        preference_vector=preference_vector,
    )
    if legacy_survey is not None:
        return legacy_survey
    return _legacy_survey_namespace(
        survey_id=0,
        client=client,
        normalized_payload=normalized_payload,
        preference_vector=preference_vector,
        created_at=timezone.now(),
    )


def persist_generated_batch(
    *,
    client: "Client",
    capture_record: "CaptureRecord | None",
    survey,
    analysis: "FaceAnalysis",
    recommendation_stage: str = "initial",
    precomputed_items: list[dict] | None = None,
) -> tuple[str, list["FormerRecommendation"]]:
    styles_by_id = ensure_catalog_styles()
    survey_payload = {
        "target_length": getattr(survey, "target_length", None),
        "target_vibe": getattr(survey, "target_vibe", None),
        "scalp_type": getattr(survey, "scalp_type", None),
        "hair_colour": getattr(survey, "hair_colour", None),
        "budget_range": getattr(survey, "budget_range", None),
    }
    items = list(precomputed_items or [])
    if precomputed_items is None:
        items = generate_recommendation_batch(
            client_id=client.id,
            survey_data=survey_payload,
            analysis_data={
                "face_shape": analysis.face_shape,
                "golden_ratio_score": analysis.golden_ratio_score,
                "image_url": resolve_storage_reference(analysis.image_url),
                "landmark_snapshot": analysis.landmark_snapshot,
            },
            styles_by_id=styles_by_id,
            scoring_weights=(RETRY_SCORING_WEIGHTS if recommendation_stage == "retry" else None),
        )
    regeneration_snapshot = build_recommendation_regeneration_snapshot(
        client=client,
        survey=survey,
        analysis=analysis,
        source="generated",
        capture_record=capture_record,
        recommendation_stage=recommendation_stage,
    )
    relation_capture_record = _resolve_capture_record_relation(capture_record)

    legacy_result = _persist_legacy_generated_batch(
        client=client,
        capture_record=relation_capture_record,
        survey=survey,
        analysis=analysis,
        items=items,
        regeneration_snapshot=regeneration_snapshot,
        recommendation_stage=recommendation_stage,
    )
    if legacy_result is not None:
        return legacy_result
    raise RuntimeError("Legacy result tables are required for recommendation writes.")


def serialize_recommendation_row(row: "FormerRecommendation") -> dict:
    reasoning_snapshot = row.reasoning_snapshot or {}
    style_reference = _style_reference(
        row.style_id_snapshot,
        styles_by_id=({row.style_id_snapshot: row.style} if row.style else None),
    )
    uses_vector_only_policy = bool(row.regeneration_snapshot)
    regeneration_attempts_used = int(reasoning_snapshot.get("regeneration_attempts_used") or 0)
    regeneration_attempts_allowed = (
        REGENERATION_MAX_ATTEMPTS if uses_vector_only_policy else 0
    )
    regeneration_remaining_count = max(0, regeneration_attempts_allowed - regeneration_attempts_used)
    can_regenerate_simulation = uses_vector_only_policy and regeneration_remaining_count > 0
    sample_image_url = style_reference["sample_image_url"] or resolve_storage_reference(row.sample_image_url)
    simulation_image_url = None if uses_vector_only_policy else resolve_storage_reference(row.simulation_image_url)
    reference_images = []
    if sample_image_url:
        reference_images.append(
            {
                "image_url": sample_image_url,
                "description": row.style_description_snapshot or style_reference["style_description"],
            }
        )
    return {
        "recommendation_id": row.id,
        "legacy_client_id": get_legacy_client_id(client=row.client),
        "batch_id": row.batch_id,
        "source": row.source,
        "style_id": row.style_id_snapshot,
        "style_name": row.style_name_snapshot or style_reference["style_name"],
        "style_description": row.style_description_snapshot or style_reference["style_description"],
        "keywords": row.keywords or style_reference["keywords"],
        "sample_image_url": sample_image_url,
        "reference_images": reference_images,
        "simulation_image_url": simulation_image_url,
        "synthetic_image_url": simulation_image_url,
        "llm_explanation": row.llm_explanation or "",
        "reasoning": reasoning_snapshot.get("summary") or row.llm_explanation or "",
        "reasoning_snapshot": reasoning_snapshot,
        "match_score": row.match_score or 0.0,
        "rank": row.rank,
        "is_chosen": row.is_chosen,
        "image_policy": ("vector_only" if uses_vector_only_policy else "legacy_asset_store"),
        "can_regenerate_simulation": can_regenerate_simulation,
        "regeneration_remaining_count": regeneration_remaining_count,
        "regeneration_policy": (
            {
                **REGENERATION_POLICY,
                "attempts_allowed": regeneration_attempts_allowed,
                "attempts_used": regeneration_attempts_used,
            }
            if uses_vector_only_policy
            else None
        ),
        "created_at": row.created_at,
    }


def _serialize_row(row: "FormerRecommendation") -> dict:
    return serialize_recommendation_row(row)


def _get_recommendation_stage(rows: "list[FormerRecommendation]") -> str:
    if not rows:
        return "initial"
    snapshot = rows[0].reasoning_snapshot or {}
    return str(snapshot.get("recommendation_stage") or "initial")


def _build_retry_recommendation_meta(*, rows: "list[FormerRecommendation]", has_active_consultation: bool) -> dict:
    recommendation_stage = _get_recommendation_stage(rows)
    attempts_used = 1 if recommendation_stage == "retry" else 0
    has_selection = any(row.is_chosen for row in rows)
    can_retry = bool(rows) and recommendation_stage == "initial" and not has_active_consultation and not has_selection
    remaining_count = max(0, RETRY_RECOMMENDATION_MAX_ATTEMPTS - attempts_used) if can_retry else 0

    if not rows:
        retry_state = "not_ready"
        retry_block_reason = "initial_recommendations_missing"
    elif has_active_consultation:
        retry_state = "consultation_locked"
        retry_block_reason = "consultation_started"
    elif has_selection:
        retry_state = "selection_locked"
        retry_block_reason = "recommendation_already_selected"
    elif recommendation_stage == "retry":
        retry_state = "retry_consumed"
        retry_block_reason = "retry_already_used"
    else:
        retry_state = "available"
        retry_block_reason = None

    return {
        "recommendation_stage": recommendation_stage,
        "can_retry_recommendations": can_retry,
        "retry_state": retry_state,
        "consultation_locked": has_active_consultation,
        "retry_block_reason": retry_block_reason,
        "retry_recommendations_remaining_count": remaining_count,
        "retry_recommendations_policy": {
            **RETRY_RECOMMENDATION_POLICY,
            "attempts_allowed": RETRY_RECOMMENDATION_MAX_ATTEMPTS,
            "attempts_used": attempts_used,
        },
    }


def _legacy_recommendation_stage(items: list[dict]) -> str:
    if not items:
        return "initial"
    snapshot = dict(items[0].get("reasoning_snapshot") or {})
    return str(snapshot.get("recommendation_stage") or "initial")


def _build_legacy_retry_recommendation_meta(*, items: list[dict], has_active_consultation: bool) -> dict:
    recommendation_stage = _legacy_recommendation_stage(items)
    attempts_used = 1 if recommendation_stage == "retry" else 0
    has_selection = any(bool(item.get("is_chosen")) for item in items)
    is_generated = all(str(item.get("source") or "").startswith("generated") for item in items)
    can_retry = bool(items) and is_generated and recommendation_stage == "initial" and not has_active_consultation and not has_selection

    if not items:
        retry_state = "not_ready"
        retry_block_reason = "initial_recommendations_missing"
    elif has_active_consultation:
        retry_state = "consultation_locked"
        retry_block_reason = "consultation_started"
    elif has_selection:
        retry_state = "selection_locked"
        retry_block_reason = "recommendation_already_selected"
    elif recommendation_stage == "retry":
        retry_state = "retry_consumed"
        retry_block_reason = "retry_already_used"
    elif not is_generated:
        retry_state = "legacy_locked"
        retry_block_reason = "legacy_result_only"
    else:
        retry_state = "available"
        retry_block_reason = None

    return {
        "recommendation_stage": recommendation_stage,
        "can_retry_recommendations": can_retry,
        "retry_state": retry_state,
        "consultation_locked": has_active_consultation,
        "retry_block_reason": retry_block_reason,
        "retry_recommendations_remaining_count": (
            max(0, RETRY_RECOMMENDATION_MAX_ATTEMPTS - attempts_used)
            if can_retry else 0
        ),
        "retry_recommendations_policy": (
            {
                **RETRY_RECOMMENDATION_POLICY,
                "attempts_allowed": RETRY_RECOMMENDATION_MAX_ATTEMPTS,
                "attempts_used": attempts_used,
            }
            if is_generated else None
        ),
    }


def _scoring_weights_for_stage(recommendation_stage: str):
    if recommendation_stage == "retry":
        return RETRY_SCORING_WEIGHTS
    return None


def _build_empty_response(*, source: str, message: str, next_action: str | None = None, next_actions: list[str] | None = None) -> dict:
    payload = {
        "status": "empty",
        "source": source,
        "message": message,
        "items": [],
    }
    if next_action:
        payload["next_action"] = next_action
    if next_actions:
        payload["next_actions"] = next_actions
    return payload


def _normalize_recommendation_item_contract(item: dict) -> dict:
    normalized = dict(item)
    sample_image_url = resolve_storage_reference(normalized.get("sample_image_url"))
    simulation_image_url = resolve_storage_reference(
        normalized.get("simulation_image_url") or normalized.get("synthetic_image_url")
    )
    display_image_url = simulation_image_url or sample_image_url
    has_displayable_simulation = bool(simulation_image_url)

    simulation_source = str(normalized.get("simulation_source") or "").strip()
    if not simulation_source:
        source = str(normalized.get("source") or "").strip()
        if has_displayable_simulation:
            simulation_source = "local_mock" if source == "local_mock" else "simulation"
        elif sample_image_url:
            simulation_source = "sample"
        else:
            simulation_source = "none"

    simulation_status = str(normalized.get("simulation_status") or "").strip()
    simulation_status_reason = str(normalized.get("simulation_status_reason") or "").strip()
    if not simulation_status:
        if has_displayable_simulation:
            simulation_status = "ready"
        elif sample_image_url:
            simulation_status = "pending"
        else:
            simulation_status = "missing"
    if not simulation_status_reason:
        if has_displayable_simulation:
            simulation_status_reason = (
                "local_mock_reference"
                if simulation_source == "local_mock"
                else "primary_simulation_ready"
            )
        elif sample_image_url:
            simulation_status_reason = "sample_reference_only"
        else:
            simulation_status_reason = "simulation_asset_missing"

    normalized["sample_image_url"] = sample_image_url
    normalized["simulation_image_url"] = simulation_image_url
    normalized["synthetic_image_url"] = simulation_image_url
    normalized["display_image_url"] = display_image_url
    normalized["has_displayable_simulation"] = has_displayable_simulation
    normalized["simulation_source"] = simulation_source
    normalized["simulation_status"] = simulation_status
    normalized["simulation_status_reason"] = simulation_status_reason
    return normalized


def _build_simulation_contract_meta(
    *,
    items: list[dict],
    client: "Client | None" = None,
    latest_capture=None,
    latest_analysis=None,
    default_reason: str | None = None,
) -> dict:
    item_count = len(items)
    displayable_simulation_count = sum(1 for item in items if item.get("has_displayable_simulation"))
    primary_simulation_count = sum(1 for item in items if item.get("simulation_source") == "simulation")
    local_mock_count = sum(1 for item in items if item.get("simulation_source") == "local_mock")
    sample_reference_count = sum(1 for item in items if item.get("simulation_source") == "sample")
    has_displayable_simulation = displayable_simulation_count > 0
    simulation_ready = item_count > 0 and displayable_simulation_count == item_count

    if default_reason:
        simulation_status_reason = default_reason
    elif local_mock_count == item_count and item_count > 0:
        simulation_status_reason = "local_mock_recommendations_ready"
    elif simulation_ready:
        simulation_status_reason = "all_simulations_ready"
    elif has_displayable_simulation:
        simulation_status_reason = "partial_simulations_ready"
    elif sample_reference_count == item_count and item_count > 0:
        simulation_status_reason = "sample_references_only"
    else:
        simulation_status_reason = "simulation_assets_missing"

    if local_mock_count == item_count and item_count > 0:
        display_gate_status = "mock_ready"
    elif simulation_ready:
        display_gate_status = "ready"
    elif has_displayable_simulation:
        display_gate_status = "partial_ready"
    elif sample_reference_count == item_count and item_count > 0:
        display_gate_status = "sample_only"
    elif simulation_status_reason == "recommendations_processing":
        display_gate_status = "processing"
    elif simulation_status_reason in {"capture_retry_required", "capture_data_not_ready", "analysis_input_incomplete"}:
        display_gate_status = "awaiting_capture"
    elif simulation_status_reason == "survey_or_capture_required":
        display_gate_status = "awaiting_input"
    else:
        display_gate_status = "blocked"

    return {
        "response_kind": "recommendation_list",
        "response_contract_version": 3,
        "canonical_display_image_field": "display_image_url",
        "primary_simulation_image_field": "simulation_image_url",
        "legacy_simulation_image_field": "synthetic_image_url",
        "display_gate_target_field": "simulation_image_url",
        "display_gate_status": display_gate_status,
        "display_gate_reason": simulation_status_reason,
        "display_gate_ready_count": displayable_simulation_count,
        "display_gate_target_count": item_count,
        "recommendation_item_count": item_count,
        "has_displayable_simulation": has_displayable_simulation,
        "simulation_ready": simulation_ready,
        "displayable_simulation_count": displayable_simulation_count,
        "primary_simulation_count": primary_simulation_count,
        "sample_reference_count": sample_reference_count,
        "local_mock_count": local_mock_count,
        "simulation_status_reason": simulation_status_reason,
        "current_capture_id": getattr(latest_capture, "id", None) or getattr(latest_capture, "analysis_id", None),
        "current_analysis_id": getattr(latest_analysis, "id", None) or getattr(latest_analysis, "analysis_id", None),
        "client_id": (client.id if client is not None else None),
        "legacy_client_id": (get_legacy_client_id(client=client) if client is not None else None),
    }


def serialize_capture_status(record: "CaptureRecord") -> dict:
    privacy_snapshot = record.privacy_snapshot or {}
    payload = {
        "record_id": record.id,
        "client_id": record.client_id,
        "legacy_client_id": get_legacy_client_id(client=record.client),
        "status": record.status.lower(),
        "face_count": record.face_count,
        "error_note": record.error_note,
        "landmark_snapshot": record.landmark_snapshot,
        "deidentified_image_url": resolve_storage_reference(record.deidentified_path),
        "privacy_snapshot": privacy_snapshot,
        "image_storage_policy": privacy_snapshot.get("storage_policy", "asset_store"),
        "storage_snapshot": build_storage_snapshot(
            original_path=record.original_path,
            processed_path=record.processed_path,
            deidentified_path=record.deidentified_path,
        ),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if record.status in {"NEEDS_RETAKE", "FAILED"}:
        payload["next_action"] = "capture"
    return payload


def regenerate_recommendation_simulation(
    *,
    recommendation_id: int | None = None,
    regeneration_snapshot: dict | None = None,
    style_id: int | None = None,
) -> dict:
    selected_legacy_client = None
    selected_legacy_item = None
    if recommendation_id is not None:
        legacy_client, legacy_item = find_legacy_recommendation_context(recommendation_id=recommendation_id)
        if legacy_client is None or legacy_item is None:
            raise ValueError("The selected recommendation could not be found.")
        selected_legacy_client = legacy_client
        selected_legacy_item = legacy_item
        regeneration_snapshot = regeneration_snapshot or dict(selected_legacy_item.get("regeneration_snapshot") or {})
        style_id = style_id or int(selected_legacy_item.get("style_id") or 0)
        attempts_used = int((selected_legacy_item.get("reasoning_snapshot") or {}).get("regeneration_attempts_used") or 0)
        if attempts_used >= REGENERATION_MAX_ATTEMPTS:
            raise ValueError("This recommendation has already used its one allowed regeneration.")

    if not regeneration_snapshot and selected_legacy_client is not None:
        regeneration_snapshot = build_recommendation_regeneration_snapshot(
            client=selected_legacy_client,
            survey=(get_latest_survey(selected_legacy_client) or build_default_survey_context(selected_legacy_client.id)),
            analysis=get_latest_analysis(selected_legacy_client),
            source=str(selected_legacy_item.get("source") or "legacy_bridge"),
            capture_record=get_latest_capture(selected_legacy_client),
            recommendation_stage="legacy",
        )

    if not regeneration_snapshot:
        raise ValueError("No regeneration snapshot is available for this recommendation.")
    if style_id is None:
        raise ValueError("Style information is required to regenerate the simulation.")

    survey_data = dict(regeneration_snapshot.get("survey_data") or {})
    analysis_data = dict(regeneration_snapshot.get("analysis_data") or {})
    client_id = int(regeneration_snapshot.get("client_id") or 0)
    recommendation_stage = str(regeneration_snapshot.get("recommendation_stage") or "initial")
    if client_id <= 0:
        raise ValueError("The regeneration snapshot is missing client context.")

    styles_by_id = ensure_catalog_styles()
    generated_items = generate_recommendation_batch(
        client_id=client_id,
        survey_data=survey_data,
        analysis_data=analysis_data,
        styles_by_id=styles_by_id,
        scoring_weights=_scoring_weights_for_stage(recommendation_stage),
    )
    regenerated_card = next(
        (item for item in generated_items if int(item.get("style_id", -1)) == int(style_id)),
        None,
    )
    if regenerated_card is None:
        raise ValueError("Could not regenerate the requested style from the current snapshot.")

    if selected_legacy_client is not None and selected_legacy_item is not None and recommendation_id is not None:
        result_row, detail_row = _legacy_result_and_detail_for_recommendation(
            client=selected_legacy_client,
            recommendation_id=int(recommendation_id),
        )
        if result_row is None or detail_row is None:
            raise ValueError("The selected recommendation could not be found.")

        reasoning_snapshot = dict(detail_row.reasoning_snapshot or {})
        attempts_used = int(reasoning_snapshot.get("regeneration_attempts_used") or 0) + 1
        reasoning_snapshot["regenerated"] = True
        reasoning_snapshot["regeneration_source"] = regeneration_snapshot.get("source")
        reasoning_snapshot["regeneration_attempts_used"] = attempts_used
        reasoning_snapshot["regeneration_attempts_allowed"] = REGENERATION_MAX_ATTEMPTS
        reasoning_snapshot["regeneration_remaining_count"] = max(0, REGENERATION_MAX_ATTEMPTS - attempts_used)
        reasoning_snapshot["regeneration_policy"] = dict(REGENERATION_POLICY)

        regenerated_score = regenerated_card.get("match_score")
        try:
            regenerated_score = float(
                regenerated_score
                if regenerated_score is not None
                else (detail_row.final_score or detail_row.similarity_score or 0.0)
            )
        except (TypeError, ValueError):
            regenerated_score = float(detail_row.final_score or detail_row.similarity_score or 0.0)

        detail_row.final_score = regenerated_score
        detail_row.similarity_score = regenerated_score
        detail_row.recommendation_reason = (
            regenerated_card.get("llm_explanation")
            or detail_row.recommendation_reason
            or ""
        )
        detail_row.reasoning_snapshot = reasoning_snapshot
        detail_row.regeneration_snapshot = regeneration_snapshot
        detail_row.simulated_image_url = regenerated_card.get("simulation_image_url")
        detail_row.save(
            update_fields=[
                "final_score",
                "similarity_score",
                "recommendation_reason",
                "reasoning_snapshot",
                "regeneration_snapshot",
                "simulated_image_url",
            ]
        )

        refreshed_item = _find_legacy_recommendation_item(
            client=selected_legacy_client,
            recommendation_id=int(recommendation_id),
        ) or selected_legacy_item
        reference_images = list(refreshed_item.get("reference_images") or [])
        if not reference_images and refreshed_item.get("sample_image_url"):
            reference_images.append(
                {
                    "image_url": refreshed_item.get("sample_image_url"),
                    "description": refreshed_item.get("style_description") or "",
                }
            )
        card = {
            **refreshed_item,
            "reference_images": reference_images,
            "simulation_image_url": regenerated_card.get("simulation_image_url"),
            "synthetic_image_url": regenerated_card.get("synthetic_image_url"),
            "llm_explanation": regenerated_card.get("llm_explanation") or refreshed_item.get("llm_explanation") or "",
            "reasoning": regenerated_card.get("reasoning") or refreshed_item.get("reasoning") or "",
            "reasoning_snapshot": reasoning_snapshot,
            "image_policy": "vector_only",
            "can_regenerate_simulation": False,
            "regeneration_remaining_count": max(0, REGENERATION_MAX_ATTEMPTS - attempts_used),
            "regeneration_policy": {
                **REGENERATION_POLICY,
                "attempts_allowed": REGENERATION_MAX_ATTEMPTS,
                "attempts_used": attempts_used,
            },
            "match_score": regenerated_score,
        }
    else:
        style_reference = _style_reference(int(style_id), styles_by_id=styles_by_id)
        card = {
            "recommendation_id": None,
            "batch_id": None,
            "source": "generated",
            "style_id": int(style_id),
            "style_name": regenerated_card.get("style_name") or style_reference["style_name"],
            "style_description": regenerated_card.get("style_description") or style_reference["style_description"],
            "keywords": regenerated_card.get("keywords") or style_reference["keywords"],
            "sample_image_url": regenerated_card.get("sample_image_url") or style_reference["sample_image_url"],
            "reference_images": [],
            "simulation_image_url": None,
            "synthetic_image_url": None,
            "llm_explanation": regenerated_card.get("llm_explanation") or "",
            "reasoning": regenerated_card.get("reasoning") or "",
            "reasoning_snapshot": {
                **(regenerated_card.get("reasoning_snapshot") or {}),
                "regenerated": True,
                "regeneration_source": regeneration_snapshot.get("source"),
                "regeneration_attempts_used": 1,
                "regeneration_attempts_allowed": REGENERATION_MAX_ATTEMPTS,
                "regeneration_remaining_count": 0,
                "regeneration_policy": dict(REGENERATION_POLICY),
            },
            "image_policy": "vector_only",
            "can_regenerate_simulation": False,
            "regeneration_remaining_count": 0,
            "regeneration_policy": {
                **REGENERATION_POLICY,
                "attempts_allowed": REGENERATION_MAX_ATTEMPTS,
                "attempts_used": 1,
            },
            "match_score": regenerated_card.get("match_score") or 0.0,
            "rank": regenerated_card.get("rank") or 1,
            "is_chosen": False,
            "created_at": timezone.now(),
        }

    card["simulation_image_url"] = regenerated_card.get("simulation_image_url")
    card["synthetic_image_url"] = regenerated_card.get("synthetic_image_url")
    card["image_policy"] = "vector_only"
    card["can_regenerate_simulation"] = False
    card["regeneration_remaining_count"] = 0
    card["reasoning"] = regenerated_card.get("reasoning") or card.get("reasoning") or ""
    card["llm_explanation"] = regenerated_card.get("llm_explanation") or card.get("llm_explanation") or ""

    return {
        "status": "success",
        "recommendation_id": (
            int(selected_legacy_item["recommendation_id"]) if selected_legacy_item else None
        ),
        "style_id": int(style_id),
        "image_policy": "vector_only",
        "can_regenerate_simulation": False,
        "regeneration_remaining_count": 0,
        "regeneration_policy": {
            **REGENERATION_POLICY,
            "attempts_allowed": REGENERATION_MAX_ATTEMPTS,
            "attempts_used": 1,
        },
        "simulation_image_url": card.get("simulation_image_url"),
        "synthetic_image_url": card.get("synthetic_image_url"),
        "card": card,
        "message": "A regenerated simulation payload is ready.",
    }


def get_former_recommendations(client: "Client") -> dict:
    legacy_items = get_legacy_former_recommendation_items(client=client) or []
    if not legacy_items:
        return _build_empty_response(
            source="former_recommendations",
            message="아직 과거의 추천 내역이 없습니다.",
            next_actions=["trend", "capture"],
        )
    return {
        "status": "ready",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "source": "former_recommendations",
        "items": legacy_items[:5],
    }


def _ensure_current_batch(
    client: "Client",
    *,
    latest_capture,
    latest_survey,
    latest_analysis,
    legacy_items: list[dict],
) -> tuple[str | None, list[dict], str | None]:
    if not latest_capture or not latest_analysis:
        return None, [], "needs_capture"

    current_assets = _prepare_current_legacy_assets(
        latest_analysis=latest_analysis,
        legacy_items=legacy_items,
    )
    if current_assets.is_ready:
        return current_assets.batch_id, current_assets.items, None
    if current_assets.has_pending_assets:
        return current_assets.batch_id, current_assets.items, "processing"

    survey_context = latest_survey or build_default_survey_context(client.id)
    batch_id, _ = persist_generated_batch(
        client=client,
        capture_record=latest_capture,
        survey=survey_context,
        analysis=latest_analysis,
    )
    refreshed_items = get_legacy_former_recommendation_items(client=client) or []
    refreshed_assets = _prepare_current_legacy_assets(
        latest_analysis=latest_analysis,
        legacy_items=refreshed_items,
    )
    if refreshed_assets.is_ready:
        return batch_id, refreshed_assets.items, None
    if refreshed_assets.has_pending_assets:
        return batch_id, refreshed_assets.items, "processing"
    return batch_id, refreshed_assets.items, None


def _build_local_mock_recommendations(*, client: "Client", latest_survey, latest_analysis: "FaceAnalysis | None") -> dict:
    styles_by_id = ensure_catalog_styles()
    mock_style_ids = [profile.style_id for profile in STYLE_CATALOG[:3]]
    if not mock_style_ids:
        return _build_empty_response(
            source="local_mock",
            message="Local mock recommendations are enabled, but no style catalog data is available yet.",
            next_action="capture",
        )

    target_vibe = getattr(latest_survey, "target_vibe", None) or "natural"
    target_length = getattr(latest_survey, "target_length", None) or "medium"
    face_shape = getattr(latest_analysis, "face_shape", None) or "balanced"
    items: list[dict] = []

    for rank, style_id in enumerate(mock_style_ids, start=1):
        reference = _style_reference(style_id, styles_by_id=styles_by_id)
        image_url = reference["sample_image_url"]
        items.append(
            {
                "recommendation_id": None,
                "batch_id": None,
                "source": "local_mock",
                "style_id": style_id,
                "style_name": reference["style_name"],
                "style_description": reference["style_description"],
                "keywords": reference["keywords"],
                "sample_image_url": image_url,
                "reference_images": (
                    [{"image_url": image_url, "description": reference["style_description"]}] if image_url else []
                ),
                "simulation_image_url": image_url,
                "synthetic_image_url": image_url,
                "llm_explanation": "로컬 테스트용 예시 결과입니다. 실제 모델 결과가 연결되면 이 설명은 교체됩니다.",
                "reasoning": f"로컬 테스트용 예시 결과입니다. {target_length} 길이감과 {target_vibe} 분위기, {face_shape} 인상을 기준으로 표시했습니다.",
                "reasoning_snapshot": {
                    "source": "local_mock",
                    "is_mock": True,
                    "client_id": client.id,
                },
                "image_policy": "local_mock",
                "can_regenerate_simulation": False,
                "regeneration_remaining_count": 0,
                "regeneration_policy": None,
                "match_score": float(max(70, 95 - ((rank - 1) * 7))),
                "rank": rank,
                "is_chosen": False,
                "created_at": timezone.now(),
            }
        )

    return {
        "status": "ready",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "source": "local_mock",
        "batch_id": None,
        "message": "Local mock recommendations are being shown because the analysis result is not ready yet.",
        "items": items,
        "next_actions": ["consultation"],
    }


def _has_active_consultation_state(*, client: "Client") -> bool:
    return bool(get_legacy_active_consultation_items(client=client))


def _coerce_batch_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return uuid.UUID(text)
        except (TypeError, ValueError, AttributeError):
            return uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-batch:{text}")
    return uuid.uuid4()


def _find_legacy_recommendation_item(
    *,
    client: "Client",
    recommendation_id: int | None = None,
    style_id: int | None = None,
    selected_image_url: str | None = None,
) -> dict | None:
    items = get_legacy_former_recommendation_items(client=client)
    if not items:
        return None

    if recommendation_id is not None:
        recommendation_key = str(recommendation_id)
        return next(
            (item for item in items if str(item.get("recommendation_id")) == recommendation_key),
            None,
        )

    if style_id is not None:
        return next((item for item in items if int(item.get("style_id") or 0) == int(style_id)), None)

    if selected_image_url:
        target = str(selected_image_url or "").strip()
        resolved_target = str(resolve_storage_reference(target) or "").strip()
        for item in items:
            for key in ("simulation_image_url", "synthetic_image_url", "sample_image_url"):
                candidate = str(item.get(key) or "").strip()
                if not candidate:
                    continue
                resolved_candidate = str(resolve_storage_reference(candidate) or "").strip()
                if target in {candidate, resolved_candidate} or (
                    resolved_target and resolved_target in {candidate, resolved_candidate}
                ):
                    return item

    return next((item for item in items if item.get("is_chosen")), items[0])


def _find_current_recommendation_item_for_consultation(
    *,
    client: "Client",
    recommendation_id: int | None = None,
    style_id: int | None = None,
    selected_image_url: str | None = None,
) -> dict | None:
    payload = get_current_recommendations(client)
    items = list(payload.get("items") or [])
    if not items:
        return None
    if recommendation_id is not None:
        recommendation_key = str(recommendation_id)
        matched = next(
            (item for item in items if str(item.get("recommendation_id")) == recommendation_key),
            None,
        )
        if matched is not None:
            return matched
    if style_id is not None:
        matched = next((item for item in items if int(item.get("style_id") or 0) == int(style_id)), None)
        if matched is not None:
            return matched
    if selected_image_url:
        target = str(selected_image_url or "").strip()
        resolved_target = str(resolve_storage_reference(target) or "").strip()
        for item in items:
            for key in ("simulation_image_url", "synthetic_image_url", "sample_image_url"):
                candidate = str(item.get(key) or "").strip()
                if not candidate:
                    continue
                resolved_candidate = str(resolve_storage_reference(candidate) or "").strip()
                if target in {candidate, resolved_candidate} or (
                    resolved_target and resolved_target in {candidate, resolved_candidate}
                ):
                    return item
    return next((item for item in items if item.get("is_chosen")), items[0])


def _materialize_direct_consultation_current_recommendation(
    *,
    client: "Client",
    legacy_client_id,
    source: str,
    recommendation_id: int | None,
    style_id: int | None,
    selected_image_url: str | None,
    survey_snapshot: dict | None,
    analysis_snapshot: dict | None,
    admin: "AdminAccount | None",
    designer,
    now,
) -> tuple[LegacyClientResult | None, LegacyClientResultDetail | None, int | None, str | None]:
    legacy_item = _find_legacy_recommendation_item(
        client=client,
        recommendation_id=recommendation_id,
        style_id=style_id,
        selected_image_url=selected_image_url,
    )
    item_origin = "legacy_former_recommendations"
    if not legacy_item:
        legacy_item = _find_current_recommendation_item_for_consultation(
            client=client,
            recommendation_id=recommendation_id,
            style_id=style_id,
            selected_image_url=selected_image_url,
        )
        item_origin = "current_recommendations_payload"
    if not legacy_item:
        logger.warning(
            "[legacy_direct_consultation_materialization_failed] client_id=%s legacy_client_id=%s source=%s reason=no_current_recommendation_item item_origin=%s",
            client.id,
            legacy_client_id,
            source,
            item_origin,
        )
        return None, None, None, None

    recommendation_id = legacy_item.get("recommendation_id")
    style_id = int(legacy_item.get("style_id") or 0) or None
    selected_result = None
    selected_detail = None
    selected_style_name = legacy_item.get("style_name")

    if recommendation_id is not None:
        try:
            selected_result, selected_detail = _legacy_result_and_detail_for_recommendation(
                client=client,
                recommendation_id=int(recommendation_id),
            )
        except (TypeError, ValueError):
            selected_result, selected_detail = None, None

    if selected_result is not None and selected_detail is not None:
        logger.info(
            "[legacy_direct_consultation_materialized] client_id=%s legacy_client_id=%s source=%s mode=linked_existing item_origin=%s recommendation_id=%s style_id=%s",
            client.id,
            legacy_client_id,
            source,
            item_origin,
            recommendation_id,
            style_id,
        )
        return selected_result, selected_detail, style_id, "linked_direct_consultation_recommendation"

    if style_id is None:
        logger.warning(
            "[legacy_direct_consultation_materialization_failed] client_id=%s legacy_client_id=%s source=%s reason=missing_style_id item_origin=%s recommendation_id=%s",
            client.id,
            legacy_client_id,
            source,
            item_origin,
            recommendation_id,
        )
        return None, None, None, None

    result_id = _next_legacy_pk(LegacyClientResult, "result_id")
    detail_id = _next_legacy_pk(LegacyClientResultDetail, "detail_id")
    style_name, style_description = _legacy_style_label(style_id)
    selected_style_name = selected_style_name or style_name
    persisted_simulation_image_reference = _resolve_persistable_display_image_reference(
        simulation_image_url=legacy_item.get("simulation_image_url") or legacy_item.get("synthetic_image_url"),
        sample_image_url=legacy_item.get("sample_image_url"),
    )
    selected_result = LegacyClientResult.objects.create(
        result_id=result_id,
        analysis_id=(getattr(get_latest_analysis(client), "id", None) or getattr(get_latest_analysis(client), "analysis_id", None) or 0),
        client_id=legacy_client_id,
        selected_hairstyle_id=None,
        selected_image_url=None,
        is_confirmed=False,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        backend_selection_id=None,
        backend_consultation_id=None,
        backend_client_ref_id=client.id,
        backend_admin_ref_id=(admin.id if admin else client.shop_id),
        backend_designer_ref_id=(designer.id if designer else client.designer_id),
        source=source,
        survey_snapshot=survey_snapshot,
        analysis_data_snapshot=analysis_snapshot,
        status="PENDING",
        is_active=True,
        is_read=False,
        closed_at=None,
        selected_recommendation_id=None,
    )
    selected_detail = LegacyClientResultDetail.objects.create(
        detail_id=detail_id,
        result_id=result_id,
        hairstyle_id=style_id,
        rank=int(legacy_item.get("rank") or 1),
        similarity_score=float(legacy_item.get("match_score") or 0.0),
        final_score=float(legacy_item.get("match_score") or 0.0),
        simulated_image_url=persisted_simulation_image_reference,
        recommendation_reason=(
            legacy_item.get("reasoning")
            or legacy_item.get("llm_explanation")
            or "current recommendation direct consultation materialized for handoff"
        ),
        backend_recommendation_id=recommendation_id,
        backend_client_ref_id=client.id,
        backend_capture_record_id=None,
        batch_id=_coerce_batch_uuid(legacy_item.get("batch_id")),
        source=source,
        style_name_snapshot=selected_style_name,
        style_description_snapshot=legacy_item.get("style_description") or style_description,
        keywords_json=list(legacy_item.get("keywords") or []),
        sample_image_url=legacy_item.get("sample_image_url"),
        regeneration_snapshot=legacy_item.get("regeneration_snapshot"),
        reasoning_snapshot={
            **dict(legacy_item.get("reasoning_snapshot") or {}),
            "summary": (
                (legacy_item.get("reasoning_snapshot") or {}).get("summary")
                or legacy_item.get("reasoning")
                or legacy_item.get("llm_explanation")
                or "current recommendation direct consultation materialized for handoff"
            ),
            "source": source,
            "materialized_for_direct_consultation": True,
        },
        is_chosen=False,
        chosen_at=None,
        is_sent_to_admin=True,
        sent_at=now,
        created_at_ts=now,
    )
    logger.info(
        "[legacy_direct_consultation_materialized] client_id=%s legacy_client_id=%s source=%s mode=created_new item_origin=%s recommendation_id=%s style_id=%s",
        client.id,
        legacy_client_id,
        source,
        item_origin,
        recommendation_id,
        style_id,
    )
    return selected_result, selected_detail, style_id, "materialized_direct_consultation"


def _bridge_recommendation_from_legacy_item(
    *,
    client: "Client",
    legacy_item: dict,
    latest_analysis: "FaceAnalysis | None" = None,
) -> "FormerRecommendation":
    style_id = int(legacy_item.get("style_id") or 0)
    style_reference = get_style_record(style_id=style_id)
    latest_capture = get_latest_capture(client)
    latest_survey = get_latest_survey(client) or build_default_survey_context(client.id)
    latest_analysis = latest_analysis or get_latest_analysis(client)
    regeneration_snapshot = build_recommendation_regeneration_snapshot(
        client=client,
        survey=latest_survey,
        analysis=latest_analysis,
        source=str(legacy_item.get("source") or "legacy_bridge"),
        capture_record=latest_capture,
        recommendation_stage="legacy",
    )
    reasoning_snapshot = {
        **dict(legacy_item.get("reasoning_snapshot") or {}),
        "summary": (
            (legacy_item.get("reasoning_snapshot") or {}).get("summary")
            or legacy_item.get("reasoning")
            or legacy_item.get("llm_explanation")
            or ""
        ),
        "legacy_bridge": True,
    }
    return SimpleNamespace(
        id=int(legacy_item.get("recommendation_id") or 0),
        client=client,
        client_id=client.id,
        capture_record=latest_capture,
        capture_record_id=getattr(latest_capture, "id", None),
        style=style_reference,
        batch_id=_coerce_batch_uuid(legacy_item.get("batch_id")),
        source=str(legacy_item.get("source") or "legacy_bridge")[:20],
        style_id_snapshot=style_id,
        style_name_snapshot=(
            legacy_item.get("style_name")
            or getattr(style_reference, "name", None)
            or getattr(style_reference, "style_name", None)
            or f"Style {style_id}"
        ),
        style_description_snapshot=(
            legacy_item.get("style_description")
            or getattr(style_reference, "description", None)
            or ""
        ),
        keywords=list(
            legacy_item.get("keywords")
            or ([getattr(style_reference, "vibe", None)] if getattr(style_reference, "vibe", None) else [])
        ),
        sample_image_url=legacy_item.get("sample_image_url") or getattr(style_reference, "image_url", None),
        simulation_image_url=legacy_item.get("simulation_image_url"),
        regeneration_snapshot=regeneration_snapshot,
        llm_explanation=legacy_item.get("llm_explanation") or legacy_item.get("reasoning") or "",
        reasoning_snapshot=reasoning_snapshot,
        match_score=legacy_item.get("match_score"),
        rank=int(legacy_item.get("rank") or 1),
        is_chosen=bool(legacy_item.get("is_chosen")),
        chosen_at=legacy_item.get("chosen_at"),
        is_sent_to_admin=bool(legacy_item.get("is_sent_to_admin")),
        sent_at=legacy_item.get("sent_at"),
        created_at=legacy_item.get("created_at"),
    )


def _mark_recommendation_batch_as_selected(*, selected_row: "FormerRecommendation") -> "FormerRecommendation":
    if has_legacy_result_source():
        result_row, detail_row = _legacy_result_and_detail_for_recommendation(
            client=selected_row.client,
            recommendation_id=int(selected_row.id),
        )
        now = timezone.now()
        if result_row is not None:
            LegacyClientResultDetail.objects.filter(result_id=result_row.result_id).update(
                is_chosen=False,
                chosen_at=None,
                is_sent_to_admin=False,
                sent_at=None,
            )
        if detail_row is not None:
            detail_row.is_chosen = True
            detail_row.chosen_at = now
            detail_row.is_sent_to_admin = True
            detail_row.sent_at = now
            detail_row.save(update_fields=["is_chosen", "chosen_at", "is_sent_to_admin", "sent_at"])
            selected_row.is_chosen = True
            selected_row.chosen_at = now
            selected_row.is_sent_to_admin = True
            selected_row.sent_at = now
    return selected_row


def _build_legacy_current_recommendations_payload(
    *,
    client: "Client",
    legacy_items: list[dict],
    has_active_consultation: bool,
    message: str,
) -> dict:
    retry_meta = _build_legacy_retry_recommendation_meta(
        items=legacy_items,
        has_active_consultation=has_active_consultation,
    )
    return {
        "status": "ready",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "source": "current_recommendations",
        "batch_id": legacy_items[0].get("batch_id"),
        "message": message,
        "items": legacy_items,
        "next_actions": (
            ["retry_recommendations", "consultation"]
            if retry_meta["can_retry_recommendations"]
            else ["consultation"]
        ),
        **retry_meta,
    }


def _build_processing_current_recommendations_payload(*, message: str, items: list[dict] | None = None) -> dict:
    items_list = list(items or [])
    retry_meta = _build_legacy_retry_recommendation_meta(items=items_list, has_active_consultation=False)
    recommendation_stage = _legacy_recommendation_stage(items_list)
    payload = {
        "status": "processing",
        "source": "current_recommendations",
        "message": message,
        "items": items_list,
        "recommendation_stage": recommendation_stage,
    }
    payload.update(retry_meta)
    payload["next_actions"] = (
        ["retry_recommendations", "consultation"]
        if retry_meta["can_retry_recommendations"]
        else ["consultation"]
    )
    return payload


def _has_failed_recommendation_inputs(*, latest_capture_attempt, latest_capture, latest_analysis) -> bool:
    statuses = {
        str(getattr(latest_capture_attempt, "status", "") or "").strip().upper(),
        str(getattr(latest_capture, "status", "") or "").strip().upper(),
        str(getattr(latest_analysis, "status", "") or "").strip().upper(),
    }
    return bool(statuses & FAILED_RECOMMENDATION_INPUT_STATUSES)


def _build_recommendation_diagnostic_snapshot(
    *,
    client: "Client",
    latest_capture_attempt,
    latest_survey,
    latest_capture,
    latest_analysis,
    legacy_items: list[dict],
) -> dict:
    capture_attempt_reason_code = None
    capture_attempt_snapshot = getattr(latest_capture_attempt, "privacy_snapshot", None)
    if latest_capture_attempt is not None:
        capture_attempt_reason_code = infer_capture_reason_code(
            error_note=getattr(latest_capture_attempt, "error_note", None),
            privacy_snapshot=(capture_attempt_snapshot if isinstance(capture_attempt_snapshot, dict) else None),
        )

    active_consultation = _has_active_consultation_state(client=client)
    local_mock_enabled = bool(settings.DEBUG and settings.MIRRAI_LOCAL_MOCK_RESULTS)
    runtime_state = build_runtime_state(
        latest_capture_attempt=latest_capture_attempt,
        latest_survey=latest_survey,
        latest_capture=latest_capture,
        latest_analysis=latest_analysis,
        legacy_items=legacy_items,
    )
    current_assets = _prepare_current_legacy_assets(
        latest_analysis=latest_analysis,
        legacy_items=legacy_items,
    )
    processing_pending = runtime_requires_wait_for_recommendations(runtime_state)
    failed_inputs = _has_failed_recommendation_inputs(
        latest_capture_attempt=latest_capture_attempt,
        latest_capture=latest_capture,
        latest_analysis=latest_analysis,
    )
    blockers: list[str] = []

    if latest_capture is None and latest_capture_attempt is not None and latest_capture_attempt.status in {"NEEDS_RETAKE", "FAILED"}:
        blockers.append("capture_failed")
        predicted_response = {
            "status": "needs_capture",
            "source": "current_recommendations",
            "decision": "capture_failed_retake",
            "message": (
                latest_capture_attempt.error_note
                or "Face detection did not succeed. Please retake a front-facing photo."
            ),
            "next_action": "capture",
            "next_actions": ["capture"],
            "blockers": blockers,
        }
    elif not latest_capture and not latest_survey:
        blockers.extend(["missing_survey", "missing_capture"])
        predicted_response = {
            "status": "needs_input",
            "source": "current_recommendations",
            "decision": "missing_survey_and_capture",
            "message": "No survey or capture data is available yet. Start with the survey or upload a capture.",
            "next_actions": ["survey", "capture"],
            "blockers": blockers,
        }
    elif failed_inputs:
        blockers.append("capture_failed")
        predicted_response = {
            "status": "needs_capture",
            "source": "current_recommendations",
            "decision": "failed_latest_capture_or_analysis",
            "message": "The latest capture did not finish successfully, so new recommendation images cannot be generated yet.",
            "next_action": "capture",
            "next_actions": ["capture"],
            "blockers": blockers,
        }
    elif processing_pending:
        blockers.append("processing_inputs")
        predicted_response = {
            "status": "processing",
            "source": "current_recommendations",
            "decision": "await_processing_inputs",
            "message": "Capture analysis is still running. Current recommendations are waiting for fresh input data.",
            "next_actions": [],
            "blockers": blockers,
        }
    elif not latest_capture or not latest_analysis:
        if not latest_capture:
            blockers.append("missing_capture")
        if not latest_analysis:
            blockers.append("missing_analysis")
        if local_mock_enabled and latest_capture:
            predicted_response = {
                "status": "ready",
                "source": "local_mock",
                "decision": "local_mock_fallback",
                "message": "Local mock recommendations can be shown because capture data exists but canonical analysis is missing.",
                "next_actions": ["consultation"],
                "blockers": blockers,
            }
        else:
            predicted_response = {
                "status": "needs_capture",
                "source": "current_recommendations",
                "decision": "missing_canonical_capture_or_analysis",
                "message": "A valid front-facing capture is required before we can generate the current Top-5 recommendations.",
                "next_action": "capture",
                "next_actions": ["capture"],
                "blockers": blockers,
            }
    elif current_assets.is_ready:
        predicted_response = {
            "status": "ready",
            "source": "current_recommendations",
            "decision": "reuse_ready_current_batch",
            "message": "Existing model-team recommendation data for the latest analysis is being reused.",
            "next_actions": ["retry_recommendations", "consultation"] if active_consultation else ["consultation"],
            "blockers": blockers,
        }
    elif current_assets.has_pending_assets:
        blockers.append("simulation_assets_pending")
        predicted_response = {
            "status": "processing",
            "source": "current_recommendations",
            "decision": "await_primary_simulations",
            "message": "The recommendation batch exists, but the primary simulation images are still being prepared.",
            "next_actions": [],
            "blockers": blockers,
        }
    else:
        predicted_response = {
            "status": "would_generate",
            "source": "current_recommendations",
            "decision": "generate_new_batch",
            "message": "Capture and analysis are ready. Requesting current recommendations would generate a new batch.",
            "next_actions": ["retry_recommendations", "consultation"],
            "blockers": blockers,
            "would_persist_batch": True,
        }

    return {
        "client": {
            "client_id": client.id,
            "legacy_client_id": get_legacy_client_id(client=client),
            "name": getattr(client, "name", None),
            "phone": getattr(client, "phone", None),
        },
        "ai_runtime": get_ai_runtime_config_snapshot(),
        "survey": {
            "present": bool(latest_survey),
            "created_at": _coerce_iso_datetime(getattr(latest_survey, "created_at", None)),
            "target_length": getattr(latest_survey, "target_length", None),
            "target_vibe": getattr(latest_survey, "target_vibe", None),
            "scalp_type": getattr(latest_survey, "scalp_type", None),
            "hair_colour": getattr(latest_survey, "hair_colour", None),
            "budget_range": getattr(latest_survey, "budget_range", None),
        },
        "capture_attempt": {
            "present": bool(latest_capture_attempt),
            "record_id": getattr(latest_capture_attempt, "id", None) or getattr(latest_capture_attempt, "analysis_id", None),
            "status": getattr(latest_capture_attempt, "status", None),
            "face_count": getattr(latest_capture_attempt, "face_count", None),
            "reason_code": capture_attempt_reason_code,
            "error_note": getattr(latest_capture_attempt, "error_note", None),
            "created_at": _coerce_iso_datetime(getattr(latest_capture_attempt, "created_at", None)),
            "updated_at": _coerce_iso_datetime(getattr(latest_capture_attempt, "updated_at", None)),
        },
        "capture": {
            "present": bool(latest_capture),
            "record_id": getattr(latest_capture, "id", None) or getattr(latest_capture, "analysis_id", None),
            "status": getattr(latest_capture, "status", None),
            "face_count": getattr(latest_capture, "face_count", None),
            "created_at": _coerce_iso_datetime(getattr(latest_capture, "created_at", None)),
        },
        "analysis": {
            "present": bool(latest_analysis),
            "analysis_id": getattr(latest_analysis, "id", None) or getattr(latest_analysis, "analysis_id", None),
            "face_shape": getattr(latest_analysis, "face_shape", None),
            "golden_ratio_score": getattr(latest_analysis, "golden_ratio_score", None),
            "created_at": _coerce_iso_datetime(getattr(latest_analysis, "created_at", None)),
        },
        "legacy_recommendations": {
            "count": len(legacy_items),
            "latest_batch_id": (legacy_items[0].get("batch_id") if legacy_items else None),
            "sources": sorted({str(item.get("source") or "") for item in legacy_items if str(item.get("source") or "").strip()}),
            "chosen_count": sum(1 for item in legacy_items if item.get("is_chosen")),
        },
        "active_consultation": active_consultation,
        "local_mock_enabled": local_mock_enabled,
        "predicted_response": predicted_response,
    }


def build_recommendation_diagnostic_snapshot(client: "Client") -> dict:
    runtime_state = _load_current_recommendation_runtime(client)
    return _build_recommendation_diagnostic_snapshot(
        client=client,
        latest_capture_attempt=runtime_state.latest_capture_attempt,
        latest_survey=runtime_state.latest_survey,
        latest_capture=runtime_state.latest_capture,
        latest_analysis=runtime_state.latest_analysis,
        legacy_items=runtime_state.legacy_items,
    )


def _finalize_recommendation_payload(*, client: "Client", payload: dict, snapshot: dict) -> dict:
    items = payload.get("items") or []
    normalized_items = [
        _normalize_recommendation_item_contract(item)
        for item in items
        if isinstance(item, dict)
    ]
    if normalized_items or isinstance(items, list):
        payload["items"] = normalized_items

    capture_snapshot = snapshot.get("capture") or {}
    analysis_snapshot = snapshot.get("analysis") or {}
    default_reason = None
    if payload.get("status") == "needs_input":
        default_reason = "survey_or_capture_required"
    elif payload.get("status") == "needs_capture":
        default_reason = (
            "capture_retry_required"
            if "retake" in str(payload.get("message") or "").lower()
            else "capture_data_not_ready"
        )
    elif payload.get("status") == "processing" and not normalized_items:
        default_reason = "recommendations_processing"
    elif payload.get("status") == "empty":
        default_reason = "recommendations_not_ready"

    payload.update(
        _build_simulation_contract_meta(
            items=normalized_items,
            client=client,
            latest_capture=SimpleNamespace(
                id=capture_snapshot.get("record_id"),
                analysis_id=capture_snapshot.get("record_id"),
            ) if capture_snapshot.get("record_id") else None,
            latest_analysis=SimpleNamespace(
                id=analysis_snapshot.get("analysis_id"),
                analysis_id=analysis_snapshot.get("analysis_id"),
            ) if analysis_snapshot.get("analysis_id") else None,
            default_reason=default_reason,
        )
    )
    predicted = snapshot.get("predicted_response") or {}
    next_actions = payload.get("next_actions")
    if not next_actions and payload.get("next_action"):
        next_actions = [payload.get("next_action")]
    logger.info(
        "[recommendation_state] client_id=%s status=%s source=%s decision=%s items=%s capture_attempt=%s capture_ready=%s analysis_ready=%s legacy_items=%s active_consultation=%s local_mock=%s next_actions=%s",
        client.id,
        payload.get("status"),
        payload.get("source"),
        predicted.get("decision"),
        len(payload.get("items") or []),
        (snapshot.get("capture_attempt") or {}).get("status"),
        (snapshot.get("capture") or {}).get("present"),
        (snapshot.get("analysis") or {}).get("present"),
        (snapshot.get("legacy_recommendations") or {}).get("count"),
        snapshot.get("active_consultation"),
        snapshot.get("local_mock_enabled"),
        next_actions or [],
    )
    return payload


def get_current_recommendations(client: "Client") -> dict:
    def load_runtime_state():
        return _load_current_recommendation_runtime(client)

    runtime_state, wait_timed_out = wait_for_runtime_state(
        load_state=load_runtime_state,
        should_wait=runtime_requires_wait_for_recommendations,
        clock=time,
        wait_policy=CURRENT_RECOMMENDATION_WAIT_POLICY,
    )
    latest_capture_attempt = runtime_state.latest_capture_attempt
    latest_survey = runtime_state.latest_survey
    latest_capture = runtime_state.latest_capture
    latest_analysis = runtime_state.latest_analysis
    legacy_items = runtime_state.legacy_items
    snapshot = _build_recommendation_diagnostic_snapshot(
        client=client,
        latest_capture_attempt=latest_capture_attempt,
        latest_survey=latest_survey,
        latest_capture=latest_capture,
        latest_analysis=latest_analysis,
        legacy_items=legacy_items,
    )

    if (
        latest_capture is None
        and latest_capture_attempt is not None
        and latest_capture_attempt.status in {"NEEDS_RETAKE", "FAILED"}
    ):
        return _finalize_recommendation_payload(
            client=client,
            payload={
                "status": "needs_capture",
                "source": "current_recommendations",
                "message": latest_capture_attempt.error_note or "Face detection did not succeed. Please retake a front-facing photo.",
                "next_action": "capture",
                "items": [],
            },
            snapshot=snapshot,
        )

    if not latest_capture and not latest_survey:
        return _finalize_recommendation_payload(
            client=client,
            payload={
                "status": "needs_input",
                "source": "current_recommendations",
                "message": "No survey or capture data is available yet. Start with the survey or upload a capture.",
                "next_actions": ["survey", "capture"],
                "items": [],
            },
            snapshot=snapshot,
        )

    if _has_failed_recommendation_inputs(
        latest_capture_attempt=latest_capture_attempt,
        latest_capture=latest_capture,
        latest_analysis=latest_analysis,
    ):
        return _finalize_recommendation_payload(
            client=client,
            payload={
                "status": "needs_capture",
                "source": "current_recommendations",
                "message": "The latest capture failed, so recommendation image generation could not start. Please retake the photo.",
                "next_action": "capture",
                "items": [],
            },
            snapshot=snapshot,
        )

    if runtime_requires_wait_for_recommendations(runtime_state):
        message = "Capture analysis is still running. Recommendation images will appear automatically when the latest processing finishes."
        if wait_timed_out:
            message = "Capture analysis is taking longer than usual. Recommendation images are still processing."
        return _finalize_recommendation_payload(
            client=client,
            payload=_build_processing_current_recommendations_payload(
                message=message,
            ),
            snapshot=snapshot,
        )

    if not latest_capture or not latest_analysis:
        if settings.DEBUG and settings.MIRRAI_LOCAL_MOCK_RESULTS and latest_capture:
            return _finalize_recommendation_payload(
                client=client,
                payload=_build_local_mock_recommendations(
                    client=client,
                    latest_survey=latest_survey,
                    latest_analysis=latest_analysis,
                ),
                snapshot=snapshot,
            )
        return _finalize_recommendation_payload(
            client=client,
            payload={
                "status": "needs_capture",
                "source": "current_recommendations",
                "message": "A valid front-facing capture is required before we can generate the current Top-5 recommendations.",
                "next_action": "capture",
                "items": [],
            },
            snapshot=snapshot,
        )

    current_assets = _prepare_current_legacy_assets(
        latest_analysis=latest_analysis,
        legacy_items=legacy_items,
    )
    if current_assets.has_pending_assets:
        refreshed_runtime_state = load_runtime_state()
        refreshed_assets = _prepare_current_legacy_assets(
            latest_analysis=refreshed_runtime_state.latest_analysis,
            legacy_items=refreshed_runtime_state.legacy_items,
        )
        if refreshed_assets.item_count:
            runtime_state = refreshed_runtime_state
            latest_capture_attempt = runtime_state.latest_capture_attempt
            latest_survey = runtime_state.latest_survey
            latest_capture = runtime_state.latest_capture
            latest_analysis = runtime_state.latest_analysis
            legacy_items = runtime_state.legacy_items
            current_assets = refreshed_assets
            snapshot = _build_recommendation_diagnostic_snapshot(
                client=client,
                latest_capture_attempt=latest_capture_attempt,
                latest_survey=latest_survey,
                latest_capture=latest_capture,
                latest_analysis=latest_analysis,
                legacy_items=legacy_items,
            )

    if current_assets.is_ready:
        has_active_consultation = snapshot["active_consultation"]
        return _finalize_recommendation_payload(
            client=client,
            payload=_build_legacy_current_recommendations_payload(
                client=client,
                legacy_items=current_assets.items,
                has_active_consultation=has_active_consultation,
                message="Existing model-team recommendation data from the latest capture is being reused.",
            ),
            snapshot=snapshot,
        )

    batch_id, rows, status_code = _ensure_current_batch(
        client,
        latest_capture=latest_capture,
        latest_survey=latest_survey,
        latest_analysis=latest_analysis,
        legacy_items=legacy_items,
    )
    if status_code == "needs_capture":
        return _finalize_recommendation_payload(
            client=client,
            payload={
                "status": "needs_capture",
                "source": "current_recommendations",
                "message": "Capture data is not ready yet. Please complete capture before requesting current recommendations.",
                "next_action": "capture",
                "items": [],
            },
            snapshot=snapshot,
        )
    if status_code == "processing":
        return _finalize_recommendation_payload(
            client=client,
            payload=_build_processing_current_recommendations_payload(
                message="Recommendation images are still being generated. This usually takes about 1 to 2 minutes.",
                items=rows,
            ),
            snapshot=snapshot,
        )

    if not rows:
        if settings.DEBUG and settings.MIRRAI_LOCAL_MOCK_RESULTS:
            return _finalize_recommendation_payload(
                client=client,
                payload=_build_local_mock_recommendations(
                    client=client,
                    latest_survey=latest_survey,
                    latest_analysis=latest_analysis,
                ),
                snapshot=snapshot,
            )
        return _finalize_recommendation_payload(
            client=client,
            payload=_build_empty_response(
                source="current_recommendations",
                message="No recommendation batch is available yet. Please retake the capture and try again.",
                next_action="capture",
            ),
            snapshot=snapshot,
        )

    snapshot["legacy_recommendations"]["count"] = len(rows)
    snapshot["legacy_recommendations"]["latest_batch_id"] = batch_id
    snapshot["predicted_response"]["decision"] = "generated_current_batch"
    has_active_consultation = snapshot["active_consultation"]
    retry_meta = _build_legacy_retry_recommendation_meta(
        items=rows,
        has_active_consultation=has_active_consultation,
    )
    message = "The latest Top-5 recommendations were generated from the most recent capture and analysis."
    if latest_survey is None:
        message = "The latest Top-5 recommendations were generated from face analysis only because survey data is not available."
    elif retry_meta["recommendation_stage"] == "retry":
        message = "The recommendations were regenerated once with preference-first scoring and no trend influence."

    payload = {
        "status": "ready",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "source": "current_recommendations",
        "batch_id": batch_id,
        "message": message,
        "items": rows,
    }
    payload.update(retry_meta)
    payload["next_actions"] = (
        ["retry_recommendations", "consultation"]
        if retry_meta["can_retry_recommendations"]
        else ["consultation"]
    )
    return _finalize_recommendation_payload(client=client, payload=payload, snapshot=snapshot)


def retry_current_recommendations(client: "Client") -> dict:
    latest_capture = get_latest_capture(client)
    latest_analysis = get_latest_analysis(client)
    latest_survey = get_latest_survey(client)
    if not latest_capture or not latest_analysis:
        raise ValueError("A completed capture and face analysis are required before retrying recommendations.")

    legacy_items = get_legacy_former_recommendation_items(client=client) or []
    if not legacy_items:
        raise ValueError("Retry is available only after the initial recommendation batch has been generated.")
    retry_meta = _build_legacy_retry_recommendation_meta(
        items=legacy_items,
        has_active_consultation=_has_active_consultation_state(client=client),
    )
    if not retry_meta["can_retry_recommendations"]:
        if retry_meta["retry_block_reason"] == "consultation_started":
            raise ValueError("Retry is not available after the consultation flow has started.")
        if retry_meta["retry_block_reason"] == "recommendation_already_selected":
            raise ValueError("Retry is not available after a recommendation has already been selected.")
        if retry_meta["retry_block_reason"] == "retry_already_used":
            raise ValueError("Retry recommendations are only available once after the initial recommendation batch.")
        raise ValueError("Retry is not available while only legacy recommendation data is available.")

    survey_context = latest_survey or build_default_survey_context(client.id)
    new_batch_id, _ = persist_generated_batch(
        client=client,
        capture_record=latest_capture,
        survey=survey_context,
        analysis=latest_analysis,
        recommendation_stage="retry",
    )
    new_items = get_legacy_former_recommendation_items(client=client) or []
    return {
        "status": "ready",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "source": "current_recommendations",
        "batch_id": new_batch_id,
        "message": "A one-time retry recommendation batch has been generated with preference-first scoring.",
        "items": new_items,
        "next_actions": ["consultation"],
        **_build_legacy_retry_recommendation_meta(
            items=new_items,
            has_active_consultation=False,
        ),
    }


def get_trend_recommendations(*, days: int = 30, client: "Client | None" = None) -> dict:
    cutoff = timezone.now() - timezone.timedelta(days=days)
    target_age_profile = build_client_age_profile(client) if client else None
    legacy_items = get_legacy_confirmed_selection_items(since=cutoff) or []
    selections = legacy_items

    scoped_selections = selections
    trend_scope = "global"
    if target_age_profile:
        exact_group_matches = [
            row
            for row in selections
            if (row.get("age_profile") or {}).get("age_group") == target_age_profile["age_group"]
        ]
        decade_matches = [
            row
            for row in selections
            if (row.get("age_profile") or {}).get("age_decade") == target_age_profile["age_decade"]
        ]
        if exact_group_matches:
            scoped_selections = exact_group_matches
            trend_scope = "age_group"
        elif decade_matches:
            scoped_selections = decade_matches
            trend_scope = "age_decade"

    popular_style_ids = []
    if scoped_selections:
        counts = Counter(row["style_id"] for row in scoped_selections)
        popular_style_ids = [
            {"style_id": style_id, "selection_count": count}
            for style_id, count in counts.most_common(5)
        ]

    items: list[dict] = []
    legacy_representative = {}
    for row in scoped_selections:
        legacy_representative.setdefault(row["style_id"], row)
    for rank, item in enumerate(popular_style_ids, start=1):
        style = get_style_record(style_id=item["style_id"])
        legacy_row = legacy_representative.get(item["style_id"], {})
        if not style and not legacy_row:
            continue
        trend_summary = f"recent confirmed selections in the last {days} days"
        if trend_scope == "age_group" and target_age_profile:
            trend_summary = f"{target_age_profile['age_group']} selections in the last {days} days"
        elif trend_scope == "age_decade" and target_age_profile:
            trend_summary = f"{target_age_profile['age_decade']} selections in the last {days} days"
        items.append(
            {
                "source": "trend",
                "style_id": item["style_id"],
                "style_name": legacy_row.get("style_name") or (style.name if style else f"Style {item['style_id']}"),
                "style_description": legacy_row.get("style_description") or (style.description if style else "") or f"This style has been selected frequently in the last {days} days.",
                "keywords": legacy_row.get("keywords") or ([style.vibe] if style and style.vibe else []),
                "sample_image_url": resolve_storage_reference(legacy_row.get("image_url") or (style.image_url if style else None)),
                "simulation_image_url": resolve_storage_reference(legacy_row.get("image_url") or (style.image_url if style else None)),
                "synthetic_image_url": resolve_storage_reference(legacy_row.get("image_url") or (style.image_url if style else None)),
                "llm_explanation": legacy_row.get("style_description") or (style.description if style else "") or f"This style has been selected frequently in the last {days} days.",
                "reasoning": f"Sorted by confirmed selection count over the last {days} days.",
                "reasoning_snapshot": {
                    "summary": trend_summary,
                    "selection_count": int(item["selection_count"]),
                    "days": days,
                    "source": "trend",
                    "trend_scope": trend_scope,
                    "age_profile": target_age_profile,
                },
                "match_score": float(item["selection_count"]),
                "rank": rank,
                "is_chosen": False,
            }
        )

    if not items:
        seed_styles = _seed_trend_styles(limit=5)
        seeded_names = [str(item.get("style_name") or "").strip() for item in seed_styles if str(item.get("style_name") or "").strip()]
        db_seeded = {}
        for style_name in seeded_names:
            style = get_style_record_by_name(style_name=style_name)
            if style is None:
                continue
            normalized_name = str(
                getattr(style, "name", None)
                or getattr(style, "style_name", None)
                or style_name
            ).strip()
            db_seeded[normalized_name] = style

        for rank, seed in enumerate(seed_styles, start=1):
            style = db_seeded.get(str(seed.get("style_name") or "").strip())
            if not style:
                continue
            style_id = int(
                getattr(style, "backend_style_id", None)
                or getattr(style, "hairstyle_id", None)
                or getattr(style, "id", 0)
            )
            style_name = getattr(style, "name", None) or getattr(style, "style_name", None) or ""
            style_description = getattr(style, "description", None) or str(seed.get("description") or "")
            style_image_url = getattr(style, "image_url", None)
            style_vibe = getattr(style, "vibe", None)
            items.append(
                {
                    "source": "trend",
                    "style_id": style_id,
                    "style_name": style_name,
                    "style_description": style_description,
                    "keywords": list(seed.get("keywords") or ([style_vibe] if style_vibe else [])),
                    "sample_image_url": resolve_storage_reference(style_image_url),
                    "simulation_image_url": resolve_storage_reference(style_image_url),
                    "synthetic_image_url": resolve_storage_reference(style_image_url),
                    "llm_explanation": style_description,
                    "reasoning": "fallback trend catalog synced from refreshed seed data",
                    "reasoning_snapshot": {
                        "summary": "fallback trend catalog synced from refreshed seed data",
                        "selection_count": 0,
                        "days": days,
                        "source": "trend",
                        "trend_scope": trend_scope,
                        "age_profile": target_age_profile,
                        "seed_source": str(seed.get("source") or ""),
                        "seed_last_updated": str(seed.get("last_updated") or ""),
                    },
                    "match_score": float(seed.get("freshness_score") or 0.0),
                    "rank": rank,
                    "is_chosen": False,
                }
            )

    if not items:
        styles_by_id = ensure_catalog_styles()
        fallback_ids = [201, 203, 205, 204, 207]
        for rank, style_id in enumerate(fallback_ids, start=1):
            style = styles_by_id[style_id]
            normalized_style_id = (
                getattr(style, "backend_style_id", None)
                or getattr(style, "hairstyle_id", None)
                or getattr(style, "id", None)
                or style_id
            )
            style_name = getattr(style, "name", None) or getattr(style, "style_name", None) or f"Style {style_id}"
            style_description = getattr(style, "description", None) or ""
            style_image_url = getattr(style, "image_url", None)
            style_vibe = getattr(style, "vibe", None)
            items.append(
                {
                    "source": "trend",
                    "style_id": normalized_style_id,
                    "style_name": style_name,
                    "style_description": style_description,
                    "keywords": [style_vibe] if style_vibe else [],
                    "sample_image_url": resolve_storage_reference(style_image_url),
                    "simulation_image_url": resolve_storage_reference(style_image_url),
                    "synthetic_image_url": resolve_storage_reference(style_image_url),
                    "llm_explanation": "Recent confirmed-selection data is limited, so the default trend catalog is shown.",
                    "reasoning": "fallback trend catalog",
                    "reasoning_snapshot": {
                        "summary": "fallback trend catalog",
                        "selection_count": 0,
                        "days": days,
                        "source": "trend",
                        "trend_scope": trend_scope,
                        "age_profile": target_age_profile,
                    },
                    "match_score": 0.0,
                    "rank": rank,
                    "is_chosen": False,
                }
            )

    payload = {
        "status": "ready",
        "source": "trend",
        "days": days,
        "trend_scope": trend_scope,
        "age_profile": target_age_profile,
        "items": items,
    }
    if client is not None:
        payload["client_id"] = client.id
        payload["legacy_client_id"] = get_legacy_client_id(client=client)
    return payload


def _legacy_result_direct_write(
    *,
    client: "Client",
    selected_style_id: int | None,
    recommendation_id: int | None,
    selected_image_url: str | None,
    source: str,
    survey_snapshot: dict | None,
    analysis_snapshot: dict | None,
    admin: "AdminAccount | None",
    designer,
    direct_consultation: bool,
) -> dict | None:
    if not _legacy_result_writable():
        logger.warning(
            "[legacy_selection_materialization_failed] client_id=%s source=%s direct_consultation=%s reason=legacy_tables_not_writable selected_style_id=%s recommendation_id=%s",
            client.id,
            source,
            direct_consultation,
            selected_style_id,
            recommendation_id,
        )
        return None

    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        logger.warning(
            "[legacy_selection_materialization_failed] client_id=%s source=%s direct_consultation=%s reason=missing_legacy_client_id selected_style_id=%s recommendation_id=%s",
            client.id,
            source,
            direct_consultation,
            selected_style_id,
            recommendation_id,
        )
        return None

    now = timezone.now()
    selected_result = None
    selected_detail = None
    selection_record_status = (
        "direct_consultation_without_selection"
        if direct_consultation
        else "style_only_selection"
    )

    if source == "current_recommendations" and selected_image_url and (
        recommendation_id is None or selected_style_id is None
    ):
        payload_item = _find_legacy_recommendation_item(
            client=client,
            recommendation_id=recommendation_id,
            style_id=selected_style_id,
            selected_image_url=selected_image_url,
        )
        if payload_item is None:
            payload_item = _find_current_recommendation_item_for_consultation(
                client=client,
                recommendation_id=recommendation_id,
                style_id=selected_style_id,
                selected_image_url=selected_image_url,
            )
        if payload_item is not None:
            if recommendation_id is None:
                try:
                    recommendation_id = int(payload_item.get("recommendation_id") or 0) or None
                except (TypeError, ValueError):
                    recommendation_id = None
            if selected_style_id is None:
                try:
                    selected_style_id = int(payload_item.get("style_id") or 0) or None
                except (TypeError, ValueError):
                    selected_style_id = None

    if recommendation_id is not None:
        selected_result, selected_detail = _legacy_result_and_detail_for_recommendation(
            client=client,
            recommendation_id=int(recommendation_id),
        )
        if selected_detail is not None:
            selected_style_id = int(selected_detail.hairstyle_id)
            selection_record_status = "linked_existing_recommendation"
    if (
        selected_result is None
        and source == "current_recommendations"
        and selected_style_id is not None
    ):
        selected_result, selected_detail = _legacy_result_and_detail_for_style(
            client=client,
            style_id=int(selected_style_id),
        )
        if selected_detail is not None:
            selection_record_status = "linked_generated_style_row"

    if selected_result is None and source == "current_recommendations" and direct_consultation and selected_style_id is None:
        (
            selected_result,
            selected_detail,
            selected_style_id,
            direct_consultation_status,
        ) = _materialize_direct_consultation_current_recommendation(
            client=client,
            legacy_client_id=legacy_client_id,
            source=source,
            recommendation_id=recommendation_id,
            style_id=selected_style_id,
            selected_image_url=selected_image_url,
            survey_snapshot=survey_snapshot,
            analysis_snapshot=analysis_snapshot,
            admin=admin,
            designer=designer,
            now=now,
        )
        if direct_consultation_status:
            selection_record_status = direct_consultation_status

    if selected_result is None and source == "current_recommendations" and selected_style_id is not None:
        result_id = _next_legacy_pk(LegacyClientResult, "result_id")
        detail_id = _next_legacy_pk(LegacyClientResultDetail, "detail_id")
        style_name, style_description = _legacy_style_label(int(selected_style_id or 0))
        selected_result = LegacyClientResult.objects.create(
            result_id=result_id,
            analysis_id=(getattr(get_latest_analysis(client), "id", None) or getattr(get_latest_analysis(client), "analysis_id", None) or 0),
            client_id=legacy_client_id,
            selected_hairstyle_id=(None if direct_consultation else selected_style_id),
            selected_image_url=None,
            is_confirmed=not direct_consultation,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            backend_selection_id=None,
            backend_consultation_id=None,
            backend_client_ref_id=client.id,
            backend_admin_ref_id=(admin.id if admin else client.shop_id),
            backend_designer_ref_id=(designer.id if designer else client.designer_id),
            source=source,
            survey_snapshot=survey_snapshot,
            analysis_data_snapshot=analysis_snapshot,
            status="PENDING",
            is_active=True,
            is_read=False,
            closed_at=None,
            selected_recommendation_id=(None if direct_consultation else detail_id),
        )
        selected_detail = LegacyClientResultDetail.objects.create(
            detail_id=detail_id,
            result_id=result_id,
            hairstyle_id=int(selected_style_id or 0),
            rank=1,
            similarity_score=0.0,
            final_score=0.0,
            simulated_image_url=None,
            recommendation_reason="current recommendation selection materialized for consultation handoff",
            backend_recommendation_id=(
                int(recommendation_id)
                if str(recommendation_id or "").isdigit()
                else None
            ),
            backend_client_ref_id=client.id,
            backend_capture_record_id=None,
            batch_id=uuid.uuid4(),
            source=source,
            style_name_snapshot=style_name,
            style_description_snapshot=style_description,
            keywords_json=[],
            sample_image_url=None,
            regeneration_snapshot=None,
            reasoning_snapshot={
                "summary": "current recommendation selection materialized for consultation handoff",
                "source": "current_recommendations",
                "materialized_for_selection": True,
            },
            is_chosen=not direct_consultation,
            chosen_at=(now if not direct_consultation else None),
            is_sent_to_admin=True,
            sent_at=now,
            created_at_ts=now,
        )
        selection_record_status = "materialized_current_recommendation"

    if selected_result is None and source == "trend":
        result_id = _next_legacy_pk(LegacyClientResult, "result_id")
        detail_id = _next_legacy_pk(LegacyClientResultDetail, "detail_id")
        style_name, style_description = _legacy_style_label(int(selected_style_id or 0))
        selected_result = LegacyClientResult.objects.create(
            result_id=result_id,
            analysis_id=(getattr(get_latest_analysis(client), "id", None) or getattr(get_latest_analysis(client), "analysis_id", None) or 0),
            client_id=legacy_client_id,
            selected_hairstyle_id=(None if direct_consultation else selected_style_id),
            selected_image_url=None,
            is_confirmed=not direct_consultation,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            backend_selection_id=None,
            backend_consultation_id=None,
            backend_client_ref_id=client.id,
            backend_admin_ref_id=(admin.id if admin else client.shop_id),
            backend_designer_ref_id=(designer.id if designer else client.designer_id),
            source=source,
            survey_snapshot=survey_snapshot,
            analysis_data_snapshot=analysis_snapshot,
            status="PENDING",
            is_active=True,
            is_read=False,
            closed_at=None,
            selected_recommendation_id=(None if direct_consultation else detail_id),
        )
        selected_detail = LegacyClientResultDetail.objects.create(
            detail_id=detail_id,
            result_id=result_id,
            hairstyle_id=int(selected_style_id or 0),
            rank=1,
            similarity_score=0.0,
            final_score=0.0,
            simulated_image_url=None,
            recommendation_reason="trend selection promoted to consultation",
            backend_recommendation_id=None,
            backend_client_ref_id=client.id,
            backend_capture_record_id=None,
            batch_id=uuid.uuid4(),
            source=source,
            style_name_snapshot=style_name,
            style_description_snapshot=style_description,
            keywords_json=[],
            sample_image_url=None,
            regeneration_snapshot=None,
            reasoning_snapshot={"summary": "trend selection promoted to consultation", "source": "trend"},
            is_chosen=not direct_consultation,
            chosen_at=(now if not direct_consultation else None),
            is_sent_to_admin=True,
            sent_at=now,
            created_at_ts=now,
        )

    if selected_result is None:
        logger.warning(
            "[legacy_selection_materialization_failed] client_id=%s legacy_client_id=%s source=%s direct_consultation=%s reason=no_result_row selected_style_id=%s recommendation_id=%s",
            client.id,
            legacy_client_id,
            source,
            direct_consultation,
            selected_style_id,
            recommendation_id,
        )
        return None

    closed_consultation_count = LegacyClientResult.objects.filter(client_id=legacy_client_id, is_active=True).exclude(
        result_id=selected_result.result_id
    ).update(
        is_active=False,
        status="CLOSED",
        closed_at=now,
        is_read=True,
        is_confirmed=False,
        selected_hairstyle_id=None,
        selected_image_url=None,
        selected_recommendation_id=None,
    )

    selected_result.selected_hairstyle_id = (None if direct_consultation else selected_style_id)
    selected_result.selected_image_url = _selected_image_url_for_result(
        selected_detail=selected_detail,
        direct_consultation=direct_consultation,
    )
    selected_result.is_confirmed = not direct_consultation and selected_style_id is not None
    selected_result.updated_at = now.isoformat()
    selected_result.backend_admin_ref_id = admin.id if admin else client.shop_id
    selected_result.backend_designer_ref_id = designer.id if designer else client.designer_id
    selected_result.source = source
    selected_result.survey_snapshot = survey_snapshot
    selected_result.analysis_data_snapshot = analysis_snapshot
    selected_result.status = "PENDING"
    selected_result.is_active = True
    selected_result.is_read = False
    selected_result.closed_at = None
    selected_result.selected_recommendation_id = _canonical_selected_recommendation_id(
        selected_detail=selected_detail,
        direct_consultation=direct_consultation,
    )
    selected_result.save()

    LegacyClientResultDetail.objects.filter(result_id=selected_result.result_id).update(
        is_chosen=False,
        chosen_at=None,
        is_sent_to_admin=False,
        sent_at=None,
    )
    if selected_detail is not None:
        selected_detail.backend_client_ref_id = client.id
        selected_detail.is_chosen = not direct_consultation
        selected_detail.chosen_at = (now if not direct_consultation else None)
        selected_detail.is_sent_to_admin = True
        selected_detail.sent_at = now
        selected_detail.save()

    logger.info(
        "[legacy_selection_materialized] client_id=%s legacy_client_id=%s source=%s direct_consultation=%s selection_record_status=%s recommendation_id=%s selected_style_id=%s consultation_replaced_previous=%s",
        client.id,
        legacy_client_id,
        source,
        direct_consultation,
        selection_record_status,
        recommendation_id,
        selected_style_id,
        bool(closed_consultation_count),
    )

    return {
        "consultation_id": selected_result.backend_consultation_id or selected_result.result_id,
        "recommendation_id": (
            _canonical_selected_recommendation_id(
                selected_detail=selected_detail,
                direct_consultation=direct_consultation,
            )
        ),
        "selected_style_id": selected_style_id,
        "selected_style_name": (
            selected_detail.style_name_snapshot
            if selected_detail is not None
            else None
        ),
        "selection_record_status": selection_record_status,
        "consultation_record_status": "created",
        "consultation_replaced_previous": bool(closed_consultation_count),
        "closed_consultation_count": int(closed_consultation_count),
    }


def _cancel_legacy_result_directly(
    *,
    client: "Client",
    recommendation_id: int | None = None,
    selected_image_url: str | None = None,
) -> bool:
    if not _legacy_result_writable():
        return False

    legacy_client_id = get_legacy_client_id(client=client)
    if not legacy_client_id:
        return False

    target_result = None
    if recommendation_id is not None:
        target_result, _ = _legacy_result_and_detail_for_recommendation(
            client=client,
            recommendation_id=int(recommendation_id),
        )

    if target_result is None and selected_image_url:
        payload_item = _find_legacy_recommendation_item(client=client, selected_image_url=selected_image_url)
        if payload_item is None:
            payload_item = _find_current_recommendation_item_for_consultation(
                client=client,
                selected_image_url=selected_image_url,
            )
        if payload_item is not None:
            payload_recommendation_id = payload_item.get("recommendation_id")
            payload_style_id = payload_item.get("style_id")
            try:
                if payload_recommendation_id not in (None, ""):
                    target_result, _ = _legacy_result_and_detail_for_recommendation(
                        client=client,
                        recommendation_id=int(payload_recommendation_id),
                    )
            except (TypeError, ValueError):
                target_result = None
            if target_result is None:
                try:
                    if payload_style_id not in (None, ""):
                        target_result, _ = _legacy_result_and_detail_for_style(
                            client=client,
                            style_id=int(payload_style_id),
                        )
                except (TypeError, ValueError):
                    target_result = None

    if target_result is None:
        target_result = (
            LegacyClientResult.objects.filter(client_id=legacy_client_id, is_active=True)
            .order_by("-updated_at", "-result_id")
            .first()
        )
    if target_result is None:
        return False

    now = timezone.now()
    LegacyClientResult.objects.filter(result_id=target_result.result_id).update(
        is_active=False,
        status="CANCELLED",
        closed_at=now,
        is_read=True,
        is_confirmed=False,
        selected_hairstyle_id=None,
        selected_image_url=None,
        selected_recommendation_id=None,
    )
    LegacyClientResultDetail.objects.filter(result_id=target_result.result_id).update(
        is_chosen=False,
        chosen_at=None,
        is_sent_to_admin=False,
        sent_at=None,
    )
    return True


def confirm_style_selection(
    *,
    client: "Client",
    recommendation_id: int | None = None,
    style_id: int | None = None,
    selected_image_url: str | None = None,
    admin_id: int | str | None = None,
    source: str = "current_recommendations",
    direct_consultation: bool = False,
) -> dict:
    latest_analysis = get_latest_analysis(client)
    survey_snapshot = build_survey_snapshot(client)
    analysis_snapshot = {}
    if latest_analysis:
        analysis_snapshot = {
            "face_shape": latest_analysis.face_shape,
            "golden_ratio": latest_analysis.golden_ratio_score,
            "image_url": resolve_storage_reference(latest_analysis.image_url),
            "landmark_snapshot": latest_analysis.landmark_snapshot,
        }

    admin = get_admin_by_identifier(identifier=admin_id) if admin_id else None
    if admin is None:
        admin = client.shop
    designer = client.designer
    legacy_direct_result = _legacy_result_direct_write(
        client=client,
        selected_style_id=style_id,
        recommendation_id=recommendation_id,
        selected_image_url=selected_image_url,
        source=source,
        survey_snapshot=survey_snapshot,
        analysis_snapshot=analysis_snapshot,
        admin=admin,
        designer=designer,
        direct_consultation=direct_consultation,
    )
    if legacy_direct_result is None:
        raise ValueError("Legacy result tables are required to confirm a selection.")

    selected_style_id = legacy_direct_result["selected_style_id"]
    selected_style_reference = (
        get_style_record(style_id=int(selected_style_id))
        if selected_style_id is not None
        else None
    )

    return {
        "status": "success",
        "consultation_id": legacy_direct_result["consultation_id"],
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "selected_style_id": selected_style_id,
        "selected_style_name": (
            legacy_direct_result["selected_style_name"]
            or getattr(selected_style_reference, "name", None)
            or getattr(selected_style_reference, "style_name", None)
        ),
        "source": source,
        "direct_consultation": direct_consultation,
        "recommendation_id": legacy_direct_result["recommendation_id"],
        "selection_record_status": legacy_direct_result.get("selection_record_status"),
        "consultation_record_status": legacy_direct_result.get("consultation_record_status"),
        "consultation_replaced_previous": bool(legacy_direct_result.get("consultation_replaced_previous")),
        "closed_consultation_count": int(legacy_direct_result.get("closed_consultation_count") or 0),
        "message": (
            "추천 선택 없이 바로 상담 요청이 접수되었습니다."
            if direct_consultation
            else "선택한 스타일과 분석 요약이 상담 요청으로 접수되었습니다."
        ),
    }

def cancel_style_selection(
    *,
    client: "Client",
    recommendation_id: int | None = None,
    selected_image_url: str | None = None,
    source: str = "current_recommendations",
) -> dict:
    if not _cancel_legacy_result_directly(
        client=client,
        recommendation_id=recommendation_id,
        selected_image_url=selected_image_url,
    ):
        raise ValueError("The recommendation to cancel could not be found.")

    return {
        "status": "cancelled",
        "client_id": client.id,
        "legacy_client_id": get_legacy_client_id(client=client),
        "source": source,
        "next_action": "client_input",
        "message": "선택한 스타일이 취소되어 다시 처음 단계로 돌아갈 수 있습니다.",
    }


def run_mirrai_analysis_pipeline(record_id: int, processed_bytes: bytes | None = None):
    """Phase 1: Store image + call RunPod analyze_face → save face analysis to DB. No hairstyle generation."""
    logger.info("[PIPELINE THREAD] Record %s started. has_processed_bytes=%s", record_id, bool(processed_bytes))
    try:
        record = mark_legacy_capture_processing(record_id=record_id)
        if record is None or record.status != "PROCESSING":
            logger.warning("[PIPELINE THREAD] Record %s early exit. record_found=%s", record_id, record is not None)
            return

        storage_snapshot = build_storage_snapshot(
            original_path=record.original_path,
            processed_path=record.processed_path,
            deidentified_path=record.deidentified_path,
        )
        logger.info(
            "[PIPELINE START] Record %s storage_mode=%s bucket=%s path_count=%s",
            record_id,
            storage_snapshot["storage_mode"],
            storage_snapshot["bucket_name"],
            storage_snapshot["path_count"],
        )

        # Step 1: Store processed image to Supabase for later use at survey time
        analysis_input_reference = persist_analysis_input_image_reference(
            processed_bytes,
            extension=".jpg",
            mime_type="image/jpeg",
        ) if processed_bytes else (record.processed_path or record.original_path)
        logger.info(
            "[PIPELINE] Record %s image stored. analysis_input_reference=%s",
            record_id,
            bool(analysis_input_reference),
        )

        # Step 2: Call RunPod action=analyze_face (warm up cold start + get face data)
        face_result = analyze_face_with_runpod(image_bytes=processed_bytes)
        if face_result:
            face_shape = face_result["face_shape"]
            golden_ratio_score = face_result["golden_ratio_score"]
            logger.info(
                "[PIPELINE] Record %s RunPod face analysis done. face_shape=%s golden_ratio_score=%s",
                record_id,
                face_shape,
                golden_ratio_score,
            )
        else:
            # Local fallback when RunPod is unavailable
            face_shape = "Oval"
            golden_ratio_score = 0.85
            logger.warning(
                "[PIPELINE] Record %s RunPod analyze_face unavailable, using local fallback.",
                record_id,
            )

        # Step 3: Save face analysis + image reference to DB
        record, analysis = complete_legacy_capture_analysis(
            record_id=record_id,
            face_shape=face_shape,
            golden_ratio_score=golden_ratio_score,
            landmark_snapshot=record.landmark_snapshot,
            analysis_image_url=analysis_input_reference,
        )
        if record is None or analysis is None:
            return

        sync_model_team_runtime_state(client=record.client)
        logger.info(
            "[PIPELINE SUCCESS] Record %s face analysis saved. face_shape=%s has_image_ref=%s",
            record_id,
            face_shape,
            bool(analysis_input_reference),
        )

    except Exception as exc:
        logger.error("[PIPELINE ERROR] Record %s: %s", record_id, exc)
        fail_legacy_capture_processing(record_id=record_id, error_note=str(exc))


_HAIRSTYLE_PIPELINE_WAIT_TIMEOUT = 60   # 카메라 파이프라인 완료 최대 대기 시간(초)
_HAIRSTYLE_PIPELINE_WAIT_INTERVAL = 3  # 재조회 간격(초)


def run_hairstyle_generation_pipeline(client: "Client", survey) -> None:
    """Phase 2: After survey submit, generate hairstyle simulations using face analysis + survey preferences."""
    client_id = client.id
    logger.info("[HAIRSTYLE PIPELINE] client_id=%s started.", client_id)
    try:
        latest_capture = get_latest_capture(client)
        if _has_failed_recommendation_inputs(
            latest_capture_attempt=get_latest_capture_attempt(client),
            latest_capture=latest_capture,
            latest_analysis=get_latest_analysis(client),
        ):
            logger.info(
                "[HAIRSTYLE PIPELINE] client_id=%s skipped because the latest capture or analysis is failed.",
                client_id,
            )
            return

        # 카메라 파이프라인이 아직 실행 중일 수 있으므로 analysis_image_url이 채워질 때까지 대기
        analysis = None
        waited = 0
        while waited <= _HAIRSTYLE_PIPELINE_WAIT_TIMEOUT:
            analysis = get_latest_analysis(client)
            if analysis and analysis.image_url:
                break
            logger.info(
                "[HAIRSTYLE PIPELINE] client_id=%s waiting for face analysis... waited=%ss analysis_found=%s image_url=%s",
                client_id,
                waited,
                analysis is not None,
                repr(getattr(analysis, "image_url", None)),
            )
            time.sleep(_HAIRSTYLE_PIPELINE_WAIT_INTERVAL)
            waited += _HAIRSTYLE_PIPELINE_WAIT_INTERVAL

        if analysis is None:
            logger.warning("[HAIRSTYLE PIPELINE] client_id=%s no face analysis found after %ss, skipping.", client_id, waited)
            return

        image_url = resolve_storage_reference(analysis.image_url) if analysis.image_url else None
        if not image_url:
            logger.info(
                "[HAIRSTYLE PIPELINE] client_id=%s analysis image is not ready yet (analysis_image_url=%s) after %ss, skipping.",
                client_id,
                repr(analysis.image_url),
                waited,
            )
            return

        analysis_data = {
            "face_shape": analysis.face_shape,
            "golden_ratio_score": analysis.golden_ratio_score,
            "image_url": image_url,
            "landmark_snapshot": analysis.landmark_snapshot,
        }
        survey_data = {
            "target_length": getattr(survey, "target_length", None),
            "target_vibe": getattr(survey, "target_vibe", None),
            "scalp_type": getattr(survey, "scalp_type", None),
            "hair_colour": getattr(survey, "hair_colour", None),
            "budget_range": getattr(survey, "budget_range", None),
        }
        logger.info(
            "[HAIRSTYLE PIPELINE] client_id=%s calling RunPod. face_shape=%s image_url_set=%s survey_data=%s",
            client_id,
            analysis_data.get("face_shape"),
            bool(image_url),
            {k: v for k, v in survey_data.items() if v},
        )

        capture_record = latest_capture or get_latest_capture(client)
        items = generate_recommendation_batch(
            client_id=client_id,
            survey_data=survey_data,
            analysis_data=analysis_data,
            styles_by_id=ensure_catalog_styles(),
        )
        persist_generated_batch(
            client=client,
            capture_record=capture_record,
            survey=survey,
            analysis=analysis,
            precomputed_items=items,
        )
        logger.info(
            "[HAIRSTYLE PIPELINE] client_id=%s done. items=%s simulated=%s",
            client_id,
            len(items),
            sum(1 for item in items if item.get("simulation_image_url")),
        )

    except Exception as exc:
        logger.error("[HAIRSTYLE PIPELINE ERROR] client_id=%s: %s", client_id, exc)

