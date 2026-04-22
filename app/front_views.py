from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.hashers import check_password, identify_hasher, make_password
from django.core.cache import cache
from django.http import HttpRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache

from app.api.v1.admin_services import register_admin
from app.api.v1.services_django import get_former_recommendations
from app.services.model_team_bridge import (
    create_designer_record,
    get_admin_by_identifier,
    get_admin_by_legacy_id,
    get_admin_by_phone,
    get_client_by_phone,
    get_client_by_identifier,
    get_designer_for_admin,
    get_designers_for_admin,
    get_legacy_admin_id,
    get_legacy_designer_id,
    has_legacy_chosen_consultation,
    update_designer_active_state,
    upsert_client_record,
)
from app.services.runtime_cache import (
    build_partner_cache_key,
    cache_timeout,
    get_cached_payload,
    invalidate_partner_scope_cache,
    set_cached_payload,
)
from app.session_state import (
    allow_owner_dashboard,
    allow_owner_mypage,
    can_access_owner_dashboard,
    can_access_owner_mypage,
    clear_admin_session,
    clear_customer_session,
    clear_designer_session,
    get_session_admin,
    get_session_customer,
    get_session_designer,
    revoke_designer_dashboard,
    revoke_all_owner_scopes,
    revoke_owner_dashboard,
    set_admin_session,
    set_customer_session,
    set_designer_session,
)

if TYPE_CHECKING:
    from app.models_django import AdminAccount, Client, Designer


