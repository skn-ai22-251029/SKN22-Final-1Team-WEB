from typing import TYPE_CHECKING

from django.http import Http404
import logging
import re

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema

from app.api.v1.admin_auth import AdminTokenAuthentication, IsAuthenticatedAdmin, refresh_admin_access_token
from app.api.v1.response_helpers import CompatEnvelopeAPIView, detail_response
from app.api.v1.admin_serializers import (
    AdminLoginSerializer,
    AdminRegisterSerializer,
    AdminTrendFilterSerializer,
    ConsultationCloseSerializer,
    ConsultationNoteCreateSerializer,
    CustomerProfileNoteUpsertSerializer,
    DesignerDiagnosisCardUpsertSerializer,
    ChatbotAskSerializer,
    RefreshTokenSerializer,
    DesignerSerializer,
)
from app.api.v1.admin_services import (
    _scoped_client_ids,
    assign_client_to_designer,
    close_consultation_session,
    create_client_note,
    get_active_client_sessions,
    get_client_customer_note,
    get_client_designer_diagnosis,
    get_admin_profile,
    get_admin_dashboard_summary,
    get_legacy_dashboard_trend_report,
    get_admin_trend_report,
    get_all_clients,
    get_client_detail,
    get_client_history_detail,
    get_client_recommendation_report,
    upsert_client_designer_diagnosis,
    get_style_report,
    login_admin,
    register_admin,
    upsert_client_customer_note,
)
from app.session_state import get_session_admin, get_session_designer, set_admin_session
from app.services.ai_facade import get_ai_health
from app.services.chatbot.service import build_admin_chatbot_reply, get_chatbot_backend_status
from app.services.model_team_bridge import (
    get_admin_by_identifier,
    get_client_by_identifier,
    get_designers_for_admin,
    get_legacy_admin_id,
    get_legacy_designer_id,
)

if TYPE_CHECKING:
    from app.models_django import AdminAccount, Designer


logger = logging.getLogger(__name__)


def _get_client_or_404(identifier):
    client = get_client_by_identifier(identifier=identifier)
    if client is None:
        raise Http404("Client not found.")
    return client


def _build_admin_register_errors(message: str) -> dict[str, list[str]]:
    lowered = message.lower()
    if "phone number" in lowered or "연락처" in message:
        return {"phone": [message]}
    if "business registration number" in lowered or "사업자등록번호" in message:
        return {"business_number": [message]}
    return {"non_field_errors": [message]}


def _build_admin_login_errors(message: str) -> dict[str, list[str]]:
    return {"non_field_errors": [message]}


def _resolve_request_admin(request) -> "AdminAccount | None":
    user = getattr(request, "user", None)
    if getattr(user, "role", None) in {"owner", "manager", "staff"}:
        admin = get_admin_by_identifier(identifier=getattr(user, "id", None))
        if admin is not None:
            return admin
    return get_session_admin(request=request)


def _resolve_request_designer(request) -> "Designer | None":
    return get_session_designer(request=request)


def _resolve_request_staff(request) -> tuple["AdminAccount | None", "Designer | None"]:
    admin = _resolve_request_admin(request)
    designer = _resolve_request_designer(request)
    if admin is None and designer is not None:
        admin = designer.shop
    return admin, designer


def _resolve_payload_admin(payload) -> "AdminAccount | None":
    if not isinstance(payload, dict):
        return None
    admin_payload = payload.get("admin") if isinstance(payload.get("admin"), dict) else {}
    identifier = (
        payload.get("legacy_admin_id")
        or admin_payload.get("legacy_admin_id")
        or payload.get("admin_id")
        or admin_payload.get("admin_id")
    )
    return get_admin_by_identifier(identifier=identifier)


def _legacy_staff_required(request):
    admin, designer = _resolve_request_staff(request)
    if admin is None:
        return None, detail_response("Admin login is required.", status_code=status.HTTP_401_UNAUTHORIZED)
    return (admin, designer), None


