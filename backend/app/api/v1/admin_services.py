import json
import os
import re
from collections import Counter
from urllib import error, request

from django.contrib.auth.hashers import check_password, make_password
from django.db.models import Count, Q
from django.utils import timezone

from app.api.v1.admin_auth import TOKEN_MAX_AGE_SECONDS, build_admin_token
from app.api.v1.recommendation_logic import STYLE_CATALOG
from app.api.v1.services_django import ensure_catalog_styles, get_latest_analysis, get_latest_survey, serialize_recommendation_row
from app.models_django import AdminAccount, CaptureRecord, ConsultationRequest, Client, ClientSessionNote, FormerRecommendation, Style, StyleSelection
from app.services.age_profile import build_client_age_profile
from app.services.storage_service import resolve_storage_reference


def _normalize_phone(value: str) -> str:
    return value.replace("-", "").strip()


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
    base_url = os.environ.get("MIRRAI_AI_SERVICE_URL", "").rstrip("/")
    if not base_url:
        return {
            "status": "fallback",
            "mode": "local",
            "message": "AI service URL is not configured. Local fallback is active.",
            "checked_at": timezone.now(),
        }

    try:
        with request.urlopen(f"{base_url}/internal/health", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {
            "status": "online",
            "mode": "remote",
            "message": payload.get("role", "ai-microservice"),
            "checked_at": timezone.now(),
        }
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "status": "offline",
            "mode": "remote",
            "message": str(exc),
            "checked_at": timezone.now(),
        }


def _serialize_survey(survey) -> dict | None:
    if not survey:
        return None
    return {
        "target_length": survey.target_length,
        "target_vibe": survey.target_vibe,
        "scalp_type": survey.scalp_type,
        "hair_colour": survey.hair_colour,
        "budget_range": survey.budget_range,
        "preference_vector": survey.preference_vector or [],
        "created_at": survey.created_at,
    }


def _serialize_analysis(analysis) -> dict | None:
    if not analysis:
        return None
    return {
        "face_shape": analysis.face_shape,
        "golden_ratio_score": analysis.golden_ratio_score,
        "image_url": resolve_storage_reference(analysis.image_url),
        "landmark_snapshot": analysis.landmark_snapshot,
        "created_at": analysis.created_at,
    }