def _normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _normalize_business_number(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _designer_pin_policy() -> tuple[int, int, int]:
    max_attempts = max(1, cache_timeout("DESIGNER_PIN_MAX_ATTEMPTS", 3))
    lock_seconds = max(60, cache_timeout("DESIGNER_PIN_LOCK_SECONDS", 900))
    fail_window_seconds = max(lock_seconds, cache_timeout("DESIGNER_PIN_FAIL_WINDOW_SECONDS", lock_seconds))
    return max_attempts, lock_seconds, fail_window_seconds


def _designer_pin_scope_token(request: HttpRequest) -> str:
    forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    remote_addr = forwarded_for or (request.META.get("REMOTE_ADDR") or "unknown")
    user_agent = (request.META.get("HTTP_USER_AGENT") or "").strip()[:120]
    payload = f"{remote_addr}|{user_agent}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _designer_pin_lock_keys(*, admin, designer_id: str, request: HttpRequest) -> tuple[str, str]:
    prefix = getattr(settings, "REDIS_KEY_PREFIX", "mirrai")
    admin_identity = str(getattr(admin, "id", None) or get_legacy_admin_id(admin=admin) or "none")
    designer_identity = str(designer_id or "none").strip() or "none"
    scope_identity = _designer_pin_scope_token(request)
    base = f"{prefix}:auth:designer-pin:{admin_identity}:{designer_identity}:{scope_identity}"
    return f"{base}:fails", f"{base}:locked-until"


def _get_designer_pin_lock_state(*, admin, designer_id: str, request: HttpRequest) -> dict[str, int | bool]:
    max_attempts, _, _ = _designer_pin_policy()
    fails_key, lock_key = _designer_pin_lock_keys(admin=admin, designer_id=designer_id, request=request)

    now_ts = timezone.now().timestamp()
    locked_until = cache.get(lock_key)
    try:
        locked_until_ts = float(locked_until or 0.0)
    except (TypeError, ValueError):
        locked_until_ts = 0.0

    if locked_until_ts > now_ts:
        return {
            "is_locked": True,
            "remaining_lock_seconds": max(1, int(locked_until_ts - now_ts)),
            "remaining_attempts": 0,
        }

    if locked_until:
        cache.delete(lock_key)

    fail_count_raw = cache.get(fails_key)
    try:
        fail_count = int(fail_count_raw or 0)
    except (TypeError, ValueError):
        fail_count = 0
    return {
        "is_locked": False,
        "remaining_lock_seconds": 0,
        "remaining_attempts": max(0, max_attempts - fail_count),
    }


def _record_designer_pin_failure(*, admin, designer_id: str, request: HttpRequest) -> dict[str, int | bool]:
    max_attempts, lock_seconds, fail_window_seconds = _designer_pin_policy()
    current_state = _get_designer_pin_lock_state(admin=admin, designer_id=designer_id, request=request)
    if current_state["is_locked"]:
        return current_state

    fails_key, lock_key = _designer_pin_lock_keys(admin=admin, designer_id=designer_id, request=request)
    if cache.add(fails_key, 1, timeout=fail_window_seconds):
        fail_count = 1
    else:
        try:
            fail_count = int(cache.incr(fails_key))
        except Exception:
            cached_count = cache.get(fails_key)
            try:
                fail_count = int(cached_count or 0) + 1
            except (TypeError, ValueError):
                fail_count = 1
            cache.set(fails_key, fail_count, timeout=fail_window_seconds)

    if fail_count >= max_attempts:
        lock_until_ts = timezone.now().timestamp() + lock_seconds
        cache.set(lock_key, lock_until_ts, timeout=lock_seconds)
        cache.delete(fails_key)
        return {
            "is_locked": True,
            "remaining_lock_seconds": lock_seconds,
            "remaining_attempts": 0,
        }

    return {
        "is_locked": False,
        "remaining_lock_seconds": 0,
        "remaining_attempts": max(0, max_attempts - fail_count),
    }


def _clear_designer_pin_failures(*, admin, designer_id: str, request: HttpRequest) -> None:
    fails_key, lock_key = _designer_pin_lock_keys(admin=admin, designer_id=designer_id, request=request)
    cache.delete_many([fails_key, lock_key])


def _is_hashed_secret(value: str | None) -> bool:
    normalized = (value or "").strip()
    if not normalized:
        return False
    try:
        identify_hasher(normalized)
    except ValueError:
        return False
    return True


def _hash_admin_pin(value: str) -> str:
    return make_password(value)


def _matches_admin_pin(*, raw_pin: str, stored_pin: str | None) -> bool:
    normalized_pin = (raw_pin or "").strip()
    normalized_stored_pin = (stored_pin or "").strip() or "0000"
    if _is_hashed_secret(normalized_stored_pin):
        return check_password(normalized_pin, normalized_stored_pin)
    return normalized_pin == normalized_stored_pin


def _is_default_admin_pin(stored_pin: str | None) -> bool:
    return _matches_admin_pin(raw_pin="0000", stored_pin=stored_pin)


def _hash_admin_password(value: str) -> str:
    return make_password(value)


def _matches_admin_password(*, raw_password: str, stored_password: str | None) -> bool:
    normalized_password = (raw_password or "").strip()
    normalized_stored_password = (stored_password or "").strip()
    if not normalized_stored_password:
        return False
    if _is_hashed_secret(normalized_stored_password):
        return check_password(normalized_password, normalized_stored_password)
    return normalized_password == normalized_stored_password


def _get_admin_account_for_runtime_admin(admin):
    from app.models_django import AdminAccount

    runtime_id = str(getattr(admin, "id", "") or "").strip()
    if runtime_id:
        if runtime_id.isdigit():
            admin_obj = AdminAccount.objects.filter(backend_admin_id=int(runtime_id)).first()
            if admin_obj is not None:
                return admin_obj
        else:
            admin_obj = AdminAccount.objects.filter(id=runtime_id).first()
            if admin_obj is not None:
                return admin_obj

    backend_admin_id = getattr(admin, "backend_admin_id", None)
    if backend_admin_id not in (None, ""):
        admin_obj = AdminAccount.objects.filter(backend_admin_id=backend_admin_id).first()
        if admin_obj is not None:
            return admin_obj

    phone = _normalize_phone(getattr(admin, "phone", ""))
    if phone:
        return AdminAccount.objects.filter(phone=phone).order_by("-backend_admin_id").first()
    return None


def _sync_admin_account_state(*, request: HttpRequest, admin_obj) -> None:
    try:
        from app.services.model_team_bridge import sync_model_team_admin_state

        sync_model_team_admin_state(admin=admin_obj)
    except Exception:
        pass
    set_admin_session(request=request, admin=admin_obj)


def _upgrade_plain_admin_pin_if_needed(*, request: HttpRequest, admin_obj, raw_pin: str) -> bool:
    stored_pin = (getattr(admin_obj, "admin_pin", "") or "").strip() or "0000"
    if _is_hashed_secret(stored_pin):
        return False
    if stored_pin != raw_pin:
        return False

    admin_obj.admin_pin = _hash_admin_pin(stored_pin)
    admin_obj.save(update_fields=["admin_pin"])
    _sync_admin_account_state(request=request, admin_obj=admin_obj)
    return True


def _upgrade_plain_admin_password_if_needed(*, request: HttpRequest, admin_obj, raw_password: str) -> bool:
    stored_password = (
        (getattr(admin_obj, "password_hash", "") or "").strip()
        or (getattr(admin_obj, "password", "") or "").strip()
    )
    if not stored_password:
        return False
    if _is_hashed_secret(stored_password):
        return False
    if stored_password != (raw_password or "").strip():
        return False

    hashed_password = _hash_admin_password(stored_password)
    admin_obj.password_hash = hashed_password
    if hasattr(admin_obj, "password"):
        admin_obj.password = hashed_password
        admin_obj.save(update_fields=["password_hash", "password"])
    else:
        admin_obj.save(update_fields=["password_hash"])
    _sync_admin_account_state(request=request, admin_obj=admin_obj)
    return True


def _birth_year_from_age(age_value: str) -> int | None:
    if not age_value:
        return None
    try:
        age = int(age_value)
    except (TypeError, ValueError):
        return None
    if age <= 0:
        return None
    return timezone.localdate().year - age


def _popup_message_from_notice(notice: str | None) -> str | None:
    messages = {
        "partner_forbidden_customer": "고객 로그인 상태에서는 파트너 센터를 이용할 수 없습니다.",
        "partner_forbidden_designer": "디자이너 로그인 상태에서는 디자이너 페이지로 이동해 주세요.",
        "designer_created": "신규 디자이너 계정이 생성되었습니다. 목록에서 선택 후 로그인해 주세요.",
    }
    return messages.get((notice or "").strip())


def _safe_internal_next_url(value: str | None, *, default: str) -> str:
    candidate = (value or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return default


def _owner_gate_path(*, request: HttpRequest, scope: str = "dashboard", next_url: str | None = None) -> str:
    normalized_scope = "mypage" if (scope or "").strip().lower() == "mypage" else "dashboard"
    default_target = reverse("partner_mypage") if normalized_scope == "mypage" else reverse("partner_dashboard")
    target_url = _safe_internal_next_url(next_url or request.get_full_path(), default=default_target)
    return f"{reverse('partner_owner_gate')}?{urlencode({'scope': normalized_scope, 'next': target_url})}"


def _owner_scope_gate_response(*, request: HttpRequest, scope: str = "dashboard", next_url: str | None = None):
    if request.method != "GET":
        return None

    normalized_scope = "mypage" if (scope or "").strip().lower() == "mypage" else "dashboard"
    has_scope = (
        can_access_owner_mypage(request=request)
        if normalized_scope == "mypage"
        else can_access_owner_dashboard(request=request)
    )
    if has_scope:
        return None

    return redirect(_owner_gate_path(request=request, scope=normalized_scope, next_url=next_url))


def _render_customer_login(
    request: HttpRequest,
    *,
    error_message: str | None = None,
    popup_message: str | None = None,
):
    return render(
        request,
        "customer/index.html",
        {
            "form_error": error_message,
            "popup_message": popup_message or _popup_message_from_notice(request.GET.get("notice")),
        },
    )


def _render_partner_login(
    request: HttpRequest,
    *,
    error_message: str | None = None,
    popup_message: str | None = None,
    status: int = 200,
):
    return render(
        request,
        "admin/index.html",
        {
            "is_dashboard": False,
            "active_shop": get_session_admin(request=request),
            "form_error": error_message,
            "popup_message": popup_message or _popup_message_from_notice(request.GET.get("notice")),
        },
        status=status,
    )


def _customer_resume_route_for_client(*, client: Client) -> str:
    from app.api.v1.services_django import get_latest_capture, get_latest_survey
    from app.services.model_team_bridge import get_legacy_former_recommendation_items

    survey = get_latest_survey(client)
    if survey:
        return "customer_result"
    capture = get_latest_capture(client)
    if capture:
        return "customer_survey"
    return "customer_menu"


def _resolve_active_shop_and_designer(*, request: HttpRequest) -> tuple["AdminAccount | None", Designer | None]:
    designer = get_session_designer(request=request)
    admin = get_session_admin(request=request)
    if designer is not None:
        return designer.shop, designer
    return admin, None


def _has_standalone_customer_session(*, request: HttpRequest) -> bool:
    return (
        get_session_customer(request=request) is not None
        and get_session_admin(request=request) is None
        and get_session_designer(request=request) is None
    )


def _resolve_client_assignment_defaults(*, request: HttpRequest) -> dict:
    shop, designer = _resolve_active_shop_and_designer(request=request)
    defaults: dict = {}
    if shop is not None:
        defaults["shop"] = shop

    if designer is not None:
        defaults["designer"] = designer
        defaults["assigned_at"] = timezone.now()
        defaults["assignment_source"] = "designer_session"
        return defaults

    if shop is None:
        return defaults

    active_designers = get_designers_for_admin(admin=shop)[:2]
    if not active_designers:
        defaults["assigned_at"] = timezone.now()
        defaults["assignment_source"] = "auto_shop_only"
    elif len(active_designers) == 1:
        defaults["designer"] = active_designers[0]
        defaults["assigned_at"] = timezone.now()
        defaults["assignment_source"] = "auto_single_designer"
    else:
        defaults["assignment_source"] = "shop_manual_assignment_pending"
    return defaults


def health_check(request):
    return JsonResponse({"status": "django_running", "framework": "Django"})


@never_cache
def home_page(request):
    # 홈으로 나가면 파트너센터/내 페이지 PIN 인증 세션을 revoke
    # → 다시 진입할 때 PIN 재인증 필요
    revoke_all_owner_scopes(request=request)
    start_url = f"{reverse('partner_login')}?next={reverse('partner_designer_select')}?next={reverse('customer_index')}"
    return render(request, "index.html", {"start_url": start_url, "partner_url": reverse("partner_login")})


def terms_page(request):
    return render(request, "pages/terms.html")


def privacy_policy_page(request):
    return render(request, "pages/privacy_policy.html")


@never_cache
def client_login_page(request):
    if request.method == "GET" and get_session_customer(request=request) is not None:
        return redirect("customer_menu")
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        gender = (request.POST.get("gender") or "").strip()
        age_value = (request.POST.get("age") or "").strip()
        phone = _normalize_phone(request.POST.get("phone", ""))
        birth_year_estimate = _birth_year_from_age(age_value)
        agree_privacy = bool(request.POST.get("agree_privacy"))
        if not name:
            return _render_customer_login(request, error_message="이름을 입력해 주세요.")
        if gender not in {"male", "female"}:
            return _render_customer_login(request, error_message="성별을 선택해 주세요.")
        if birth_year_estimate is None:
            return _render_customer_login(request, error_message="연령을 올바르게 입력해 주세요.")
        if not phone:
            return _render_customer_login(request, error_message="연락처를 입력해 주세요.")
        if not agree_privacy:
            return _render_customer_login(request, error_message="AI 스타일 분석 데이터 수집 및 이용에 동의해 주세요.")

        defaults = {
            "name": name,
            "gender": gender,
            "age_input": (
                int(age_value)
                if age_value.isdigit()
                else None
            ),
            "birth_year_estimate": birth_year_estimate,
        }
        defaults.update(_resolve_client_assignment_defaults(request=request))

        client = upsert_client_record(
            phone=phone,
            name=defaults["name"],
            gender=defaults.get("gender"),
            age_input=defaults.get("age_input"),
            birth_year_estimate=defaults.get("birth_year_estimate"),
            shop=defaults.get("shop"),
            designer=defaults.get("designer"),
            assignment_source=defaults.get("assignment_source"),
        )
        set_customer_session(request=request, client=client)
        return redirect("customer_menu")

    return _render_customer_login(request)


@never_cache
def client_survey_page(request, gender=None):
    client = get_session_customer(request=request)
    if not client:
        return redirect("customer_index")

    raw_gender = gender if gender else client.gender
    normalized = str(raw_gender or "").strip().lower()
    if normalized in {"m", "male", "man", "남", "남성"}:
        display_gender = "male"
    else:
        display_gender = "female"

    return render(
        request,
        "customer/survey.html",
        {
            "client": client,
            "display_gender": display_gender,
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
        },
    )


@never_cache
def client_menu_page(request):
    client = get_session_customer(request=request)
    if not client:
        return redirect("customer_index")

    from app.api.v1.services_django import get_latest_capture, get_latest_survey

    survey = get_latest_survey(client)
    capture = get_latest_capture(client)
    has_completed_consultation = has_legacy_chosen_consultation(client=client)

    survey_at = getattr(survey, "created_at", None)
    capture_at = (
        getattr(capture, "updated_at", None)
        or getattr(capture, "created_at", None)
    )
    if survey_at is not None and timezone.is_naive(survey_at):
        survey_at = timezone.make_aware(survey_at, timezone.get_current_timezone())
    if capture_at is not None and timezone.is_naive(capture_at):
        capture_at = timezone.make_aware(capture_at, timezone.get_current_timezone())
    needs_survey_after_capture = bool(
        capture
        and (
            survey is None
            or (survey_at is not None and capture_at is not None and survey_at < capture_at)
        )
    )

    if has_completed_consultation:
        resume_step = None
        resume_step_label = None
        resume_url = None
    elif needs_survey_after_capture:
        resume_step = 2
        resume_step_label = "스타일 설문"
        resume_url = reverse("customer_survey")
    elif survey:
        resume_step = 3
        resume_step_label = "추천 결과 확인"
        resume_url = reverse("customer_result")
    else:
        # 첫 방문(진행 이력 없음)은 진행 상태 UI를 숨긴다.
        resume_step = None
        resume_step_label = None
        resume_url = None

    return render(
        request,
        "customer/menu.html",
        {
            "client": client,
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
            "resume_step": resume_step,
            "resume_step_label": resume_step_label,
            "resume_url": resume_url,
            "has_completed_consultation": has_completed_consultation,
        },
    )


@never_cache
def client_camera_page(request):
    client = get_session_customer(request=request)
    if not client:
        return redirect("customer_index")
    return render(
        request,
        "customer/camera.html",
        {"client": client, "popup_message": _popup_message_from_notice(request.GET.get("notice"))},
    )


@never_cache
def client_upload_page(request):
    client = get_session_customer(request=request)
    if not client:
        return redirect("customer_index")
    return render(
        request,
        "customer/upload.html",
        {"client": client, "popup_message": _popup_message_from_notice(request.GET.get("notice"))},
    )


@never_cache
def client_recommendation_page(request):
    client = get_session_customer(request=request)
    if not client:
        return redirect("customer_index")
    return render(
        request,
        "customer/result.html",
        {"client": client, "popup_message": _popup_message_from_notice(request.GET.get("notice"))},
    )


@never_cache
def client_recommendation_history_page(request):
    client = get_session_customer(request=request)
    if not client:
        return redirect("customer_index")
    payload = get_former_recommendations(client)
    history_items = payload.get("items", [])
    history_has_completed = any(bool(item.get("is_chosen")) for item in history_items if isinstance(item, dict))
    latest_completed_item = next(
        (item for item in history_items if isinstance(item, dict) and item.get("is_chosen")),
        None,
    )
    return render(
        request,
        "customer/history.html",
        {
            "client": client,
            "history_items": history_items,
            "history_has_completed": history_has_completed,
            "latest_completed_item": latest_completed_item,
            "history_message": payload.get("message"),
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
        },
    )


@never_cache
def client_trend_page(request):
    client = get_session_customer(request=request)
    if client is not None:
        back_url = reverse("customer_menu")
    elif get_session_designer(request=request) is not None:
        back_url = reverse("partner_staff_dashboard")
    elif get_session_admin(request=request) is not None:
        back_url = reverse("partner_dashboard")
    else:
        back_url = reverse("index")

    return render(
        request,
        "customer/trend.html",
        {
            "client": client,
            "back_url": back_url,
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
        },
    )


@never_cache
def customer_consultation_complete_page(request):
    client = get_session_customer(request=request)
    if not client:
        return redirect("customer_index")
    return render(
        request,
        "customer/consultation_complete.html",
        {
            "client": client,
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
        },
    )


@never_cache
def customer_resume_page(request):
    client = get_session_customer(request=request)
    if not client:
        return redirect("customer_index")

    notice = (request.GET.get("notice") or "").strip()
    # notice 파라미터가 있을 때는 기존처럼 해당 페이지로 바로 리다이렉트
    if notice:
        target = reverse(_customer_resume_route_for_client(client=client))
        return redirect(f"{target}?notice={notice}")

    from app.api.v1.services_django import get_latest_capture, get_latest_survey

    capture = get_latest_capture(client)
    survey = get_latest_survey(client)

    if survey:
        step = 3
        step_label = "추천 결과 확인"
        resume_url = reverse("customer_result")
    elif capture:
        step = 2
        step_label = "스타일 설문"
        resume_url = reverse("customer_survey")
    else:
        step = 1
        step_label = "서비스 선택"
        resume_url = reverse("customer_menu")

    return render(
        request,
        "customer/continue.html",
        {
            "client": client,
            "step": step,
            "step_label": step_label,
            "resume_url": resume_url,
        },
    )


@never_cache
def partner_designer_select_page(request: HttpRequest):
    admin = get_session_admin(request=request)
    if admin is None:
        login_url = reverse("partner_login")
        query = urlencode({"next": request.get_full_path()})
        return redirect(f"{login_url}?{query}")

    # 고객 세션이 있는 경우 템플릿으로 전달
    client = get_session_customer(request=request)
    active_designer = get_session_designer(request=request)
    return render(
        request,
        "admin/designer_select.html",
        {
            "client": client,
            "active_designer_id": getattr(active_designer, "id", "") or "",
        },
    )


@never_cache
def partner_owner_gate_page(request: HttpRequest):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    if get_session_designer(request=request) is not None:
        return redirect("partner_staff_dashboard")

    scope = "mypage" if (request.GET.get("scope") or "").strip().lower() == "mypage" else "dashboard"
    default_target = reverse("partner_mypage") if scope == "mypage" else reverse("partner_dashboard")
    target_url = _safe_internal_next_url(request.GET.get("next"), default=default_target)

    admin = get_session_admin(request=request)
    if admin is None:
        login_url = reverse("partner_login")
        query = urlencode({"next": request.get_full_path()})
        return redirect(f"{login_url}?{query}")

    if scope == "mypage" and can_access_owner_mypage(request=request):
        return redirect(target_url)
    if scope == "dashboard" and can_access_owner_dashboard(request=request):
        return redirect(target_url)

    gate_message = (
        "내 페이지 접근을 위해 관리자 보안키(PIN) 4자리를 입력해 주세요."
        if scope == "mypage"
        else "파트너 센터 접근을 위해 관리자 보안키(PIN) 4자리를 입력해 주세요."
    )
    return render(
        request,
        "admin/owner_gate.html",
        {
            "target_scope": scope,
            "target_url": target_url,
            "gate_message": gate_message,
            "cancel_url": reverse("index"),
        },
    )


@never_cache
def partner_customer_detail_page(request: HttpRequest, pk: int):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin, designer = _resolve_active_shop_and_designer(request=request)
    if admin is None:
        return redirect("partner_index")
    if designer is None:
        gate_response = _owner_scope_gate_response(request=request, scope="dashboard")
        if gate_response is not None:
            return gate_response

    client = get_client_by_identifier(identifier=pk)
    if client is None:
        return redirect("partner_index")

    if designer is not None:
        if client.designer_id != designer.id:
            return redirect("partner_staff_dashboard")
    elif client.shop_id != admin.id:
        return redirect("partner_dashboard")

    return render(
        request,
        "admin/customer_detail.html",
        {
            "client_id": pk,
            "show_customer_detail_chatbot": bool(designer is not None),
            "skip_owner_gate_for_current_view": bool(designer is not None),
            "is_designer_session": bool(designer is not None),
        },
    )


@never_cache
def customer_reanalysis_start_page(request, pk: int):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin, designer = _resolve_active_shop_and_designer(request=request)
    if admin is None:
        return redirect("partner_index")
    if designer is None:
        gate_response = _owner_scope_gate_response(request=request, scope="dashboard")
        if gate_response is not None:
            return gate_response

    client = get_client_by_identifier(identifier=pk)
    if client is None:
        return redirect("partner_index")

    if designer is not None:
        if client.designer_id != designer.id:
            return redirect("partner_staff_dashboard")
    elif client.shop_id != admin.id:
        return redirect("partner_dashboard")

    set_customer_session(request=request, client=client)
    return redirect(f"{reverse('customer_survey')}?reanalysis=1&customer_id={client.id}")


@never_cache
def admin_login_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")
    if get_session_designer(request=request) is not None:
        return redirect(f"{reverse('partner_staff_dashboard')}?notice=partner_forbidden_designer")
    admin = get_session_admin(request=request)
    if admin is not None:
        return redirect("partner_dashboard")
    return _render_partner_login(request)


@never_cache
def admin_signup_page(request):
    if request.method == "POST":
        password = request.POST.get("password", "")
        password_confirm = request.POST.get("password_confirm", "")
        payload = {
            "name": (request.POST.get("name") or "").strip(),
            "store_name": (request.POST.get("store_name") or "").strip(),
            "role": (request.POST.get("role") or "owner").strip() or "owner",
            "phone": _normalize_phone(request.POST.get("phone", "")),
            "business_number": (
                request.POST.get("business_number")
                or request.POST.get("biz_number")
                or ""
            ).strip(),
            "password": password,
            "agree_terms": bool(request.POST.get("agree_terms")),
            "agree_privacy": bool(request.POST.get("agree_privacy")),
            "agree_third_party_sharing": bool(request.POST.get("agree_third_party_sharing")),
            "agree_marketing": bool(request.POST.get("agree_marketing")),
        }
        if password_confirm and password != password_confirm:
            return render(
                request,
                "admin/signup.html",
                {
                    "form_error": "비밀번호 확인이 일치하지 않습니다.",
                    "form_values": payload,
                },
                status=400,
            )
        try:
            result = register_admin(payload=payload)
        except ValueError as exc:
            return render(
                request,
                "admin/signup.html",
                {
                    "form_error": str(exc),
                    "form_values": payload,
                },
                status=400,
            )

        admin = get_admin_by_legacy_id(legacy_admin_id=result.get("legacy_admin_id")) or get_admin_by_identifier(
            identifier=result.get("admin_id")
        )
        if admin is not None:
            clear_customer_session(request=request)
            clear_designer_session(request=request)
            set_admin_session(request=request, admin=admin)
            allow_owner_dashboard(request=request)
        return redirect("index")

    return render(request, "admin/signup.html")


@never_cache
def designer_signup_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin = get_session_admin(request=request)
    if admin is None or get_session_designer(request=request) is not None:
        return redirect("partner_index")
    gate_response = _owner_scope_gate_response(request=request, scope="dashboard")
    if gate_response is not None:
        return gate_response

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        phone = _normalize_phone(request.POST.get("phone", ""))
        pin = (request.POST.get("pin") or "").strip()
        pin_confirm = (request.POST.get("pin_confirm") or "").strip()

        form_values = {
            "name": name,
            "phone": phone,
        }

        if not name:
            return render(
                request,
                "admin/designer_signup.html",
                {"form_error": "디자이너 이름은 필수 정보입니다.", "form_values": form_values},
                status=400,
            )
        if not phone:
            return render(
                request,
                "admin/designer_signup.html",
                {"form_error": "디자이너 연락처는 필수 정보입니다.", "form_values": form_values},
                status=400,
            )
        if any(existing.phone == phone for existing in get_designers_for_admin(admin=admin)):
            return render(
                request,
                "admin/designer_signup.html",
                {"form_error": "이미 등록된 디자이너 연락처입니다.", "form_values": form_values},
                status=400,
            )
        if not re.fullmatch(r"\d{4}", pin):
            return render(
                request,
                "admin/designer_signup.html",
                {"form_error": "디자이너 PIN은 4자리 숫자로 입력해 주세요.", "form_values": form_values},
                status=400,
            )
        if pin != pin_confirm:
            return render(
                request,
                "admin/designer_signup.html",
                {"form_error": "디자이너 PIN 확인이 일치하지 않습니다.", "form_values": form_values},
                status=400,
            )

        designer = create_designer_record(
            admin=admin,
            name=name,
            phone=phone,
            pin_hash=make_password(pin),
        )
        invalidate_partner_scope_cache(admin=admin)
        return redirect(f"{reverse('partner_index')}?notice=designer_created")

    return render(request, "admin/designer_signup.html", {"active_shop": admin})


def _designer_management_rows(*, admin, request: HttpRequest) -> list[dict]:
    from django.db.models import Count, Q

    from app.models_model_team import LegacyClient
    from app.services.model_team_bridge import (
        LEGACY_CLIENT_MODEL_COLUMNS,
        get_backend_admin_id,
    )

    designers = get_designers_for_admin(admin=admin)
    if not designers:
        return []

    client_counts_by_designer: dict[int, int] = {}
    if LEGACY_CLIENT_MODEL_COLUMNS.issubset({field.name for field in LegacyClient._meta.get_fields()}):
        legacy_admin_id = get_legacy_admin_id(admin=admin)
        backend_admin_id = get_backend_admin_id(admin=admin)
        scope_filter = Q()
        if legacy_admin_id:
            scope_filter |= Q(shop_id=legacy_admin_id)
        if backend_admin_id is not None:
            scope_filter |= Q(backend_shop_ref_id=backend_admin_id)

        if scope_filter:
            for item in (
                LegacyClient.objects.filter(scope_filter, backend_designer_ref_id__isnull=False)
                .values("backend_designer_ref_id")
                .annotate(unique_client_count=Count("client_id", distinct=True))
            ):
                try:
                    designer_key = int(item.get("backend_designer_ref_id"))
                except (TypeError, ValueError):
                    continue
                client_counts_by_designer[designer_key] = int(item.get("unique_client_count") or 0)

    now = timezone.localtime()
    rows: list[dict] = []
    for designer in designers:
        joined_at = getattr(designer, "created_at", None)
        joined_local = None
        if joined_at is not None:
            if timezone.is_naive(joined_at):
                joined_at = timezone.make_aware(joined_at, timezone.get_current_timezone())
            joined_local = timezone.localtime(joined_at)
        tenure_months = 0
        if joined_local is not None:
            tenure_months = (now.year - joined_local.year) * 12 + (now.month - joined_local.month)
            if now.day < joined_local.day:
                tenure_months -= 1
            tenure_months = max(0, tenure_months)

        lock_state = _get_designer_pin_lock_state(admin=admin, designer_id=str(designer.id), request=request)
        try:
            designer_key = int(designer.id)
        except (TypeError, ValueError):
            designer_key = None
        rows.append(
            {
                "id": designer.id,
                "name": designer.name,
                "phone": designer.phone,
                "customer_count": client_counts_by_designer.get(designer_key, 0) if designer_key is not None else 0,
                "joined_at": joined_local,
                "tenure_months": tenure_months,
                "status_label": "인증 잠김" if lock_state.get("is_locked") else "활성",
                "is_locked": bool(lock_state.get("is_locked")),
            }
        )
    return rows


@never_cache
def designer_management_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin = get_session_admin(request=request)
    if admin is None or get_session_designer(request=request) is not None:
        return redirect("partner_index")
    gate_response = _owner_scope_gate_response(request=request, scope="dashboard")
    if gate_response is not None:
        return gate_response

    designers = _designer_management_rows(admin=admin, request=request)
    return render(
        request,
        "admin/designer_management.html",
        {
            "active_shop": admin,
            "designers": designers,
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
        },
    )


@never_cache
def designer_delete_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin = get_session_admin(request=request)
    if admin is None or get_session_designer(request=request) is not None:
        return redirect("partner_index")
    gate_response = _owner_scope_gate_response(request=request, scope="dashboard")
    if gate_response is not None:
        return gate_response

    if request.method == "POST":
        designer_id = (request.POST.get("designer_id") or "").strip()
        designer = get_designer_for_admin(admin=admin, designer_id=designer_id)
        if designer is None:
            return render(
                request,
                "admin/designer_delete.html",
                {
                    "active_shop": admin,
                    "designers": get_designers_for_admin(admin=admin),
                    "form_error": "삭제할 디자이너를 찾을 수 없습니다.",
                },
                status=400,
            )
        update_designer_active_state(designer=designer, is_active=False)
        invalidate_partner_scope_cache(admin=admin)
        return redirect(f"{reverse('partner_designer_management')}?notice=designer_deleted")

    return render(
        request,
        "admin/designer_delete.html",
        {
            "active_shop": admin,
            "designers": get_designers_for_admin(admin=admin),
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
        },
    )


@never_cache
def designer_unlock_pin_page(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "POST method is required."}, status=405)

    if _has_standalone_customer_session(request=request):
        return JsonResponse({"status": "error", "message": "고객 세션에서는 접근할 수 없습니다."}, status=403)

    admin = get_session_admin(request=request)
    if admin is None or get_session_designer(request=request) is not None:
        return JsonResponse({"status": "error", "message": "관리자 세션이 필요합니다."}, status=401)

    gate_response = _owner_scope_gate_response(request=request, scope="dashboard")
    if gate_response is not None:
        return JsonResponse({"status": "error", "message": "관리자 인증이 필요합니다."}, status=403)

    designer_id = (request.POST.get("designer_id") or "").strip()
    pin = (request.POST.get("pin") or "").strip()
    if not designer_id:
        return JsonResponse({"status": "error", "message": "디자이너를 선택해 주세요."}, status=400)
    if not re.fullmatch(r"\d{4}", pin):
        return JsonResponse({"status": "error", "message": "관리자 보안키 4자리를 입력해 주세요."}, status=400)

    admin_obj = get_admin_by_identifier(identifier=admin.id) or admin
    if not _matches_admin_pin(raw_pin=pin, stored_pin=getattr(admin_obj, "admin_pin", None)):
        return JsonResponse({"status": "error", "message": "관리자 보안키가 일치하지 않습니다."}, status=401)

    try:
        _upgrade_plain_admin_pin_if_needed(request=request, admin_obj=admin_obj, raw_pin=pin)
    except Exception:
        pass

    designer = get_designer_for_admin(admin=admin, designer_id=designer_id)
    if designer is None:
        return JsonResponse({"status": "error", "message": "디자이너 정보를 찾을 수 없습니다."}, status=404)

    _clear_designer_pin_failures(admin=admin, designer_id=designer_id, request=request)
    return JsonResponse(
        {
            "status": "success",
            "message": f"{getattr(designer, 'name', None) or '디자이너'} 잠금이 해제되었습니다.",
            "designer_id": str(designer_id),
        }
    )


@never_cache
def admin_mypage_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin = get_session_admin(request=request)
    if admin is None:
        return redirect("partner_index")

    if request.method == "POST":
        action = (request.POST.get("action") or "change_pin").strip()
        admin_obj = _get_admin_account_for_runtime_admin(admin)
        if admin_obj is None:
            return JsonResponse({"status": "error", "message": "관리자 정보를 찾을 수 없습니다."}, status=404)

        if action == "update_info":
            current_pin = (request.POST.get("current_pin") or "").strip()
            if not _matches_admin_pin(raw_pin=current_pin, stored_pin=admin_obj.admin_pin):
                return JsonResponse({"status": "error", "message": "보안키가 일치하지 않습니다."}, status=401)

            store_name = (request.POST.get("store_name") or "").strip()
            manager_name = (request.POST.get("name") or "").strip()
            phone = (request.POST.get("phone") or "").strip()

            if not store_name or not manager_name or not phone:
                return JsonResponse({"status": "error", "message": "모든 필드를 입력해 주세요."}, status=400)

            updated_fields = []
            if admin_obj.store_name != store_name:
                admin_obj.store_name = store_name
                updated_fields.append("store_name")
            if admin_obj.name != manager_name:
                admin_obj.name = manager_name
                updated_fields.append("name")
            if admin_obj.phone != phone:
                admin_obj.phone = phone
                updated_fields.append("phone")

            if updated_fields:
                admin_obj.save(update_fields=updated_fields)
                _sync_admin_account_state(request=request, admin_obj=admin_obj)
                return JsonResponse({"status": "success", "message": "기본 정보가 성공적으로 수정되었습니다."})
            
            return JsonResponse({"status": "success", "message": "변경사항이 없습니다."})

        if action == "change_password":
            current_password = (request.POST.get("current_password") or "").strip()
            new_password = (request.POST.get("new_password") or "").strip()
            confirm_password = (request.POST.get("new_password_confirm") or "").strip()
            stored_password = (
                (getattr(admin_obj, "password_hash", "") or "").strip()
                or (getattr(admin_obj, "password", "") or "").strip()
            )

            if not current_password:
                return JsonResponse({"status": "error", "message": "현재 비밀번호를 입력해 주세요."}, status=400)
            if not _matches_admin_password(raw_password=current_password, stored_password=stored_password):
                return JsonResponse({"status": "error", "message": "현재 비밀번호가 일치하지 않습니다."}, status=401)

            _upgrade_plain_admin_password_if_needed(request=request, admin_obj=admin_obj, raw_password=current_password)

            if len(new_password) < 8:
                return JsonResponse({"status": "error", "message": "새 비밀번호를 8자 이상 입력해 주세요."}, status=400)
            if new_password != confirm_password:
                return JsonResponse({"status": "error", "message": "새 비밀번호 확인이 일치하지 않습니다."}, status=400)
            if _matches_admin_password(
                raw_password=new_password,
                stored_password=(getattr(admin_obj, "password_hash", "") or "").strip()
                or (getattr(admin_obj, "password", "") or "").strip(),
            ):
                return JsonResponse(
                    {"status": "error", "message": "현재 사용 중인 비밀번호와 다른 비밀번호를 입력해 주세요."},
                    status=400,
                )

            new_password_hash = _hash_admin_password(new_password)
            admin_obj.password_hash = new_password_hash
            if hasattr(admin_obj, "password"):
                admin_obj.password = new_password_hash
                admin_obj.save(update_fields=["password_hash", "password"])
            else:
                admin_obj.save(update_fields=["password_hash"])
            _sync_admin_account_state(request=request, admin_obj=admin_obj)
            allow_owner_mypage(request=request)
            return JsonResponse({"status": "success", "message": "비밀번호가 성공적으로 변경되었습니다."})

        current_pin = (request.POST.get("current_pin") or "").strip()
        if not re.fullmatch(r"\d{4}", current_pin):
            return JsonResponse({"status": "error", "message": "현재 보안키 4자리를 입력해 주세요."}, status=400)

        if not _matches_admin_pin(raw_pin=current_pin, stored_pin=admin_obj.admin_pin):
            return JsonResponse({"status": "error", "message": "현재 보안키가 일치하지 않습니다."}, status=401)

        _upgrade_plain_admin_pin_if_needed(request=request, admin_obj=admin_obj, raw_pin=current_pin)

        if action == "verify_current_pin":
            allow_owner_mypage(request=request)
            return JsonResponse({"status": "success"})

        if action != "change_pin":
            return JsonResponse({"status": "error", "message": "지원하지 않는 요청입니다."}, status=400)

        new_pin = (request.POST.get("admin_pin") or "").strip()
        if not re.fullmatch(r"\d{4}", new_pin):
            return JsonResponse({"status": "error", "message": "새 보안키는 4자리 숫자로 입력해 주세요."}, status=400)

        if _matches_admin_pin(raw_pin=new_pin, stored_pin=admin_obj.admin_pin):
            return JsonResponse(
                {"status": "error", "message": "현재 사용 중인 보안키와 동일합니다. 다른 번호를 입력해 주세요."},
                status=400,
            )

        admin_obj.admin_pin = _hash_admin_pin(new_pin)
        admin_obj.save(update_fields=["admin_pin"])
        _sync_admin_account_state(request=request, admin_obj=admin_obj)
        allow_owner_mypage(request=request)
        return JsonResponse(
            {
                "status": "success",
                "message": "보안키가 성공적으로 변경되었습니다.",
                "is_default_admin_pin": False,
            }
        )

    admin_obj = _get_admin_account_for_runtime_admin(admin)
    return render(
        request,
        "admin/mypage.html",
        {
            "active_shop": admin,
            "is_mypage_owner": can_access_owner_mypage(request=request),
            "is_default_admin_pin": _is_default_admin_pin(getattr(admin_obj, "admin_pin", None)) if admin_obj else False,
        },
    )


@never_cache
def admin_dashboard_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")
    admin = get_session_admin(request=request)
    designer = get_session_designer(request=request)
    if designer is not None:
        return redirect("partner_staff_dashboard")
    if not admin:
        return redirect("partner_index")
    return render(
        request,
        "admin/index.html",
        {
            "is_dashboard": True,
            "admin": admin,
            "active_shop": admin,
            "is_designer_session": False,
            "is_shop_owner": can_access_owner_dashboard(request=request),
        },
    )


@never_cache
def designer_dashboard_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")
    designer = get_session_designer(request=request)
    if designer is None:
        return redirect("partner_index")
    return render(
        request,
        "admin/index.html",
        {
            "is_dashboard": True,
            "admin": designer.shop,
            "active_shop": designer.shop,
            "designer": designer,
            "is_designer_session": True,
            "is_shop_owner": False,
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
        },
    )


@never_cache
def partner_verify(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "POST method is required."}, status=405)

    if _has_standalone_customer_session(request=request):
        return JsonResponse(
            {"status": "error", "message": "고객 세션을 종료한 뒤 파트너 로그인을 진행해 주세요."},
            status=403,
        )

    designer_id = (request.POST.get("designer_id") or "").strip()
    phone = _normalize_phone(request.POST.get("phone", ""))
    business_number = _normalize_business_number(
        request.POST.get("biz_number", "") or request.POST.get("business_number", "")
    )
    password = (request.POST.get("password") or "").strip()
    pin = (request.POST.get("pin") or "").strip()

    if phone and password:
        admin = get_admin_by_phone(phone=phone)
        if not admin or not check_password(password, admin.password_hash):
            return JsonResponse(
                {"status": "error", "message": "연락처 또는 비밀번호를 다시 확인해 주세요."},
                status=401,
            )

        clear_customer_session(request=request)
        clear_designer_session(request=request)
        set_admin_session(request=request, admin=admin)
        # 매장 로그인 성공 시 메인 페이지로 랜딩
        return JsonResponse(
            {
                "status": "success",
                "redirect": "/",
                "session_type": "admin",
                "next_step": "index",
                "shop_id": admin.id,
                "legacy_shop_id": get_legacy_admin_id(admin=admin),
                "store_name": admin.store_name,
            }
        )

    if business_number and password:
        return JsonResponse(
            {"status": "error", "message": "매장 로그인은 관리자 연락처와 비밀번호로 진행해 주세요."},
            status=400,
        )

    if business_number:
        return JsonResponse(
            {"status": "error", "message": "사업자등록번호 대신 관리자 연락처를 입력해 주세요."},
            status=400,
        )

    if designer_id:
        admin = get_session_admin(request=request)
        if admin is None:
            return JsonResponse(
                {"status": "error", "message": "먼저 매장 관리자 로그인을 진행해 주세요."},
                status=401,
            )

        if not re.fullmatch(r"\d{4}", pin):
            return JsonResponse(
                {"status": "error", "message": "PIN 번호 4자리를 입력해 주세요."},
                status=400,
            )

        designer = get_designer_for_admin(admin=admin, designer_id=designer_id)
        if designer is None:
            return JsonResponse(
                {"status": "error", "message": "선택한 디자이너 정보를 찾을 수 없습니다."},
                status=404,
            )

        lock_state = _get_designer_pin_lock_state(admin=admin, designer_id=designer_id, request=request)
        if lock_state["is_locked"]:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "인증번호 입력이 잠겨 있습니다. 관리자에게 확인하세요!",
                    "locked": True,
                    "remaining_lock_seconds": lock_state["remaining_lock_seconds"],
                },
                status=423,
            )

        if not check_password(pin, designer.pin_hash):
            fail_state = _record_designer_pin_failure(admin=admin, designer_id=designer_id, request=request)
            if fail_state["is_locked"]:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": "인증번호 입력이 잠겨 있습니다. 관리자에게 확인하세요!",
                        "locked": True,
                        "remaining_lock_seconds": fail_state["remaining_lock_seconds"],
                    },
                    status=423,
                )
            return JsonResponse(
                {
                    "status": "error",
                    "message": f"PIN 번호를 다시 확인해 주세요. (남은 시도 {fail_state['remaining_attempts']}회)",
                    "locked": False,
                    "remaining_attempts": fail_state["remaining_attempts"],
                },
                status=401,
            )

        _clear_designer_pin_failures(admin=admin, designer_id=designer_id, request=request)
        clear_customer_session(request=request)
        active_shop = getattr(designer, "shop", None) or admin
        set_admin_session(request=request, admin=active_shop)
        set_designer_session(request=request, designer=designer)
        revoke_owner_dashboard(request=request)
        return JsonResponse(
            {
                "status": "success",
                "redirect": "/partner/staff/",
                "session_type": "designer",
                "shop_id": active_shop.id,
                "designer_id": designer.id,
                "legacy_shop_id": get_legacy_admin_id(admin=active_shop),
                "legacy_designer_id": get_legacy_designer_id(designer=designer),
            }
        )

    if pin:
        return JsonResponse(
            {"status": "error", "message": "매장 로그인 후 디자이너를 선택하고 PIN을 입력해 주세요."},
            status=400,
        )

    return JsonResponse(
        {"status": "error", "message": "연락처와 비밀번호를 입력해 주세요."},
        status=400,
    )


