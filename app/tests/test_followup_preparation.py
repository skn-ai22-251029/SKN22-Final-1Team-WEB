import io
import os
from unittest import mock

from django.contrib.auth.hashers import make_password
from django.db import connection
from django.utils import timezone
from rest_framework import status
from PIL import Image
from rest_framework.test import APITestCase

from app.api.v1.admin_services import get_admin_trend_report, get_style_report
from app.api.v1.admin_auth import build_admin_refresh_token, build_admin_token, build_client_refresh_token, get_admin_auth_policy_snapshot
from app.api.v1.response_helpers import detail_response, get_error_contract_snapshot
from app.api.v1.services_django import persist_generated_batch, upsert_survey
from app.services.face_processing import build_deidentified_capture
from app.services.model_team_bridge import (
    _table_columns,
    complete_legacy_capture_analysis,
    create_admin_record,
    create_designer_record,
    create_legacy_capture_upload_record,
    upsert_client_record,
)
from app.services.legacy_model_sync import _existing_legacy_tables, _table_names
from app.services.storage_service import build_storage_snapshot
from app.tests.test_legacy_model_sync import LEGACY_TABLE_DDL, LEGACY_TABLES


def _create_runtime_client(*, name: str, phone: str, gender: str = "F"):
    return upsert_client_record(name=name, phone=phone, gender=gender)