def _serialize_capture(record: CaptureRecord) -> dict:
    privacy_snapshot = record.privacy_snapshot or {}
    return {
        "record_id": record.id,
        "status": record.status,
        "face_count": record.face_count,
        "landmark_snapshot": record.landmark_snapshot,
        "deidentified_image_url": resolve_storage_reference(record.deidentified_path),
        "privacy_snapshot": privacy_snapshot,
        "image_storage_policy": privacy_snapshot.get("storage_policy", "asset_store"),
        "error_note": record.error_note,
        "original_image_url": resolve_storage_reference(record.original_path),
        "processed_image_url": resolve_storage_reference(record.processed_path),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _style_snapshot(style_id: int) -> dict:
    styles_by_id = ensure_catalog_styles()
    style = styles_by_id.get(style_id) or Style.objects.filter(id=style_id).first()
    if not style:
        return {
            "style_id": style_id,
            "style_name": f"Style {style_id}",
            "image_url": None,
            "description": "",
            "keywords": [],
        }

    profile = next((item for item in STYLE_CATALOG if item.style_id == style_id), None)
    keywords = list(profile.keywords) if profile else ([style.vibe] if style.vibe else [])
    return {
        "style_id": style.id,
        "style_name": style.name,
        "image_url": resolve_storage_reference(style.image_url),
        "description": style.description or "",
        "keywords": keywords,
    }


def _serialize_recommendation(row: FormerRecommendation) -> dict:
    return serialize_recommendation_row(row)


def _serialize_style_selection(selection: StyleSelection) -> dict:
    style_snapshot = _style_snapshot(selection.style_id)
    return {
        "selection_id": selection.id,
        "style_id": selection.style_id,
        "style_name": style_snapshot["style_name"],
        "image_url": style_snapshot["image_url"],
        "description": style_snapshot["description"],
        "source": selection.source,
        "match_score": selection.match_score,
        "is_sent_to_admin": selection.is_sent_to_admin,
        "created_at": selection.created_at,
    }


def _serialize_admin_profile(admin: AdminAccount) -> dict:
    formatted_business_number = (
        _format_business_number(admin.business_number)
        if len(admin.business_number) == 10 and admin.business_number.isdigit()
        else admin.business_number
    )
    return {
        "admin_id": admin.id,
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


def _client_age_fields(client: Client) -> dict:
    profile = build_client_age_profile(client) or {}
    return {
        "age": profile.get("current_age"),
        "age_decade": profile.get("age_decade"),
        "age_segment": profile.get("age_segment"),
        "age_group": profile.get("age_group"),
    }


def _admin_client_ids(admin: AdminAccount | None) -> set[int] | None:
    if admin is None:
        return None

    client_ids = set(
        ConsultationRequest.objects.filter(admin=admin).values_list("client_id", flat=True)
    )
    client_ids.update(
        ClientSessionNote.objects.filter(admin=admin).values_list("client_id", flat=True)
    )
    return client_ids


def _scoped_client_queryset(*, admin: AdminAccount | None = None):
    if admin is None:
        return Client.objects.all()

    client_ids = _admin_client_ids(admin)
    if not client_ids:
        return Client.objects.all()
    return Client.objects.filter(id__in=client_ids)


def _scoped_consultation_queryset(*, admin: AdminAccount | None = None):
    if admin is None:
        return ConsultationRequest.objects.all()

    client_ids = _admin_client_ids(admin)
    if not client_ids:
        return ConsultationRequest.objects.all()
    return ConsultationRequest.objects.filter(admin=admin)


def get_admin_profile(*, admin: AdminAccount) -> dict:
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

    if AdminAccount.objects.filter(phone=phone).exists():
        raise ValueError("This phone number is already registered for an admin account.")
    if not _is_valid_business_number(business_number):
        raise ValueError("The business registration number is not valid.")
    if AdminAccount.objects.filter(Q(business_number__in=_business_number_variants(business_number))).exists():
        raise ValueError("This business registration number is already registered.")

    admin = AdminAccount.objects.create(
        name=payload["name"],
        store_name=payload["store_name"],
        role=payload.get("role", "owner"),
        phone=phone,
        business_number=business_number,
        password_hash=make_password(payload["password"]),
        consent_snapshot=consent_snapshot,
        consented_at=timezone.now(),
    )
    token = build_admin_token(admin=admin)
    return {
        "status": "success",
        "admin_id": admin.id,
        "admin": _serialize_admin_profile(admin),
        "access_token": token,
        "token_type": "bearer",
        "expires_in": TOKEN_MAX_AGE_SECONDS,
    }


def login_admin(*, phone: str, password: str) -> dict:
    phone = _normalize_phone(phone)
    admin = AdminAccount.objects.filter(phone=phone, is_active=True).first()
    if not admin or not check_password(password, admin.password_hash):
        raise ValueError("Please check the admin account credentials and try again.")
    token = build_admin_token(admin=admin)
    return {
        "status": "success",
        "admin": _serialize_admin_profile(admin),
        "access_token": token,
        "token_type": "bearer",
        "expires_in": TOKEN_MAX_AGE_SECONDS,
    }


def _today_client_ids(*, admin: AdminAccount | None = None) -> set[int]:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    clients = _scoped_client_queryset(admin=admin)
    capture_ids = set(clients.filter(captures__created_at__gte=start).values_list("id", flat=True))
    consult_ids = set(clients.filter(consultations__created_at__gte=start).values_list("id", flat=True))
    return capture_ids | consult_ids


def _latest_active_consultations(*, admin: AdminAccount | None = None) -> list[ConsultationRequest]:
    rows = _scoped_consultation_queryset(admin=admin).filter(is_active=True).select_related("client", "selected_style", "selected_recommendation").order_by("-created_at")
    seen: set[int] = set()
    latest_rows: list[ConsultationRequest] = []
    for row in rows:
        if row.client_id in seen:
            continue
        seen.add(row.client_id)
        latest_rows.append(row)
    return latest_rows


def get_admin_dashboard_summary(*, admin: AdminAccount | None = None) -> dict:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    styles_by_id = ensure_catalog_styles()
    admin_client_ids = _admin_client_ids(admin)
    style_selection_queryset = StyleSelection.objects.filter(created_at__gte=start)
    if admin_client_ids:
        style_selection_queryset = style_selection_queryset.filter(client_id__in=admin_client_ids)
    top_rows = (
        style_selection_queryset
        .values("style_id")
        .annotate(selection_count=Count("id"))
        .order_by("-selection_count", "style_id")[:5]
    )
    top_styles = []
    for row in top_rows:
        style = styles_by_id.get(row["style_id"]) or Style.objects.filter(id=row["style_id"]).first()
        top_styles.append(
            {
                "style_id": row["style_id"],
                "style_name": style.name if style else f"Style {row['style_id']}",
                "image_url": resolve_storage_reference(style.image_url) if style else None,
                "selection_count": row["selection_count"],
            }
        )

    active_consultations = _latest_active_consultations(admin=admin)
    active_preview = [
        {
            "consultation_id": row.id,
            "client_id": row.client_id,
            "client_name": row.client.name,
            "phone": row.client.phone,
            "has_unread_consultation": not row.is_read,
            "status": row.status,
            "selected_style_name": row.selected_style.name if row.selected_style else None,
            "created_at": row.created_at,
        }
        for row in active_consultations[:5]
    ]
    return {
        "status": "ready",
        "ai_engine": _ai_health(),
        "today_metrics": {
            "unique_visitors": len(_today_client_ids(admin=admin)),
            "active_clients": len(active_consultations),
            "pending_consultations": sum(1 for row in active_consultations if not row.is_read),
            "confirmed_styles": style_selection_queryset.count(),
        },
        "top_styles_today": top_styles,
        "active_clients_preview": active_preview,
    }


def get_active_client_sessions(*, admin: AdminAccount | None = None) -> dict:
    items = []
    for row in _latest_active_consultations(admin=admin):
        recommendation_count = 0
        if row.selected_recommendation:
            recommendation_count = FormerRecommendation.objects.filter(
                client_id=row.client_id,
                batch_id=row.selected_recommendation.batch_id,
            ).count()
        items.append(
            {
                "consultation_id": row.id,
                "client_id": row.client_id,
                "client_name": row.client.name,
                "phone": row.client.phone,
                "status": row.status,
                "has_unread_consultation": not row.is_read,
                "selected_style_name": row.selected_style.name if row.selected_style else None,
                "recommendation_count": recommendation_count,
                "last_activity_at": row.created_at,
            }
        )
    return {"status": "ready", "items": items}


def get_all_clients(*, query: str = "", admin: AdminAccount | None = None) -> dict:
    queryset = _scoped_client_queryset(admin=admin).order_by("name", "id")
    if query:
        queryset = queryset.filter(Q(name__icontains=query) | Q(phone__icontains=query))

    items = []
    for client in queryset[:100]:
        latest_consult = client.consultations.order_by("-created_at").first()
        items.append(
            {
                "client_id": client.id,
                "name": client.name,
                "gender": client.gender,
                "phone": client.phone,
                **_client_age_fields(client),
                "created_at": client.created_at,
                "last_consulted_at": latest_consult.created_at if latest_consult else None,
                "has_active_consultation": client.consultations.filter(is_active=True).exists(),
            }
        )
    return {"status": "ready", "items": items}


def get_client_detail(*, client: Client, admin: AdminAccount | None = None) -> dict:
    scoped_client_ids = _admin_client_ids(admin)
    if scoped_client_ids and client.id not in scoped_client_ids:
        raise ValueError("Client is outside the current admin scope.")

    latest_survey = get_latest_survey(client)
    latest_analysis = get_latest_analysis(client)
    consultation_queryset = client.consultations
    if admin is not None and scoped_client_ids:
        consultation_queryset = consultation_queryset.filter(admin=admin)
    latest_consultation = consultation_queryset.order_by("-created_at").first()
    notes_queryset = ClientSessionNote.objects.filter(client=client).select_related("admin", "consultation")
    if admin is not None and scoped_client_ids:
        notes_queryset = notes_queryset.filter(admin=admin)
    notes = notes_queryset.order_by("-created_at")[:20]
    capture_history = client.captures.order_by("-created_at")[:20]
    analysis_history = client.face_analyses.order_by("-created_at")[:20]
    selection_history = client.style_selections.order_by("-created_at")[:20]
    chosen_recommendations = FormerRecommendation.objects.filter(client=client, is_chosen=True).order_by("-chosen_at", "-created_at")[:20]

    return {
        "status": "ready",
        "client": {
            "client_id": client.id,
            "name": client.name,
            "gender": client.gender,
            "phone": client.phone,
            **_client_age_fields(client),
            "created_at": client.created_at,
        },
        "latest_survey": _serialize_survey(latest_survey),
        "latest_analysis": _serialize_analysis(latest_analysis),
        "capture_history": [_serialize_capture(record) for record in capture_history],
        "analysis_history": [_serialize_analysis(analysis) for analysis in analysis_history],
        "style_selection_history": [_serialize_style_selection(selection) for selection in selection_history],
        "chosen_recommendation_history": [_serialize_recommendation(row) for row in chosen_recommendations],
        "active_consultation": (
            {
                "consultation_id": latest_consultation.id,
                "status": latest_consultation.status,
                "is_active": latest_consultation.is_active,
                "is_read": latest_consultation.is_read,
                "source": latest_consultation.source,
                "created_at": latest_consultation.created_at,
                "closed_at": latest_consultation.closed_at,
            }
            if latest_consultation
            else None
        ),
        "notes": [
            {
                "note_id": note.id,
                "consultation_id": note.consultation_id,
                "admin_id": note.admin_id,
                "admin_name": note.admin.name if note.admin else None,
                "content": note.content,
                "created_at": note.created_at,
            }
            for note in notes
        ],
    }


def get_client_recommendation_report(*, client: Client, admin: AdminAccount | None = None) -> dict:
    scoped_client_ids = _admin_client_ids(admin)
    if scoped_client_ids and client.id not in scoped_client_ids:
        raise ValueError("Client is outside the current admin scope.")

    latest_analysis = get_latest_analysis(client)
    latest_survey = get_latest_survey(client)
    recommendation_queryset = FormerRecommendation.objects.filter(client=client, source="generated")
    if admin is not None and scoped_client_ids:
        recommendation_queryset = recommendation_queryset.filter(client_id__in=scoped_client_ids)
    latest_generated = recommendation_queryset.order_by("-created_at").first()
    batch_rows = []
    if latest_generated:
        batch_rows = list(FormerRecommendation.objects.filter(client=client, batch_id=latest_generated.batch_id).order_by("rank", "id"))
    final_selected = FormerRecommendation.objects.filter(client=client, is_chosen=True).order_by("-chosen_at", "-created_at").first()

    return {
        "status": "ready",
        "client": {
            "client_id": client.id,
            "name": client.name,
            "phone": client.phone,
            **_client_age_fields(client),
        },
        "latest_survey": _serialize_survey(latest_survey),
        "latest_analysis": _serialize_analysis(latest_analysis),
        "final_selected_style": (_serialize_recommendation(final_selected) if final_selected else None),
        "latest_generated_batch": {
            "batch_id": str(latest_generated.batch_id) if latest_generated else None,
            "items": [_serialize_recommendation(row) for row in batch_rows],
        },
    }


def create_client_note(*, client: Client, consultation_id: int, content: str, admin: AdminAccount | None = None) -> dict:
    consultation = ConsultationRequest.objects.filter(id=consultation_id, client=client).first()
    if not consultation:
        raise ValueError("The consultation session could not be found.")

    if admin is not None and consultation.admin_id is None:
        consultation.admin = admin
        consultation.save(update_fields=["admin"])

    note = ClientSessionNote.objects.create(
        consultation=consultation,
        client=client,
        admin=admin,
        content=content.strip(),
    )
    consultation.is_read = True
    consultation.status = "IN_PROGRESS"
    consultation.save(update_fields=["is_read", "status"])
    return {
        "status": "success",
        "note_id": note.id,
        "consultation_id": consultation.id,
        "message": "The consultation note has been saved.",
    }


def close_consultation_session(*, consultation_id: int, admin: AdminAccount | None = None) -> dict:
    consultation = ConsultationRequest.objects.filter(id=consultation_id).select_related("client").first()
    if not consultation:
        raise ValueError("The consultation session could not be found.")

    if admin is not None and consultation.admin_id is None:
        consultation.admin = admin

    consultation.is_active = False
    consultation.is_read = True
    consultation.status = "CLOSED"
    consultation.closed_at = timezone.now()
    update_fields = ["is_active", "is_read", "status", "closed_at"]
    if admin is not None and consultation.admin_id == admin.id:
        update_fields.append("admin")
    consultation.save(update_fields=update_fields)
    return {
        "status": "success",
        "consultation_id": consultation.id,
        "client_id": consultation.client_id,
        "message": "The consultation session has been closed.",
    }


def _selection_matches_snapshot(selection: StyleSelection, filters: dict) -> bool:
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
        if snapshot.get(key) != value:
            return False
    return True


def get_admin_trend_report(*, days: int = 7, filters: dict | None = None, admin: AdminAccount | None = None) -> dict:
    filters = filters or {}
    cutoff = timezone.now() - timezone.timedelta(days=days)
    selections_queryset = StyleSelection.objects.filter(created_at__gte=cutoff).select_related("client").order_by("-created_at")
    scoped_client_ids = _admin_client_ids(admin)
    if admin is not None and scoped_client_ids:
        selections_queryset = selections_queryset.filter(client_id__in=scoped_client_ids)
    selections = list(selections_queryset)
    filtered = [row for row in selections if _selection_matches_snapshot(row, filters)]

    counter = Counter(row.style_id for row in filtered)
    ranking = []
    for rank, (style_id, count) in enumerate(counter.most_common(10), start=1):
        style_data = _style_snapshot(style_id)
        ranking.append(
            {
                "rank": rank,
                "style_id": style_id,
                "style_name": style_data["style_name"],
                "image_url": style_data["image_url"],
                "selection_count": count,
                "keywords": style_data["keywords"],
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
    age_decade_counter = Counter()
    age_group_counter = Counter()
    for row in filtered:
        profile = build_client_age_profile(row.client)
        if not profile:
            continue
        if profile.get("age_decade"):
            age_decade_counter[profile["age_decade"]] += 1
        if profile.get("age_group"):
            age_group_counter[profile["age_group"]] += 1
    unique_clients = len({row.client_id for row in filtered})
    return {
        "status": "ready",
        "days": days,
        "filters": filters,
        "kpi": {
            "unique_clients": unique_clients,
            "total_confirmations": len(filtered),
            "active_consultations": len(_latest_active_consultations(admin=admin)),
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
    }


def get_style_report(*, style_id: int, days: int = 7, admin: AdminAccount | None = None) -> dict:
    style_data = _style_snapshot(style_id)
    cutoff = timezone.now() - timezone.timedelta(days=days)
    recent_queryset = StyleSelection.objects.filter(style_id=style_id, created_at__gte=cutoff)
    chosen_queryset = FormerRecommendation.objects.filter(style_id_snapshot=style_id, is_chosen=True)
    scoped_client_ids = _admin_client_ids(admin)
    if admin is not None and scoped_client_ids:
        recent_queryset = recent_queryset.filter(client_id__in=scoped_client_ids)
        chosen_queryset = chosen_queryset.filter(client_id__in=scoped_client_ids)
    recent_count = recent_queryset.count()
    chosen_count = chosen_queryset.count()

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

    return {
        "status": "ready",
        "style": {
            **style_data,
            "recent_selection_count": recent_count,
            "chosen_count": chosen_count,
        },
        "related_styles": related,
    }

