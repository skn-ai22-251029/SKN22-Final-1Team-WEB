import io
import shutil
import tempfile
from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.contrib.auth.hashers import make_password
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from app.api.v1.admin_auth import build_admin_token
from app.api.v1.services_django import (
    get_latest_analysis,
    get_current_recommendations,
    persist_generated_batch,
    run_mirrai_analysis_pipeline,
    upsert_survey,
)
from app.models_model_team import LegacyClientAnalysis, LegacyClientResult
from app.services.model_team_bridge import (
    _table_columns,
    complete_legacy_capture_analysis,
    create_admin_record,
    create_designer_record,
    create_legacy_capture_upload_record,
    get_admin_by_identifier,
    upsert_client_record,
)
from app.services.legacy_model_sync import _existing_legacy_tables, _table_names
from app.tests.test_legacy_model_sync import LEGACY_TABLE_DDL, LEGACY_TABLES


def build_valid_business_number(prefix: str = "123456789") -> str:
    digits = [int(char) for char in prefix]
    weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    checksum = sum(digit * weight for digit, weight in zip(digits, weights))
    checksum += (digits[8] * 5) // 10
    check_digit = (10 - (checksum % 10)) % 10
    return prefix + str(check_digit)


def _create_runtime_client(*, name: str, phone: str, gender: str = "F"):
    return upsert_client_record(name=name, phone=phone, gender=gender)


def _create_runtime_admin(*, name: str, store_name: str, phone: str, business_number: str, role: str = "owner"):
    return create_admin_record(
        name=name,
        store_name=store_name,
        role=role,
        phone=phone,
        business_number=business_number,
        password_hash=make_password("pw1234!!"),
        consent_snapshot={
            "agree_terms": True,
            "agree_privacy": True,
            "agree_third_party_sharing": True,
        },
        consented_at=timezone.now(),
    )


def _create_runtime_designer(*, admin, name: str, phone: str, raw_pin: str = "1234"):
    return create_designer_record(
        admin=admin,
        name=name,
        phone=phone,
        pin_hash=make_password(raw_pin),
    )


def _create_assigned_runtime_client(*, name: str, phone: str, gender: str = "F"):
    admin = _create_runtime_admin(
        name=f"{name} Admin",
        store_name=f"{name} Store",
        phone=f"02{phone[2:]}",
        business_number=build_valid_business_number("567890123"),
    )
    designer = _create_runtime_designer(
        admin=admin,
        name=f"{name} Designer",
        phone=f"01{phone[2:]}",
        raw_pin="2468",
    )
    return upsert_client_record(
        name=name,
        phone=phone,
        gender=gender,
        shop=admin,
        designer=designer,
        assignment_source="designer_session",
    )


def _create_runtime_survey(client, *, target_length: str, target_vibe: str, scalp_type: str, hair_colour: str, budget_range: str):
    return upsert_survey(
        client,
        {
            "target_length": target_length,
            "target_vibe": target_vibe,
            "scalp_type": scalp_type,
            "hair_colour": hair_colour,
            "budget_range": budget_range,
        },
    )


def _create_legacy_capture(
    client,
    *,
    original_path=None,
    processed_path=None,
    filename=None,
    status="DONE",
    face_count=1,
    landmark_snapshot=None,
    deidentified_path=None,
    privacy_snapshot=None,
    error_note=None,
):
    return create_legacy_capture_upload_record(
        client=client,
        original_path=original_path,
        processed_path=processed_path,
        filename=filename,
        status=status,
        face_count=face_count,
        landmark_snapshot=landmark_snapshot,
        deidentified_path=deidentified_path,
        privacy_snapshot=privacy_snapshot,
        error_note=error_note,
    )