def _truthy_query_param(request, key: str) -> bool:
    value = request.query_params.get(key)
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _history_limit_from_request(request, default: int = 20, max_limit: int = 100) -> int:
    raw_value = request.query_params.get("history_limit")
    if raw_value in (None, ""):
        return default
    try:
        limit = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, max_limit))


def _build_legacy_capture_preview_rows(payload: dict) -> list[dict]:
    return [
        {
            "processed_path": (
                row.get("processed_image_url")
                or row.get("deidentified_image_url")
                or row.get("original_image_url")
            ),
            "created_at": row.get("created_at"),
        }
        for row in payload["capture_history"]
    ]


def _build_legacy_customer_detail_payload(payload: dict) -> dict:
    return {
        "id": payload["client"]["client_id"],
        "legacy_client_id": payload["client"].get("legacy_client_id"),
        "name": payload["client"]["name"],
        "phone": payload["client"]["phone"],
        "survey": payload.get("latest_survey"),
        "face_analyses": payload["analysis_history"],
        "reanalysis": payload.get("reanalysis"),
        "designer_diagnosis": payload.get("designer_diagnosis"),
        "session_status": payload.get("session_status"),
        "customer_note": payload.get("customer_note"),
        "active_consultation": payload.get("active_consultation"),
        "notes": payload.get("notes", []),
        "captures": _build_legacy_capture_preview_rows(payload),
        "history": payload.get("history"),
    }


def _build_legacy_customer_history_payload(payload: dict) -> dict:
    return {
        "id": payload["client"]["client_id"],
        "legacy_client_id": payload["client"].get("legacy_client_id"),
        "analysis_history": payload["analysis_history"],
        "face_analyses": payload["analysis_history"],
        "capture_history": payload["capture_history"],
        "captures": _build_legacy_capture_preview_rows(payload),
        "style_selection_history": payload["style_selection_history"],
        "chosen_recommendation_history": payload["chosen_recommendation_history"],
        "notes": payload.get("notes", []),
        "history": payload.get("history"),
    }


