import io
import threading

from django.conf import settings
from django.shortcuts import get_object_or_404
from PIL import Image, ImageOps
from rest_framework import parsers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema

from app.api.v1.django_serializers import (
    ClientCheckSerializer,
    ClientRegisterSerializer,
    RecommendationListResponseSerializer,
    SurveySerializer,
)
from app.api.v1.services_django import (
    cancel_style_selection,
    confirm_style_selection,
    get_current_recommendations,
    get_former_recommendations,
    get_trend_recommendations,
    run_mirrai_analysis_pipeline,
    serialize_capture_status,
    upsert_survey,
)
from app.models_django import CaptureRecord, Client
from app.services.age_profile import build_client_age_profile
from app.services.capture_validation import sanitize_original_upload, validate_capture_image
from app.services.face_processing import build_deidentified_capture, extract_landmark_snapshot
from app.services.storage_service import store_capture_assets


class LoginView(APIView):
    @extend_schema(
        summary="Log in client",
        request={
            "application/json": {
                "type": "object",
                "properties": {"phone": {"type": "string", "example": "010-9999-8888"}},
            }
        },
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        phone = request.data.get("phone", "").replace("-", "").strip()
        if not phone:
            return Response({"detail": "Phone number is required."}, status=status.HTTP_400_BAD_REQUEST)

        client = Client.objects.filter(phone=phone).first()
        if not client:
            return Response({"detail": "Client not found."}, status=status.HTTP_404_NOT_FOUND)

        age_profile = build_client_age_profile(client) or {}
        return Response(
            {
                "access_token": f"mock-token-{client.id}",
                "token_type": "bearer",
                "client_id": client.id,
                "age": age_profile.get("current_age"),
                "age_decade": age_profile.get("age_decade"),
                "age_segment": age_profile.get("age_segment"),
                "age_group": age_profile.get("age_group"),
            }
        )


class ClientCheckView(APIView):
    @extend_schema(summary="Check existing client", request=ClientCheckSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        phone = request.data.get("phone", "").replace("-", "").strip()
        client = Client.objects.filter(phone=phone).first()
        if not client:
            return Response({"is_existing": False})

        age_profile = build_client_age_profile(client) or {}
        return Response(
            {
                "is_existing": True,
                "name": client.name,
                "gender": client.gender,
                "client_id": client.id,
                "age": age_profile.get("current_age"),
                "age_decade": age_profile.get("age_decade"),
                "age_segment": age_profile.get("age_segment"),
                "age_group": age_profile.get("age_group"),
            }
        )


class RegisterView(APIView):
    @extend_schema(summary="Register new client", request=ClientRegisterSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request):
        phone = request.data.get("phone", "").replace("-", "").strip()
        if Client.objects.filter(phone=phone).exists():
            return Response({"detail": "This phone number is already registered."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ClientRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        client = serializer.save(phone=phone)

        age_profile = build_client_age_profile(client) or {}
        return Response(
            {
                "status": "success",
                "client_id": client.id,
                "access_token": f"mock-token-{client.id}",
                "token_type": "bearer",
                "age": age_profile.get("current_age"),
                "age_decade": age_profile.get("age_decade"),
                "age_segment": age_profile.get("age_segment"),
                "age_group": age_profile.get("age_group"),
            },
            status=status.HTTP_201_CREATED,
        )


class SurveyView(APIView):
    @extend_schema(summary="Submit client survey", request=SurveySerializer, responses={200: SurveySerializer})
    def post(self, request):
        client_id = request.data.get("client") or request.data.get("client_id")
        client = get_object_or_404(Client, id=client_id)
        survey = upsert_survey(client, request.data)
        return Response(SurveySerializer(survey).data)


class CaptureUploadView(APIView):
    parser_classes = (parsers.MultiPartParser, parsers.FormParser)

    @extend_schema(
        summary="Upload client capture",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer"},
                    "file": {"type": "string", "format": "binary"},
                },
                "required": ["client_id", "file"],
            }
        },
        responses={200: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        client_id = request.data.get("client_id")
        client = get_object_or_404(Client, id=client_id)
        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response({"detail": "Image file is required."}, status=status.HTTP_400_BAD_REQUEST)

        original_bytes = file_obj.read()
        original_ext = "." + file_obj.name.split(".")[-1] if "." in file_obj.name else ".jpg"
        try:
            with Image.open(io.BytesIO(original_bytes)) as image:
                image = ImageOps.exif_transpose(image)
                sanitized_original_bytes, sanitized_ext = sanitize_original_upload(
                    image=image,
                    original_ext=original_ext,
                )
                processed_buffer = io.BytesIO()
                image.convert("RGB").save(processed_buffer, "JPEG")
                processed_bytes = processed_buffer.getvalue()
        except OSError:
            return Response({"detail": "Unsupported or invalid image file."}, status=status.HTTP_400_BAD_REQUEST)

        validation = validate_capture_image(processed_bytes=processed_bytes)
        landmark_snapshot = extract_landmark_snapshot(processed_bytes=processed_bytes)
        if settings.MIRRAI_PERSIST_CAPTURE_IMAGES:
            deidentified_bytes, privacy_snapshot = build_deidentified_capture(
                processed_bytes=processed_bytes,
                landmark_snapshot=landmark_snapshot,
            )
            stored_filename, original_path, processed_path, deidentified_path = store_capture_assets(
                original_name=file_obj.name,
                original_bytes=sanitized_original_bytes,
                processed_bytes=processed_bytes,
                original_ext=sanitized_ext,
                deidentified_bytes=deidentified_bytes,
            )
            privacy_snapshot = {
                **privacy_snapshot,
                "storage_policy": "asset_store",
            }
        else:
            stored_filename = None
            original_path = None
            processed_path = None
            deidentified_path = None
            privacy_snapshot = {
                "metadata_removed": True,
                "deidentification_applied": False,
                "storage_policy": "vector_only",
                "persisted_assets": [],
                "reason": "capture_images_not_persisted",
            }

        record = CaptureRecord.objects.create(
            client=client,
            original_path=original_path,
            processed_path=processed_path,
            filename=stored_filename,
            status=validation["status"],
            face_count=validation["face_count"],
            landmark_snapshot=landmark_snapshot,
            deidentified_path=deidentified_path,
            privacy_snapshot=privacy_snapshot,
            error_note=(None if validation["is_valid"] else validation["message"]),
        )

        if not validation["is_valid"]:
            return Response(
                {
                    "status": "needs_retake",
                    "record_id": record.id,
                    "face_count": validation["face_count"],
                    "reason_code": validation["reason_code"],
                    "message": validation["message"],
                    "next_action": "capture",
                    "privacy_snapshot": privacy_snapshot,
                }
            )

        thread_args = (record.id,)
        thread_kwargs = {}
        if not settings.MIRRAI_PERSIST_CAPTURE_IMAGES:
            thread_kwargs["processed_bytes"] = processed_bytes
        threading.Thread(
            target=run_mirrai_analysis_pipeline,
            args=thread_args,
            kwargs=thread_kwargs,
            daemon=True,
        ).start()
        return Response(
            {
                "status": "success",
                "record_id": record.id,
                "face_count": validation["face_count"],
                "message": validation["message"],
                "privacy_snapshot": privacy_snapshot,
            }
        )


class CaptureStatusView(APIView):
    @extend_schema(
        summary="Get capture processing status",
        parameters=[OpenApiParameter("record_id", OpenApiTypes.INT, OpenApiParameter.QUERY, required=True)],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        record = get_object_or_404(CaptureRecord, id=request.query_params.get("record_id"))
        return Response(serialize_capture_status(record))


class FormerRecommendationView(APIView):
    @extend_schema(
        summary="Get former recommendation history",
        parameters=[OpenApiParameter("client_id", OpenApiTypes.INT, OpenApiParameter.QUERY, required=True)],
        responses={200: RecommendationListResponseSerializer},
    )
    def get(self, request):
        client_id = request.query_params.get("client_id")
        client = get_object_or_404(Client, id=client_id)
        return Response(get_former_recommendations(client))


class RecommendationView(APIView):
    @extend_schema(
        summary="Get current recommendations",
        parameters=[OpenApiParameter("client_id", OpenApiTypes.INT, OpenApiParameter.QUERY, required=True)],
        responses={200: RecommendationListResponseSerializer},
    )
    def get(self, request):
        client_id = request.query_params.get("client_id")
        client = get_object_or_404(Client, id=client_id)
        return Response(get_current_recommendations(client))


class TrendView(APIView):
    @extend_schema(
        summary="Get trend-based style recommendations",
        parameters=[
            OpenApiParameter("days", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("client_id", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: RecommendationListResponseSerializer},
    )
    def get(self, request):
        days = int(request.query_params.get("days", 30))
        client_id = request.query_params.get("client_id")
        client = get_object_or_404(Client, id=client_id) if client_id else None
        return Response(get_trend_recommendations(days=days, client=client))


class ConfirmView(APIView):
    @extend_schema(
        summary="Confirm selected style and hand off to admin",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer"},
                    "recommendation_id": {"type": "integer"},
                    "style_id": {"type": "integer"},
                    "admin_id": {"type": "integer"},
                    "source": {"type": "string", "example": "current_recommendations"},
                    "direct_consultation": {"type": "boolean", "default": False},
                },
                "required": ["client_id"],
            }
        },
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        client = get_object_or_404(Client, id=request.data.get("client_id"))
        try:
            payload = confirm_style_selection(
                client=client,
                recommendation_id=request.data.get("recommendation_id"),
                style_id=request.data.get("style_id"),
                admin_id=request.data.get("admin_id"),
                source=request.data.get("source", "current_recommendations"),
                direct_consultation=bool(request.data.get("direct_consultation", False)),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class CancelView(APIView):
    @extend_schema(
        summary="Cancel selected style and return to client input",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer"},
                    "recommendation_id": {"type": "integer"},
                    "source": {"type": "string", "example": "current_recommendations"},
                },
                "required": ["client_id"],
            }
        },
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        client = get_object_or_404(Client, id=request.data.get("client_id"))
        try:
            payload = cancel_style_selection(
                client=client,
                recommendation_id=request.data.get("recommendation_id"),
                source=request.data.get("source", "current_recommendations"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class ConsultView(ConfirmView):
    pass