@never_cache
def partner_select_designer(request):
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "POST method is required."}, status=405)

    admin = get_session_admin(request=request)
    if admin is None:
        return JsonResponse({"status": "error", "message": "매장 관리자 로그인이 필요합니다."}, status=401)

    designer_id = (request.POST.get("designer_id") or "").strip()
    if not designer_id:
        return JsonResponse({"status": "error", "message": "디자이너를 선택해 주세요."}, status=400)

    designer = get_designer_for_admin(admin=admin, designer_id=designer_id)
    if designer is None:
        return JsonResponse({"status": "error", "message": "선택한 디자이너 정보를 찾을 수 없습니다."}, status=404)

    # PIN 없이 세션 설정
    set_designer_session(request=request, designer=designer)
    revoke_designer_dashboard(request=request)

    return JsonResponse({
        "status": "success",
        "designer_id": designer.id,
        "designer_name": designer.name
    })


@never_cache
def partner_designer_list(request):
    if _has_standalone_customer_session(request=request):
        return JsonResponse(
            {"status": "error", "message": "고객 세션에서는 파트너 기능에 접근할 수 없습니다."},
            status=403,
        )
    admin = get_session_admin(request=request)
    if admin is None:
        return JsonResponse({"status": "error", "message": "매장 관리자 로그인을 진행해 주세요."}, status=401)
    if get_session_designer(request=request) is not None:
        return JsonResponse(
            {"status": "error", "message": "디자이너 세션에서는 매장 관리자 기능에 접근할 수 없습니다."},
            status=403,
        )

    cache_key = build_partner_cache_key("partner-designers", admin=admin)
    cached_payload = get_cached_payload(cache_key)
    if cached_payload is not None:
        return JsonResponse(cached_payload, safe=False)

    designers = [
        {
            "id": designer.id,
            "legacy_id": get_legacy_designer_id(designer=designer),
            "name": designer.name,
            "phone": designer.phone,
            "profile_image": None,
        }
        for designer in get_designers_for_admin(admin=admin)
    ]
    set_cached_payload(
        cache_key,
        designers,
        timeout=cache_timeout("PARTNER_LOOKUP_CACHE_SECONDS", 45),
    )
    return JsonResponse(designers, safe=False)