def _legacy_shop_required(
    request,
    *,
    designer_message: str = "디자이너 세션에서는 고객 배정을 변경할 수 없습니다.",
):
    staff, error_response = _legacy_staff_required(request)
    if error_response:
        return None, error_response
    admin, designer = staff
    if designer is not None:
        return None, detail_response(
            designer_message,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return admin, None


class AdminProtectedAPIView(CompatEnvelopeAPIView):
    authentication_classes = [AdminTokenAuthentication]
    permission_classes = [IsAuthenticatedAdmin]


class AdminRegisterView(CompatEnvelopeAPIView):
    @extend_schema(summary="Register admin", request=AdminRegisterSerializer, responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = AdminRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = register_admin(payload=serializer.validated_data)
        except ValueError as exc:
            message = str(exc)
            return detail_response(
                message,
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="validation_error",
                errors=_build_admin_register_errors(message),
            )
        admin = _resolve_payload_admin(payload)
        if admin is not None:
            set_admin_session(request=request, admin=admin)
            payload["redirect"] = "/"
            payload["session_type"] = "admin"
        return Response(payload, status=status.HTTP_201_CREATED)


class AdminLoginView(CompatEnvelopeAPIView):
    @extend_schema(summary="Login admin", request=AdminLoginSerializer, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = AdminLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = login_admin(**serializer.validated_data)
        except ValueError as exc:
            message = str(exc)
            return detail_response(
                message,
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="validation_error",
                errors=_build_admin_login_errors(message),
            )
        admin = _resolve_payload_admin(payload)
        if admin is not None:
            set_admin_session(request=request, admin=admin)
            payload["redirect"] = "/"
            payload["session_type"] = "admin"
        return Response(payload)


class AdminRefreshView(CompatEnvelopeAPIView):
    @extend_schema(summary="Refresh admin token", request=RefreshTokenSerializer, responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = RefreshTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = refresh_admin_access_token(refresh_token=serializer.validated_data["refresh_token"])
        except Exception as exc:
            logger.warning("[admin_refresh_failed] reason=%s", exc)
            return detail_response(str(exc), status_code=status.HTTP_401_UNAUTHORIZED)
        return Response(payload)


class AdminProfileView(AdminProtectedAPIView):
    @extend_schema(summary="Get current admin profile", responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(get_admin_profile(admin=request.user))


class AdminDashboardView(AdminProtectedAPIView):
    @extend_schema(summary="Admin dashboard summary", responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(get_admin_dashboard_summary(admin=request.user))


class ActiveClientSessionsView(AdminProtectedAPIView):
    @extend_schema(summary="Active client sessions", responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(get_active_client_sessions(admin=request.user))


class AllClientsView(AdminProtectedAPIView):
    @extend_schema(
        summary="All clients for admin",
        parameters=[OpenApiParameter("q", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False)],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        return Response(get_all_clients(query=request.query_params.get("q", ""), admin=request.user))


class LegacyAllClientsView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Legacy customer list for template dashboard",
        parameters=[OpenApiParameter("q", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False)],
        responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        payload = get_all_clients(query=request.query_params.get("q", ""), admin=admin, designer=designer)
        items = [
            {
                "id": item["client_id"],
                "client_id": item["client_id"],
                "legacy_client_id": item.get("legacy_client_id"),
                "name": item["name"],
                "phone": item["phone"],
                "created_at": item["created_at"],
                "last_visit_date": item.get("last_visit_date"),
                "visit_count": item.get("visit_count", 0),
                "designer_id": item["designer_id"],
                "legacy_designer_id": item.get("legacy_designer_id"),
                "designer_name": item["designer_name"],
                "assigned_at": item["assigned_at"],
                "assignment_source": item["assignment_source"],
                "is_assignment_pending": item["is_assignment_pending"],
                "has_active_consultation": item.get("has_active_consultation", False),
                "session_active": item.get("session_active", False),
                "can_write_designer_diagnosis": item.get("can_write_designer_diagnosis", False),
                "has_survey_completed": item.get("has_survey_completed", False),
                "has_photo_captured": item.get("has_photo_captured", False),
                "has_consultation_requested": item.get("has_consultation_requested", False),
            }
            for item in payload["items"]
        ]
        return Response(items)


class AdminClientDetailView(AdminProtectedAPIView):
    @extend_schema(
        summary="Admin client detail",
        parameters=[OpenApiParameter("client_id", OpenApiTypes.STR, OpenApiParameter.QUERY, required=True)],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        client = _get_client_or_404(request.query_params.get("client_id"))
        include_history = _truthy_query_param(request, "include_history")
        history_limit = _history_limit_from_request(request)
        try:
            return Response(
                get_client_detail(
                    client=client,
                    admin=request.user,
                    include_history=include_history,
                    history_limit=history_limit,
                )
            )
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_404_NOT_FOUND)


class AdminClientHistoryView(AdminProtectedAPIView):
    @extend_schema(
        summary="Admin client history detail",
        parameters=[
            OpenApiParameter("client_id", OpenApiTypes.STR, OpenApiParameter.QUERY, required=True),
            OpenApiParameter("history_limit", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        client = _get_client_or_404(request.query_params.get("client_id"))
        history_limit = _history_limit_from_request(request)
        try:
            payload = get_client_history_detail(
                client=client,
                admin=request.user,
                history_limit=history_limit,
            )
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_404_NOT_FOUND)
        return Response(payload)


class LegacyAdminClientDetailView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Legacy customer detail for template dashboard",
        responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        try:
            client = _get_client_or_404(pk)
            include_history = _truthy_query_param(request, "include_history")
            history_limit = _history_limit_from_request(request)
            
            payload = get_client_detail(
                client=client,
                admin=admin,
                designer=designer,
                include_history=include_history,
                history_limit=history_limit,
            )
            return Response(_build_legacy_customer_detail_payload(payload))
        except Exception as e:
            logger.error(f"[legacy_client_detail_failed] pk={pk} error={str(e)}", exc_info=True)
            return detail_response(
                f"고객 상세 정보를 불러오는 중 서버 오류가 발생했습니다: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class LegacyAdminClientHistoryView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Legacy customer history for template dashboard",
        parameters=[OpenApiParameter("history_limit", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False)],
        responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        client = _get_client_or_404(pk)
        history_limit = _history_limit_from_request(request)
        try:
            payload = get_client_history_detail(
                client=client,
                admin=admin,
                designer=designer,
                history_limit=history_limit,
            )
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_404_NOT_FOUND)

        return Response(_build_legacy_customer_history_payload(payload))


class LegacyDesignerDiagnosisCardView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Legacy designer diagnosis card for template dashboard",
        responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        client = _get_client_or_404(pk)
        try:
            payload = get_client_designer_diagnosis(client=client, admin=admin, designer=designer)
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_404_NOT_FOUND)
        return Response(payload)

    @extend_schema(
        summary="Save designer diagnosis card for the active shop session",
        request=DesignerDiagnosisCardUpsertSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        serializer = DesignerDiagnosisCardUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        client = _get_client_or_404(pk)
        try:
            payload = upsert_client_designer_diagnosis(
                client=client,
                diagnosis_state=serializer.validated_data,
                admin=admin,
                designer=designer,
            )
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class LegacyConsultationNoteCreateView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Save consultation note for the active shop session",
        request=ConsultationNoteCreateSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        payload = dict(request.data)
        payload["client_id"] = pk
        serializer = ConsultationNoteCreateSerializer(data=payload)
        serializer.is_valid(raise_exception=True)

        client = _get_client_or_404(pk)
        try:
            response_payload = create_client_note(
                client=client,
                consultation_id=serializer.validated_data["consultation_id"],
                content=serializer.validated_data["content"],
                admin=admin,
                designer=designer,
            )
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
        return Response(response_payload)


class LegacyConsultationCloseView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Close the active consultation session from the template customer detail view",
        request=ConsultationCloseSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        serializer = ConsultationCloseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        client = _get_client_or_404(pk)
        try:
            payload = close_consultation_session(
                consultation_id=serializer.validated_data["consultation_id"],
                client=client,
                admin=admin,
                designer=designer,
            )
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class LegacyCustomerProfileNoteView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Customer-level note for the active shop session",
        responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, pk):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        client = _get_client_or_404(pk)
        try:
            payload = get_client_customer_note(client=client, admin=admin, designer=designer)
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_404_NOT_FOUND)
        return Response(payload)

    @extend_schema(
        summary="Save customer-level note for the active shop session",
        request=CustomerProfileNoteUpsertSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        serializer = CustomerProfileNoteUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        client = _get_client_or_404(pk)
        try:
            payload = upsert_client_customer_note(
                client=client,
                content=serializer.validated_data.get("content", ""),
                admin=admin,
                designer=designer,
            )
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class LegacyAdminClientAssignView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Assign a customer to a designer for the active shop session",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        admin, error_response = _legacy_shop_required(request)
        if error_response:
            return error_response

        designer_id = request.data.get("designer_id")
        if designer_id in (None, ""):
            return detail_response("디자이너를 선택해 주세요.", status_code=status.HTTP_400_BAD_REQUEST)

        try:
            designer_id = int(designer_id)
        except (TypeError, ValueError):
            return detail_response("디자이너 정보가 올바르지 않습니다.", status_code=status.HTTP_400_BAD_REQUEST)

        client = _get_client_or_404(pk)
        scoped_ids = set(_scoped_client_ids(admin=admin))
        if client.id not in scoped_ids:
            return detail_response("현재 매장 범위를 벗어난 고객입니다.", status_code=status.HTTP_404_NOT_FOUND)

        try:
            payload = assign_client_to_designer(client=client, designer_id=designer_id, admin=admin)
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class AdminClientRecommendationView(AdminProtectedAPIView):
    @extend_schema(
        summary="Admin client recommendation report",
        parameters=[OpenApiParameter("client_id", OpenApiTypes.STR, OpenApiParameter.QUERY, required=True)],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        client = _get_client_or_404(request.query_params.get("client_id"))
        try:
            return Response(get_client_recommendation_report(client=client, admin=request.user))
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_404_NOT_FOUND)


class ConsultationNoteView(AdminProtectedAPIView):
    @extend_schema(summary="Create client consultation note", request=ConsultationNoteCreateSerializer, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = ConsultationNoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        client = _get_client_or_404(serializer.validated_data["client_id"])
        try:
            payload = create_client_note(
                client=client,
                consultation_id=serializer.validated_data["consultation_id"],
                content=serializer.validated_data["content"],
                admin=request.user,
            )
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class ConsultationCloseView(AdminProtectedAPIView):
    @extend_schema(summary="Close consultation session", request=ConsultationCloseSerializer, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = ConsultationCloseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = close_consultation_session(consultation_id=serializer.validated_data["consultation_id"], admin=request.user)
        except ValueError as exc:
            return detail_response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class AdminTrendReportView(AdminProtectedAPIView):
    @extend_schema(
        summary="Admin weekly trend report",
        parameters=[
            OpenApiParameter("days", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("target_length", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("target_vibe", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("scalp_type", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("hair_colour", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("budget_range", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("age_decade", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("age_segment", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("age_group", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        serializer = AdminTrendFilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        days = data.pop("days", 7)
        logger.info(
            "[admin_trend_report_request] admin_id=%s days=%s filters=%s",
            request.user.id,
            days,
            data,
        )
        return Response(get_admin_trend_report(days=days, filters=data, admin=request.user))


class LegacyAdminTrendReportView(CompatEnvelopeAPIView):
    @extend_schema(summary="Legacy trend report for template dashboard", responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT})
    def get(self, request):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, designer = staff

        days = int(request.query_params.get("days", 7))
        return Response(
            get_legacy_dashboard_trend_report(
                days=days,
                admin=admin,
                designer=designer,
            )
        )


class StyleReportView(AdminProtectedAPIView):
    @extend_schema(
        summary="Style report for admin",
        parameters=[
            OpenApiParameter("style_id", OpenApiTypes.INT, OpenApiParameter.QUERY, required=True),
            OpenApiParameter("days", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        style_id = int(request.query_params.get("style_id"))
        days = int(request.query_params.get("days", 7))
        logger.info(
            "[admin_style_report_request] admin_id=%s style_id=%s days=%s",
            request.user.id,
            style_id,
            days,
        )
        return Response(get_style_report(style_id=style_id, days=days, admin=request.user))


class AdminChatbotAskView(CompatEnvelopeAPIView):
    authentication_classes = [AdminTokenAuthentication]

    @extend_schema(
        summary="Ask admin chatbot for styling guidance",
        request=ChatbotAskSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        admin, designer = _resolve_request_staff(request)
        if admin is None:
            return detail_response("Admin login is required.", status_code=status.HTTP_401_UNAUTHORIZED)

        serializer = ChatbotAskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = build_admin_chatbot_reply(
            message=serializer.validated_data["message"],
            admin_name=(designer.name if designer is not None else admin.name),
            store_name=admin.store_name,
            conversation_history=serializer.validated_data.get("conversation_history") or [],
        )
        logger.info(
            "[admin_chatbot_reply] admin_id=%s designer_id=%s store_name=%s message=%s",
            admin.id,
            (designer.id if designer is not None else None),
            admin.store_name,
            serializer.validated_data["message"][:120],
        )
        payload["actor_type"] = "designer" if designer is not None else "admin"
        payload["designer_id"] = designer.id if designer is not None else None
        payload["legacy_admin_id"] = get_legacy_admin_id(admin=admin)
        payload["legacy_designer_id"] = (
            get_legacy_designer_id(designer=designer) if designer is not None else None
        )
        return Response(payload)


class AdminAiHealthView(CompatEnvelopeAPIView):
    authentication_classes = [AdminTokenAuthentication]

    @extend_schema(
        summary="Get AI backend health and chatbot routing status",
        responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        admin, designer = _resolve_request_staff(request)
        if admin is None:
            return detail_response("Admin login is required.", status_code=status.HTTP_401_UNAUTHORIZED)

        return Response(
            {
                "status": "ready",
                "checked_at": timezone.now(),
                "actor_type": "designer" if designer is not None else "admin",
                "admin_id": admin.id,
                "legacy_admin_id": get_legacy_admin_id(admin=admin),
                "designer_id": designer.id if designer is not None else None,
                "legacy_designer_id": (
                    get_legacy_designer_id(designer=designer) if designer is not None else None
                ),
                "ai_engine": get_ai_health(),
                "chatbot_backend": get_chatbot_backend_status(),
            }
        )


class DesignerPinChangeView(CompatEnvelopeAPIView):
    @extend_schema(
        summary="Change current designer's PIN",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        from django.contrib.auth.hashers import make_password
        import uuid

        # 1. 관리자 및 디자이너 세션 정보 획득
        admin = get_session_admin(request=request)
        designer = get_session_designer(request=request)

        # 관리자 세션이 없더라도, 디자이너 세션이 있다면 해당 디자이너가 소속된 샵을 신뢰함
        if not admin and designer:
            admin = designer.shop

        if not admin:
            return detail_response("매장 관리자 로그인이 필요합니다.", status_code=status.HTTP_401_UNAUTHORIZED)

        if not designer:
            return detail_response("디자이너 세션이 유효하지 않습니다. 다시 로그인해 주세요.", status_code=status.HTTP_401_UNAUTHORIZED)

        # 2. 새로운 PIN 데이터 획득
        new_pin = (request.data.get("new_pin") or request.POST.get("new_pin") or "").strip()
        if not re.fullmatch(r"\d{4}", new_pin):
            return detail_response("보안키는 4자리 숫자로 입력해 주세요.", status_code=status.HTTP_400_BAD_REQUEST)

        try:
            from app.models_django import Designer
            import uuid
            designer_obj = None

            # 세션에서 가져온 ID(문자열)를 UUID로 안전하게 변환하여 조회 시도
            try:
                d_uuid = uuid.UUID(str(designer.id))
                designer_obj = Designer.objects.get(id=d_uuid)
            except (ValueError, Designer.DoesNotExist):
                # UUID 조회 실패 시 backend_designer_id로 2차 조회 (정수형 지원)
                try:
                    designer_obj = Designer.objects.get(backend_designer_id=designer.id)
                except (Designer.DoesNotExist, ValueError):
                    return detail_response("디자이너 정보를 찾을 수 없습니다.", status_code=status.HTTP_404_NOT_FOUND)

            # 3. 보안키 동일 여부 체크
            from django.contrib.auth.hashers import check_password
            if check_password(new_pin, designer_obj.pin_hash):
                return detail_response("현재 사용 중인 보안키와 동일합니다. 다른 번호를 입력해 주세요.", status_code=status.HTTP_400_BAD_REQUEST)

            # 4. 보안키 해싱 및 저장
            designer_obj.pin_hash = make_password(new_pin)
            designer_obj.save()

            # 5. 성공 응답
            return Response({
                "status": "success",
                "message": "디자이너 보안키가 성공적으로 변경되었습니다."
            })
        except Exception as e:
            logger.error(f"[designer_pin_change_failed] designer_id={designer.id} error={str(e)}")
            return detail_response("보안키 변경 중 서버 오류가 발생했습니다.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DesignerListView(CompatEnvelopeAPIView):
    @extend_schema(summary="List designers for the current shop session", responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT})
    def get(self, request):
        staff, error_response = _legacy_staff_required(request)
        if error_response:
            return error_response
        admin, _ = staff
        designers = get_designers_for_admin(admin=admin)
        serializer = DesignerSerializer(designers, many=True)
        return Response(serializer.data)