def _create_runtime_admin(*, name: str, store_name: str, phone: str, business_number: str, role: str = "owner"):
    return create_admin_record(
        name=name,
        store_name=store_name,
        role=role,
        phone=phone,
        business_number=business_number,
        password_hash=make_password("plain-password"),
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
        business_number="1012345672",
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
    survey = upsert_survey(
        client,
        {
            "target_length": target_length,
            "target_vibe": target_vibe,
            "scalp_type": scalp_type,
            "hair_colour": hair_colour,
            "budget_range": budget_range,
        },
    )
    capture = create_legacy_capture_upload_record(
        client=client,
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
    _, analysis = complete_legacy_capture_analysis(
        record_id=capture.id,
        face_shape=face_shape,
        golden_ratio_score=golden_ratio_score,
        landmark_snapshot={"version": "coarse-v1"},
        analysis_image_url=None,
    )
    return persist_generated_batch(
        client=client,
        capture_record=capture,
        survey=survey,
        analysis=analysis,
    )


def _reset_legacy_table_caches():
    _table_columns.cache_clear()
    _existing_legacy_tables.cache_clear()
    _table_names.cache_clear()


class ContractPreparationSnapshotTests(APITestCase):
    def setUp(self):
        self._preexisting_tables = set(connection.introspection.table_names())
        with connection.cursor() as cursor:
            for ddl in LEGACY_TABLE_DDL:
                cursor.execute(ddl)
        _reset_legacy_table_caches()

    def tearDown(self):
        created_tables = [table for table in LEGACY_TABLES if table not in self._preexisting_tables]
        with connection.cursor() as cursor:
            for table in created_tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        _reset_legacy_table_caches()

    def test_error_contract_snapshot_reports_current_compat_mode(self):
        payload = get_error_contract_snapshot()

        self.assertEqual(payload["mode"], "compat_envelope")
        self.assertEqual(payload["fields"], ["detail", "message", "error_code", "errors"])
        self.assertTrue(payload["envelope_supported"])
        self.assertTrue(payload["detail_backward_compatible"])

    def test_detail_response_uses_default_error_code_for_not_found(self):
        response = detail_response("Client not found.", status_code=status.HTTP_404_NOT_FOUND)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["error_code"], "not_found")
        self.assertEqual(response.data["message"], "Client not found.")
        self.assertEqual(response.data["detail"], "Client not found.")

    def test_detail_response_includes_field_errors_when_provided(self):
        response = detail_response(
            "Validation failed.",
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="validation_error",
            errors={"phone": ["이미 등록된 번호입니다."]},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error_code"], "validation_error")
        self.assertEqual(response.data["errors"]["phone"], ["이미 등록된 번호입니다."])

    def test_admin_auth_policy_snapshot_reports_refresh_support(self):
        payload = get_admin_auth_policy_snapshot()

        self.assertEqual(payload["token_type"], "bearer")
        self.assertTrue(payload["refresh_token_supported"])
        self.assertGreater(payload["token_max_age_seconds"], 0)
        self.assertGreater(payload["refresh_token_max_age_seconds"], payload["token_max_age_seconds"])

    def test_storage_snapshot_reports_db_and_storage_linkage(self):
        payload = build_storage_snapshot(
            original_path="captures/original.jpg",
            processed_path="captures/processed.jpg",
            deidentified_path="captures/deidentified.jpg",
        )

        self.assertIn(payload["storage_mode"], {"local", "remote"})
        self.assertIn("mirrai-assets", payload["bucket_name"])
        self.assertEqual(payload["path_count"], 3)
        self.assertGreaterEqual(payload["resolved_url_count"], 0)
        self.assertTrue(payload["has_required_capture_assets"])
        self.assertIn(payload["fully_resolved_capture_assets"], {True, False})
        self.assertEqual(payload["reference_presence"]["original_path"], True)
        self.assertIn(
            payload["resolution_statuses"]["original_path"],
            {
                "local_reference",
                "signed_url",
                "signed_url_failed",
                "signed_url_unresolved",
                "storage_client_unavailable",
                "public_url",
                "already_resolved",
            },
        )
        original_url = payload["resolved_urls"]["original_path"]
        if original_url is not None:
            self.assertIn("captures/original.jpg", original_url)
        else:
            self.assertIn(
                payload["resolution_statuses"]["original_path"],
                {"signed_url_failed", "signed_url_unresolved", "storage_client_unavailable"},
            )

    def test_deidentified_capture_applies_mirrai_watermark(self):
        buffer = io.BytesIO()
        Image.new("RGB", (640, 640), "white").save(buffer, format="PNG")
        image_bytes = buffer.getvalue()
        landmark_snapshot = {
            "face_bbox": {"x": 160, "y": 150, "width": 240, "height": 260},
            "landmarks": {
                "left_eye": {"point": {"x": 230, "y": 250}},
                "right_eye": {"point": {"x": 360, "y": 252}},
            },
        }

        deidentified_bytes, privacy_snapshot = build_deidentified_capture(
            processed_bytes=image_bytes,
            landmark_snapshot=landmark_snapshot,
        )

        self.assertIsNotNone(deidentified_bytes)
        self.assertTrue(privacy_snapshot["watermark_applied"])
        self.assertEqual(privacy_snapshot["watermark_mode"], "image")
        self.assertEqual(privacy_snapshot["watermark_asset"], "mirrai_wordmark_primary.png")
        self.assertIn("watermark_config", privacy_snapshot)
        self.assertIn("opacity", privacy_snapshot["watermark_config"])
        self.assertIn("angle", privacy_snapshot["watermark_config"])
        self.assertTrue(privacy_snapshot["eye_bar_applied"])

    def test_admin_trend_report_exposes_report_snapshot(self):
        payload = get_admin_trend_report(days=7, filters={"store_name": "MirrAI"})

        self.assertEqual(payload["status"], "ready")
        self.assertIn("report_snapshot", payload)
        self.assertEqual(payload["report_snapshot"]["days"], 7)
        self.assertEqual(payload["report_snapshot"]["filters"]["store_name"], "MirrAI")
        self.assertIn("message", payload)

    def test_style_report_exposes_report_snapshot(self):
        payload = get_style_report(style_id=101, days=7)

        self.assertEqual(payload["status"], "ready")
        self.assertIn("report_snapshot", payload)
        self.assertEqual(payload["report_snapshot"]["style_id"], 101)
        self.assertEqual(payload["report_snapshot"]["days"], 7)

    def test_admin_trend_report_endpoint_exposes_report_snapshot(self):
        admin = _create_runtime_admin(
            name="Trend Admin",
            store_name="MirrAI Trend",
            phone="01090909090",
            business_number="1012345672",
        )

        response = self.client.get(
            "/api/v1/admin/trend-report/?days=7",
            HTTP_AUTHORIZATION=f"Bearer {build_admin_token(admin=admin)}",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("report_snapshot", response.data)
        self.assertEqual(response.data["report_snapshot"]["days"], 7)

    def test_admin_style_report_endpoint_exposes_report_snapshot(self):
        admin = _create_runtime_admin(
            name="Style Admin",
            store_name="MirrAI Style",
            phone="01091919191",
            business_number="2222222222",
        )

        response = self.client.get(
            "/api/v1/admin/style-report/?style_id=101&days=14",
            HTTP_AUTHORIZATION=f"Bearer {build_admin_token(admin=admin)}",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("report_snapshot", response.data)
        self.assertEqual(response.data["report_snapshot"]["style_id"], 101)
        self.assertEqual(response.data["report_snapshot"]["days"], 14)


class RegenerateSimulationEndpointTests(APITestCase):
    def setUp(self):
        self._preexisting_tables = set(connection.introspection.table_names())
        with connection.cursor() as cursor:
            for ddl in LEGACY_TABLE_DDL:
                cursor.execute(ddl)
        _reset_legacy_table_caches()

    def tearDown(self):
        created_tables = [table for table in LEGACY_TABLES if table not in self._preexisting_tables]
        with connection.cursor() as cursor:
            for table in created_tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        _reset_legacy_table_caches()

    @mock.patch.dict(os.environ, {"MIRRAI_AI_PROVIDER": "local"}, clear=False)
    def test_regenerate_simulation_endpoint_returns_card_for_vector_only_row(self):
        client = _create_assigned_runtime_client(name="Regen Tester", phone="01012121212", gender="F")
        _, rows = _seed_generated_batch(
            client=client,
            target_length="medium",
            target_vibe="soft",
            scalp_type="normal",
            hair_colour="brown",
            budget_range="10-15",
            face_shape="Oval",
            golden_ratio_score=0.89,
        )

        response = self.client.post(
            "/api/v1/analysis/regenerate-simulation/",
            {"recommendation_id": rows[0].id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(response.data["recommendation_id"], rows[0].id)
        self.assertEqual(response.data["image_policy"], "vector_only")
        self.assertEqual(response.data["card"]["style_id"], rows[0].style_id_snapshot)
        self.assertFalse(response.data["card"]["can_regenerate_simulation"])
        self.assertEqual(response.data["card"]["regeneration_remaining_count"], 0)
        self.assertIn("regenerated", response.data["card"]["reasoning_snapshot"])

        second_response = self.client.post(
            "/api/v1/analysis/regenerate-simulation/",
            {"recommendation_id": rows[0].id},
            format="json",
        )
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch.dict(os.environ, {"MIRRAI_AI_PROVIDER": "local"}, clear=False)
    def test_regenerate_simulation_endpoint_accepts_snapshot_and_style_id(self):
        client = _create_assigned_runtime_client(name="Snapshot Tester", phone="01034343434", gender="F")
        _, rows = _seed_generated_batch(
            client=client,
            target_length="short",
            target_vibe="chic",
            scalp_type="normal",
            hair_colour="black",
            budget_range="10-15",
            face_shape="Oval",
            golden_ratio_score=0.9,
        )
        snapshot = rows[0].regeneration_snapshot

        response = self.client.post(
            "/api/v1/analysis/regenerate-simulation/",
            {
                "regeneration_snapshot": snapshot,
                "style_id": rows[0].style_id_snapshot,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")
        self.assertIsNone(response.data["recommendation_id"])
        self.assertEqual(response.data["style_id"], rows[0].style_id_snapshot)
        self.assertEqual(response.data["regeneration_remaining_count"], 0)


class RetryRecommendationFlowTests(APITestCase):
    def setUp(self):
        self._preexisting_tables = set(connection.introspection.table_names())
        with connection.cursor() as cursor:
            for ddl in LEGACY_TABLE_DDL:
                cursor.execute(ddl)
        _reset_legacy_table_caches()

    def tearDown(self):
        created_tables = [table for table in LEGACY_TABLES if table not in self._preexisting_tables]
        with connection.cursor() as cursor:
            for table in created_tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        _reset_legacy_table_caches()

    @mock.patch.dict(os.environ, {"MIRRAI_AI_PROVIDER": "local"}, clear=False)
    def test_current_recommendations_expose_single_retry_before_consultation(self):
        client = _create_assigned_runtime_client(name="Retry Ready", phone="01091919191", gender="F")
        _seed_generated_batch(
            client=client,
            target_length="long",
            target_vibe="natural",
            scalp_type="waved",
            hair_colour="brown",
            budget_range="10-15",
            face_shape="Oval",
            golden_ratio_score=0.9,
        )

        response = self.client.get(f"/api/v1/analysis/recommendations/?client_id={client.id}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["recommendation_stage"], "initial")
        self.assertTrue(response.data["can_retry_recommendations"])
        self.assertEqual(response.data["retry_recommendations_remaining_count"], 1)
        self.assertIn("retry_recommendations", response.data["next_actions"])

    @mock.patch.dict(os.environ, {"MIRRAI_AI_PROVIDER": "local"}, clear=False)
    def test_retry_recommendations_creates_retry_batch_and_disables_second_retry(self):
        client = _create_assigned_runtime_client(name="Retry Flow", phone="01092929292", gender="F")
        _seed_generated_batch(
            client=client,
            target_length="long",
            target_vibe="natural",
            scalp_type="waved",
            hair_colour="brown",
            budget_range="10-15",
            face_shape="Round",
            golden_ratio_score=0.78,
        )

        response = self.client.post(
            "/api/v1/analysis/retry-recommendations/",
            {"client_id": client.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["recommendation_stage"], "retry")
        self.assertFalse(response.data["can_retry_recommendations"])
        self.assertEqual(response.data["retry_recommendations_remaining_count"], 0)
        self.assertEqual(response.data["retry_recommendations_policy"]["preference_weight"], 70)
        self.assertEqual(response.data["retry_recommendations_policy"]["face_total_weight"], 30)
        self.assertEqual(response.data["next_actions"], ["consultation"])
        self.assertEqual(response.data["items"][0]["reasoning_snapshot"]["scoring_profile"], "retry_preference_dominant")

        second_response = self.client.post(
            "/api/v1/analysis/retry-recommendations/",
            {"client_id": client.id},
            format="json",
        )
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch.dict(os.environ, {"MIRRAI_AI_PROVIDER": "local"}, clear=False)
    def test_retry_recommendations_is_blocked_after_consultation_starts(self):
        client = _create_assigned_runtime_client(name="Retry Locked", phone="01093939393", gender="F")
        _, rows = _seed_generated_batch(
            client=client,
            target_length="medium",
            target_vibe="chic",
            scalp_type="straight",
            hair_colour="black",
            budget_range="10-15",
            face_shape="Oval",
            golden_ratio_score=0.88,
        )

        consult_response = self.client.post(
            "/api/v1/analysis/confirm/",
            {
                "client_id": client.id,
                "direct_consultation": True,
                "recommendation_id": rows[0].id,
                "source": "current_recommendations",
            },
            format="json",
        )
        self.assertEqual(consult_response.status_code, status.HTTP_200_OK)

        retry_response = self.client.post(
            "/api/v1/analysis/retry-recommendations/",
            {"client_id": client.id},
            format="json",
        )
        self.assertEqual(retry_response.status_code, status.HTTP_400_BAD_REQUEST)


class RefreshTokenEndpointTests(APITestCase):
    def setUp(self):
        self._preexisting_tables = set(connection.introspection.table_names())
        with connection.cursor() as cursor:
            for ddl in LEGACY_TABLE_DDL:
                cursor.execute(ddl)
        _reset_legacy_table_caches()

    def tearDown(self):
        created_tables = [table for table in LEGACY_TABLES if table not in self._preexisting_tables]
        with connection.cursor() as cursor:
            for table in created_tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        _reset_legacy_table_caches()

    def test_client_refresh_endpoint_returns_new_tokens(self):
        client = _create_runtime_client(name="Client Refresh", phone="01056565656", gender="F")
        refresh_token = build_client_refresh_token(client=client)

        response = self.client.post(
            "/api/v1/auth/refresh/",
            {"refresh_token": refresh_token},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["client_id"], client.id)
        self.assertIn("access_token", response.data)
        self.assertIn("refresh_token", response.data)
        self.assertGreater(response.data["refresh_expires_in"], response.data["expires_in"])

    def test_admin_refresh_endpoint_returns_new_tokens(self):
        admin = _create_runtime_admin(
            name="Refresh Admin",
            store_name="MirrAI Refresh",
            phone="01078787878",
            business_number="3333333333",
        )
        refresh_token = build_admin_refresh_token(admin=admin)

        response = self.client.post(
            "/api/v1/admin/auth/refresh/",
            {"refresh_token": refresh_token},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["admin_id"], admin.id)
        self.assertIn("access_token", response.data)
        self.assertIn("refresh_token", response.data)
        self.assertGreater(response.data["refresh_expires_in"], response.data["expires_in"])

    def test_client_refresh_validation_error_uses_compat_envelope(self):
        response = self.client.post(
            "/api/v1/auth/refresh/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error_code"], "validation_error")
        self.assertEqual(response.data["message"], "Validation failed.")
        self.assertIn("refresh_token", response.data["detail"])

    def test_client_refresh_invalid_token_uses_compat_envelope(self):
        response = self.client.post(
            "/api/v1/auth/refresh/",
            {"refresh_token": "invalid-token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["error_code"], "unauthorized")
        self.assertIn("message", response.data)
        self.assertIn("detail", response.data)

    def test_admin_login_validation_error_uses_compat_envelope(self):
        response = self.client.post(
            "/api/v1/admin/auth/login/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error_code"], "validation_error")
        self.assertEqual(response.data["message"], "Validation failed.")
        self.assertIsInstance(response.data["detail"], dict)

    def test_admin_login_blank_field_error_is_localized(self):
        response = self.client.post(
            "/api/v1/admin/auth/login/",
            {
                "phone": "",
                "password": "",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error_code"], "validation_error")
        self.assertEqual(response.data["errors"]["phone"][0], "필수 정보입니다.")

    def test_client_refresh_parse_error_uses_compat_envelope(self):
        response = self.client.generic(
            "POST",
            "/api/v1/auth/refresh/",
            data="{invalid-json",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error_code"], "parse_error")
        self.assertIn("message", response.data)
        self.assertIn("detail", response.data)