@never_cache
def enter_partner_dashboard(request):
    if _has_standalone_customer_session(request=request):
        return _render_partner_login(
            request,
            error_message="고객 로그인 상태에서는 파트너 대시보드에 접근할 수 없습니다. 먼저 로그아웃해 주세요.",
            status=403,
        )

    admin = get_session_admin(request=request)
    if admin is None:
        if request.method == "POST":
            return JsonResponse({"status": "error", "message": "먼저 매장 관리자 로그인을 진행해 주세요."}, status=401)
        return redirect("partner_index")
    if get_session_designer(request=request) is not None:
        if request.method == "POST":
            return JsonResponse({"status": "error", "message": "디자이너 로그인 상태에서는 매장 전체 대시보드에 접근할 수 없습니다."}, status=403)
        return redirect("partner_staff_dashboard")

    if request.method != "POST":
        return redirect("partner_index")

    scope = (request.POST.get("scope") or "dashboard").strip().lower()
    if scope not in {"dashboard", "mypage"}:
        return JsonResponse({"status": "error", "message": "유효하지 않은 접근 범위입니다."}, status=400)

    pin = (request.POST.get("pin") or request.POST.get("password") or "").strip()
    if not pin:
        return JsonResponse({"status": "error", "message": "관리자 보안키를 입력해 주세요."}, status=400)

    admin_obj = _get_admin_account_for_runtime_admin(admin)
    stored_pin = getattr(admin_obj or admin, "admin_pin", None)
    if not _matches_admin_pin(raw_pin=pin, stored_pin=stored_pin):
        return JsonResponse({"status": "error", "message": "보안키가 일치하지 않습니다."}, status=401)

    if admin_obj is not None:
        _upgrade_plain_admin_pin_if_needed(request=request, admin_obj=admin_obj, raw_pin=pin)

    # PIN 인증 성공 시 dashboard + mypage 모두 허용
    # → 파트너센터 PIN으로 내 페이지 접근 가능 (재인증 불필요)
    allow_owner_dashboard(request=request)
    allow_owner_mypage(request=request)
    if scope == "mypage":
        redirect_url = "/partner/mypage/"
    else:
        redirect_url = "/partner/dashboard/"
    return JsonResponse({"status": "success", "redirect": redirect_url})


@never_cache
def customer_logout_page(request):
    clear_customer_session(request=request)
    return redirect("index")


@never_cache
def designer_logout_page(request):
    clear_designer_session(request=request)
    if get_session_admin(request=request) is not None:
        return redirect("partner_designer_select")
    return redirect("partner_index")


@never_cache
def logout_page(request):
    clear_customer_session(request=request)
    clear_admin_session(request=request)
    clear_designer_session(request=request)
    return redirect("index")


def page_not_found_view(request, exception):
    return render(request, "errors/error.html", {"error_code": "404"}, status=404)


def server_error_view(request):
    return render(request, "errors/error.html", {"error_code": "500"}, status=500)
