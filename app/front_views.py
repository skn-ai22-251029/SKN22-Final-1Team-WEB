from __future__ import annotations

import re
from typing import TYPE_CHECKING

from django.contrib.auth.hashers import check_password, make_password
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
from app.services.runtime_client import RuntimeClient as Client
from app.session_state import (
    allow_owner_dashboard,
    can_access_owner_dashboard,
    clear_admin_session,
    clear_customer_session,
    clear_designer_session,
    get_session_admin,
    get_session_customer,
    get_session_designer,
    revoke_owner_dashboard,
    set_admin_session,
    set_customer_session,
    set_designer_session,
)

if TYPE_CHECKING:
    from app.models_django import AdminAccount, Designer


def _normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _normalize_business_number(value: str) -> str:
    return re.sub(r"\D", "", value or "")


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


def home_page(request):
    return render(request, "index.html", {"start_url": "/customer/", "partner_url": "/partner/login/"})


def terms_page(request):
    return render(request, "pages/terms.html")


def privacy_policy_page(request):
    return render(request, "pages/privacy_policy.html")


@never_cache
def client_login_page(request):
    if request.method == "GET" and get_session_customer(request=request) is not None:
        return redirect("customer_resume")
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
    return render(
        request,
        "customer/menu.html",
        {
            "client": client,
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
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
    return render(
        request,
        "customer/history.html",
        {
            "client": client,
            "history_items": payload.get("items", []),
            "history_message": payload.get("message"),
            "popup_message": _popup_message_from_notice(request.GET.get("notice")),
        },
    )


@never_cache
def client_trend_page(request):
    client = get_session_customer(request=request)
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
    target = reverse(_customer_resume_route_for_client(client=client))
    notice = (request.GET.get("notice") or "").strip()
    if notice:
        return redirect(f"{target}?notice={notice}")
    return redirect(target)


@never_cache
def partner_designer_select_page(request: HttpRequest):
    admin = get_session_admin(request=request)
    if admin is None:
        login_url = reverse("partner_login")
        query = urlencode({"next": request.get_full_path()})
        return redirect(f"{login_url}?{query}")
    
    # 고객 세션이 있는 경우 템플릿으로 전달
    client = get_session_customer(request=request)
    return render(request, "admin/designer_select.html", {"client": client})


@never_cache
def customer_reanalysis_start_page(request, pk: int):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin, designer = _resolve_active_shop_and_designer(request=request)
    if admin is None:
        return redirect("partner_index")

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
        return redirect("partner_dashboard")

    return render(request, "admin/signup.html")


@never_cache
def designer_signup_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin = get_session_admin(request=request)
    if admin is None or get_session_designer(request=request) is not None:
        return redirect("partner_index")

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


@never_cache
def designer_management_page(request):
    if _has_standalone_customer_session(request=request):
        return redirect(f"{reverse('customer_resume')}?notice=partner_forbidden_customer")

    admin = get_session_admin(request=request)
    if admin is None or get_session_designer(request=request) is not None:
        return redirect("partner_index")

    designers = get_designers_for_admin(admin=admin)
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
        # 매장 로그인 성공 시 대시보드 기본 접근은 허용하되, 
        # 디자이너 관리 등 민감 페이지는 별도 비밀번호 확인 절차를 거침
        return JsonResponse(
            {
                "status": "success",
                "redirect": "/partner/dashboard/",
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
        if not check_password(pin, designer.pin_hash):
            return JsonResponse(
                {"status": "error", "message": "PIN 번호를 다시 확인해 주세요."},
                status=401,
            )

        clear_customer_session(request=request)
        set_admin_session(request=request, admin=designer.shop)
        set_designer_session(request=request, designer=designer)
        revoke_owner_dashboard(request=request)
        return JsonResponse(
            {
                "status": "success",
                "redirect": "/partner/staff/",
                "session_type": "designer",
                "shop_id": designer.shop_id,
                "designer_id": designer.id,
                "legacy_shop_id": get_legacy_admin_id(admin=designer.shop),
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

    password = (request.POST.get("password") or "").strip()
    if not password:
        return JsonResponse({"status": "error", "message": "매장 전체 대시보드 접근을 위해 비밀번호를 다시 입력해 주세요."}, status=400)
    if not check_password(password, admin.password_hash):
        return JsonResponse({"status": "error", "message": "비밀번호를 다시 확인해 주세요."}, status=401)

    allow_owner_dashboard(request=request)
    return JsonResponse({"status": "success", "redirect": "/partner/designers/"})


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