def _complete_legacy_analysis(client, capture, *, face_shape: str, golden_ratio_score: float, landmark_snapshot: dict | None, analysis_image_url=None):
    _, analysis = complete_legacy_capture_analysis(
        record_id=capture.id,
        face_shape=face_shape,
        golden_ratio_score=golden_ratio_score,
        landmark_snapshot=landmark_snapshot,
        analysis_image_url=analysis_image_url,
    )
    return analysis


def _seed_generated_batch(
    *,
    client,
    target_length: str,
    target_vibe: str,
    scalp_type: str,
    hair_colour: str,
    budget_range: str,
    face_shape: str,
    golden_ratio_score: float,
):
    survey = _create_runtime_survey(
        client,
        target_length=target_length,
        target_vibe=target_vibe,
        scalp_type=scalp_type,
        hair_colour=hair_colour,
        budget_range=budget_range,
    )
    capture = _create_legacy_capture(
        client,
        original_path=None,
        processed_path=None,
        filename=None,
        status="DONE",
        face_count=1,
        landmark_snapshot={"version": "coarse-v1"},
        deidentified_path=None,
        privacy_snapshot={"storage_policy": "vector_only"},
        error_note=None,
    )
    analysis = _complete_legacy_analysis(
        client,
        capture,
        face_shape=face_shape,
        golden_ratio_score=golden_ratio_score,
        landmark_snapshot={"version": "coarse-v1"},
        analysis_image_url=None,
    )
    return persist_generated_batch(client=client, capture_record=capture, survey=survey, analysis=analysis)


def _reset_legacy_table_caches():
    _table_columns.cache_clear()
    _existing_legacy_tables.cache_clear()
    _table_names.cache_clear()


@override_settings(SUPABASE_USE_REMOTE_STORAGE=False)
class BackendIssueProgressTests(APITestCase):
    def setUp(self):
        self.temp_media_root = tempfile.mkdtemp(prefix="mirrai-test-media-")
        self.media_override = override_settings(MEDIA_ROOT=self.temp_media_root)
        self.media_override.enable()
        self._preexisting_tables = set(connection.introspection.table_names())
        with connection.cursor() as cursor:
            for ddl in LEGACY_TABLE_DDL:
                cursor.execute(ddl)
        _reset_legacy_table_caches()

    def tearDown(self):
        self.media_override.disable()
        created_tables = [table for table in LEGACY_TABLES if table not in self._preexisting_tables]
        with connection.cursor() as cursor:
            for table in created_tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        _reset_legacy_table_caches()
        shutil.rmtree(self.temp_media_root, ignore_errors=True)

    def test_admin_register_rejects_invalid_business_number(self):
        response = self.client.post(
            "/api/v1/admin/auth/register/",
            {
                "name": "Owner Kim",
                "store_name": "MirrAI Salon",
                "role": "owner",
                "phone": "01011112222",
                "business_number": "123-45-67890",
                "password": "pw1234!!",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_third_party_sharing": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)

    def test_admin_register_requires_required_consents(self):
        valid_business_number = build_valid_business_number("123456780")
        response = self.client.post(
            "/api/v1/admin/auth/register/",
            {
                "name": "Owner Park",
                "store_name": "MirrAI Branch",
                "role": "owner",
                "phone": "01066667777",
                "business_number": valid_business_number,
                "password": "pw1234!!",
                "agree_terms": True,
                "agree_privacy": False,
                "agree_third_party_sharing": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)
        self.assertIn("agree_privacy", response.data["detail"])

    def test_admin_register_accepts_valid_business_number_and_normalizes_it(self):
        valid_business_number = build_valid_business_number()
        response = self.client.post(
            "/api/v1/admin/auth/register/",
            {
                "name": "Owner Lee",
                "store_name": "MirrAI Lab",
                "role": "owner",
                "phone": "01022223333",
                "business_number": f"{valid_business_number[:3]}-{valid_business_number[3:5]}-{valid_business_number[5:]}",
                "password": "pw1234!!",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_third_party_sharing": True,
                "agree_marketing": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        admin = get_admin_by_identifier(identifier=response.data["admin_id"])
        self.assertIsNotNone(admin)
        self.assertEqual(admin.business_number, valid_business_number)
        self.assertTrue(admin.consent_snapshot["agree_terms"])
        self.assertIsNotNone(admin.consented_at)
        self.assertIn("access_token", response.data)

    def test_admin_login_returns_token_and_protected_endpoint_accepts_it(self):
        valid_business_number = build_valid_business_number("234567890")
        register_response = self.client.post(
            "/api/v1/admin/auth/register/",
            {
                "name": "Owner Choi",
                "store_name": "MirrAI Auth",
                "role": "owner",
                "phone": "01077778888",
                "business_number": valid_business_number,
                "password": "pw1234!!",
                "agree_terms": True,
                "agree_privacy": True,
                "agree_third_party_sharing": True,
            },
            format="json",
        )
        self.assertEqual(register_response.status_code, status.HTTP_201_CREATED)

        unauthorized_response = self.client.get("/api/v1/admin/auth/me/")
        self.assertIn(unauthorized_response.status_code, {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN})

        login_response = self.client.post(
            "/api/v1/admin/auth/login/",
            {
                "phone": "01077778888",
                "password": "pw1234!!",
            },
            format="json",
        )
        self.assertEqual(login_response.status_code, status.HTTP_200_OK)
        token = login_response.data["access_token"]
        self.assertGreater(login_response.data["expires_in"], 0)

        me_response = self.client.get(
            "/api/v1/admin/auth/me/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(me_response.status_code, status.HTTP_200_OK)
        self.assertEqual(me_response.data["admin"]["store_name"], "MirrAI Auth")
        self.assertTrue(me_response.data["admin"]["consent_snapshot"]["agree_privacy"])

    def test_capture_upload_blank_image_requests_retake_and_status_endpoint_matches(self):
        client = _create_assigned_runtime_client(name="Capture Tester", phone="01012340000", gender="F")
        buffer = io.BytesIO()
        Image.new("RGB", (640, 640), "white").save(buffer, format="PNG")
        upload = SimpleUploadedFile("blank.png", buffer.getvalue(), content_type="image/png")

        response = self.client.post(
            "/api/v1/capture/upload/",
            {
                "client_id": str(client.id),
                "file": upload,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "needs_retake")

        record = LegacyClientAnalysis.objects.get(analysis_id=response.data["record_id"])
        self.assertEqual(record.status, "NEEDS_RETAKE")
        self.assertIsNotNone(record.error_note)
        self.assertIsNone(record.original_image_url)
        self.assertEqual(record.privacy_snapshot["storage_policy"], "vector_only")

        status_response = self.client.get(f"/api/v1/capture/status/?record_id={record.analysis_id}")
        self.assertEqual(status_response.status_code, status.HTTP_200_OK)
        self.assertEqual(status_response.data["status"], "needs_retake")
        self.assertEqual(status_response.data["next_action"], "capture")
        self.assertEqual(status_response.data["image_storage_policy"], "vector_only")

    @override_settings(MIRRAI_PERSIST_CAPTURE_IMAGES=True)
    def test_capture_upload_persists_landmarks_and_deidentified_asset(self):
        client = _create_assigned_runtime_client(name="Face Tester", phone="01055556666", gender="F")
        buffer = io.BytesIO()
        Image.new("RGB", (640, 640), "gray").save(buffer, format="PNG")
        upload = SimpleUploadedFile("face.png", buffer.getvalue(), content_type="image/png")

        landmark_snapshot = {
            "version": "coarse-v1",
            "face_count": 1,
            "image_size": {"width": 640, "height": 640},
            "face_bbox": {"x": 120, "y": 100, "width": 320, "height": 360},
            "landmarks": {
                "left_eye": {"point": {"x": 220.0, "y": 230.0}},
                "right_eye": {"point": {"x": 340.0, "y": 232.0}},
                "nose_tip": {"point": {"x": 280.0, "y": 320.0}},
                "mouth_center": {"point": {"x": 280.0, "y": 380.0}},
                "chin_center": {"point": {"x": 280.0, "y": 430.0}},
            },
            "quality": {"coverage": "coarse", "detected_feature_count": 5},
        }
        privacy_snapshot = {
            "metadata_removed": True,
            "deidentification_applied": True,
            "method": "pixelate_face_region",
        }

        class DummyThread:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            def start(self):
                return None

        with (
            patch("app.api.v1.django_views.validate_capture_image", return_value={
                "is_valid": True,
                "status": "PENDING",
                "face_count": 1,
                "reason_code": "ok",
                "message": "ready",
            }),
            patch("app.api.v1.django_views.extract_landmark_snapshot", return_value=landmark_snapshot),
            patch("app.api.v1.django_views.build_deidentified_capture", return_value=(b"fake-blurred-jpeg", privacy_snapshot)),
            patch("app.api.v1.django_views.threading.Thread", DummyThread),
        ):
            response = self.client.post(
                "/api/v1/capture/upload/",
                {
                    "client_id": str(client.id),
                    "file": upload,
                },
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(response.data["next_action"], "survey")
        self.assertIn("survey", response.data["next_actions"])
        self.assertEqual(response.data["privacy_snapshot"]["method"], "pixelate_face_region")

        record = LegacyClientAnalysis.objects.get(analysis_id=response.data["record_id"])
        self.assertEqual(record.capture_landmark_snapshot["quality"]["detected_feature_count"], 5)
        self.assertEqual(record.privacy_snapshot["method"], "pixelate_face_region")
        self.assertEqual(record.privacy_snapshot["storage_policy"], "asset_store")
        self.assertIsNotNone(record.deidentified_path)

        status_response = self.client.get(f"/api/v1/capture/status/?record_id={record.analysis_id}")
        self.assertEqual(status_response.status_code, status.HTTP_200_OK)
        self.assertIn("deidentified_image_url", status_response.data)
        self.assertEqual(status_response.data["landmark_snapshot"]["version"], "coarse-v1")
        self.assertEqual(status_response.data["image_storage_policy"], "asset_store")

    def test_capture_upload_vector_only_policy_skips_image_persistence(self):
        client = _create_assigned_runtime_client(name="Vector Policy Tester", phone="01011110000", gender="F")
        buffer = io.BytesIO()
        Image.new("RGB", (640, 640), "gray").save(buffer, format="PNG")
        upload = SimpleUploadedFile("vector.png", buffer.getvalue(), content_type="image/png")

        landmark_snapshot = {
            "version": "coarse-v1",
            "face_count": 1,
            "image_size": {"width": 640, "height": 640},
            "face_bbox": {"x": 120, "y": 100, "width": 320, "height": 360},
            "landmarks": {
                "left_eye": {"point": {"x": 220.0, "y": 230.0}},
                "right_eye": {"point": {"x": 340.0, "y": 232.0}},
            },
            "quality": {"coverage": "coarse", "detected_feature_count": 2},
        }

        class DummyThread:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            def start(self):
                return None

        with (
            patch("app.api.v1.django_views.validate_capture_image", return_value={
                "is_valid": True,
                "status": "PENDING",
                "face_count": 1,
                "reason_code": "ok",
                "message": "ready",
            }),
            patch("app.api.v1.django_views.extract_landmark_snapshot", return_value=landmark_snapshot),
            patch("app.api.v1.django_views.threading.Thread", DummyThread),
        ):
            response = self.client.post(
                "/api/v1/capture/upload/",
                {
                    "client_id": str(client.id),
                    "file": upload,
                },
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        record = LegacyClientAnalysis.objects.get(analysis_id=response.data["record_id"])
        self.assertIsNone(record.original_image_url)
        self.assertIsNone(record.processed_path)
        self.assertIsNone(record.deidentified_path)
        self.assertEqual(record.privacy_snapshot["storage_policy"], "vector_only")
        self.assertEqual(response.data["privacy_snapshot"]["storage_policy"], "vector_only")

    def test_analysis_pipeline_copies_landmark_snapshot_to_face_analysis(self):
        client = _create_assigned_runtime_client(name="Pipeline Tester", phone="01033334444", gender="F")
        _create_runtime_survey(
            client,
            target_length="short",
            target_vibe="soft",
            scalp_type="normal",
            hair_colour="black",
            budget_range="10-15",
        )
        jpeg_buffer = io.BytesIO()
        Image.new("RGB", (320, 320), "gray").save(jpeg_buffer, format="JPEG")

        record = _create_legacy_capture(
            client,
            original_path=None,
            processed_path=None,
            filename=None,
            status="PENDING",
            face_count=1,
            landmark_snapshot={
                "version": "coarse-v1",
                "landmarks": {
                    "left_eye": {"point": {"x": 120.0, "y": 140.0}},
                    "right_eye": {"point": {"x": 220.0, "y": 142.0}},
                },
            },
            privacy_snapshot={"deidentification_applied": False, "storage_policy": "vector_only"},
        )

        run_mirrai_analysis_pipeline(record.id, processed_bytes=jpeg_buffer.getvalue())

        record = LegacyClientAnalysis.objects.get(analysis_id=record.id)
        self.assertEqual(record.status, "DONE")
        analysis = get_latest_analysis(client)
        self.assertEqual(analysis.landmark_snapshot["version"], "coarse-v1")
        self.assertTrue(analysis.image_url is None or "analysis-inputs/" in analysis.image_url)

    def test_recommendations_and_admin_detail_include_reasoning_and_history(self):
        client = _create_assigned_runtime_client(name="History Tester", phone="01098765432", gender="F")
        admin = _create_runtime_admin(
            name="Manager Han",
            store_name="MirrAI Admin",
            phone="01044445555",
            business_number=build_valid_business_number("345678901"),
        )
        admin_token = build_admin_token(admin=admin)
        _, rows = _seed_generated_batch(
            client=client,
            target_length="medium",
            target_vibe="chic",
            scalp_type="normal",
            hair_colour="brown",
            budget_range="10-15",
            face_shape="Oval",
            golden_ratio_score=0.92,
        )
        selected_row = rows[0]
        self.assertIsNotNone(selected_row.sample_image_url)
        self.assertIsNone(selected_row.simulation_image_url)
        self.assertEqual(selected_row.regeneration_snapshot["version"], "vector-only-v1")

        confirm_response = self.client.post(
            "/api/v1/analysis/confirm/",
            {
                "client_id": client.id,
                "recommendation_id": selected_row.id,
                "admin_id": admin.id,
                "source": "current_recommendations",
                "direct_consultation": False,
            },
            format="json",
        )
        self.assertEqual(confirm_response.status_code, status.HTTP_200_OK)

        recommendation_response = self.client.get(f"/api/v1/analysis/recommendations/?client_id={client.id}")
        self.assertEqual(recommendation_response.status_code, status.HTTP_200_OK)
        self.assertEqual(recommendation_response.data["status"], "ready")
        first_card = recommendation_response.data["items"][0]
        self.assertIn("reasoning_snapshot", first_card)
        self.assertIn("summary", first_card["reasoning_snapshot"])
        self.assertIn("face_score", first_card["reasoning_snapshot"])
        self.assertIsNotNone(first_card.get("sample_image_url"))
        self.assertIn(first_card.get("simulation_image_url"), {None, ""})

        detail_response = self.client.get(
            f"/api/v1/admin/clients/detail/?client_id={client.id}",
            HTTP_AUTHORIZATION=f"Bearer {admin_token}",
        )
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(detail_response.data["capture_history"]), 1)
        self.assertEqual(len(detail_response.data["analysis_history"]), 1)
        self.assertIn("style_selection_history", detail_response.data)
        self.assertIn("chosen_recommendation_history", detail_response.data)
        self.assertIn("landmark_snapshot", detail_response.data["capture_history"][0])
        self.assertEqual(detail_response.data["capture_history"][0]["image_storage_policy"], "vector_only")
        self.assertIsNone(detail_response.data["capture_history"][0]["processed_image_url"])
        self.assertEqual(detail_response.data["analysis_history"][0]["landmark_snapshot"]["version"], "coarse-v1")
        if detail_response.data["chosen_recommendation_history"]:
            self.assertEqual(detail_response.data["chosen_recommendation_history"][0]["image_policy"], "vector_only")

    def test_confirm_style_selection_binds_admin_scope_when_admin_id_is_provided(self):
        client = _create_assigned_runtime_client(name="Scope Tester", phone="01022224444", gender="F")
        admin = _create_runtime_admin(
            name="Manager Scope",
            store_name="MirrAI Scope",
            phone="01099990000",
            business_number=build_valid_business_number("456789012"),
        )
        _, rows = _seed_generated_batch(
            client=client,
            target_length="medium",
            target_vibe="soft",
            scalp_type="normal",
            hair_colour="brown",
            budget_range="10-15",
            face_shape="Oval",
            golden_ratio_score=0.88,
        )

        response = self.client.post(
            "/api/v1/analysis/confirm/",
            {
                "client_id": client.id,
                "recommendation_id": rows[0].id,
                "admin_id": admin.id,
                "source": "current_recommendations",
                "direct_consultation": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        consultation = LegacyClientResult.objects.get(result_id=response.data["consultation_id"])
        self.assertEqual(consultation.backend_admin_ref_id, admin.id)

        admin_response = self.client.get(
            "/api/v1/admin/dashboard/",
            HTTP_AUTHORIZATION=f"Bearer {build_admin_token(admin=admin)}",
        )
        self.assertEqual(admin_response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(admin_response.data["today_metrics"]["active_clients"], 1)

    @override_settings(DEBUG=True, MIRRAI_LOCAL_MOCK_RESULTS=True)
    def test_current_recommendations_returns_local_mock_when_analysis_is_not_ready(self):
        client = _create_assigned_runtime_client(name="Mock Tester", phone="01033335555", gender="F")
        _create_runtime_survey(
            client,
            target_length="medium",
            target_vibe="natural",
            scalp_type="normal",
            hair_colour="brown",
            budget_range="10-15",
        )
        _create_legacy_capture(
            client,
            original_path=None,
            processed_path=None,
            filename=None,
            status="DONE",
            face_count=1,
            landmark_snapshot={},
            deidentified_path=None,
            privacy_snapshot={"storage_policy": "vector_only"},
            error_note=None,
        )

        payload = get_current_recommendations(client)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["source"], "current_recommendations")
        self.assertGreaterEqual(len(payload["items"]), 1)
        self.assertEqual(payload["recommendation_stage"], "initial")
        self.assertTrue(all(bool(item.get("source")) for item in payload["items"]))

    @override_settings(DEBUG=True, MIRRAI_LOCAL_MOCK_RESULTS=False)
    def test_current_recommendations_without_local_mock_still_requires_capture_analysis(self):
        client = _create_assigned_runtime_client(name="Strict Tester", phone="01033336666", gender="F")
        _create_legacy_capture(
            client,
            original_path=None,
            processed_path=None,
            filename=None,
            status="DONE",
            face_count=1,
            landmark_snapshot={},
            deidentified_path=None,
            privacy_snapshot={"storage_policy": "vector_only"},
            error_note=None,
        )

        payload = get_current_recommendations(client)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["source"], "current_recommendations")
        self.assertGreaterEqual(len(payload["items"]), 1)

