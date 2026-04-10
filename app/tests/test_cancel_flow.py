import uuid

from django.db import connection
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app.models_model_team import LegacyClientResult, LegacyClientResultDetail, LegacyHairstyle
from app.services.model_team_bridge import get_legacy_client_id, upsert_client_record
from app.tests.test_legacy_model_sync import LEGACY_TABLE_DDL, LEGACY_TABLES


class CancelStyleSelectionApiTests(APITestCase):
    def setUp(self):
        self._preexisting_tables = set(connection.introspection.table_names())
        with connection.cursor() as cursor:
            for ddl in LEGACY_TABLE_DDL:
                cursor.execute(ddl)
        self.client_profile = upsert_client_record(
            name="Cancel Tester",
            phone="01012345678",
            gender="F",
        )
        self.style_id = 201
        LegacyHairstyle.objects.update_or_create(
            hairstyle_id=self.style_id,
            defaults={
                "chroma_id": str(self.style_id),
                "style_name": "Soft Layered Bob",
                "image_url": "styles/soft-layered-bob.jpg",
                "created_at": timezone.now().isoformat(),
                "backend_style_id": self.style_id,
                "name": "Soft Layered Bob",
                "vibe": "natural",
                "description": "Soft layered bob for cancel flow testing.",
            },
        )

    def tearDown(self):
        created_tables = [table for table in LEGACY_TABLES if table not in self._preexisting_tables]
        with connection.cursor() as cursor:
            for table in created_tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")

    def _create_active_selection(self):
        created_at = timezone.now()
        batch_id = uuid.uuid4()
        result = LegacyClientResult.objects.create(
            result_id=1,
            analysis_id=0,
            client_id=get_legacy_client_id(client=self.client_profile),
            selected_hairstyle_id=self.style_id,
            selected_image_url="captures/result.jpg",
            is_confirmed=True,
            created_at=created_at.isoformat(),
            updated_at=created_at.isoformat(),
            backend_selection_id=1,
            backend_consultation_id=1,
            backend_client_ref_id=self.client_profile.id,
            backend_admin_ref_id=getattr(self.client_profile, "shop_id", None),
            backend_designer_ref_id=getattr(self.client_profile, "designer_id", None),
            source="current_recommendations",
            survey_snapshot={},
            analysis_data_snapshot={},
            status="PENDING",
            is_active=True,
            is_read=False,
            closed_at=None,
            selected_recommendation_id=101,
        )
        selected_detail = LegacyClientResultDetail.objects.create(
            detail_id=101,
            result_id=result.result_id,
            hairstyle_id=self.style_id,
            rank=1,
            similarity_score=92.0,
            final_score=92.0,
            simulated_image_url="captures/result.jpg",
            recommendation_reason="Best match for testing.",
            backend_recommendation_id=101,
            backend_client_ref_id=self.client_profile.id,
            backend_capture_record_id=None,
            batch_id=batch_id,
            source="generated",
            style_name_snapshot="Soft Layered Bob",
            style_description_snapshot="Soft layered bob for cancel flow testing.",
            keywords_json=["natural"],
            sample_image_url="styles/soft-layered-bob.jpg",
            regeneration_snapshot=None,
            reasoning_snapshot={"source": "test"},
            is_chosen=True,
            chosen_at=created_at,
            is_sent_to_admin=True,
            sent_at=created_at,
            created_at_ts=None,
        )
        sibling_detail = LegacyClientResultDetail.objects.create(
            detail_id=102,
            result_id=result.result_id,
            hairstyle_id=self.style_id,
            rank=2,
            similarity_score=88.0,
            final_score=88.0,
            simulated_image_url="captures/result-2.jpg",
            recommendation_reason="Second option for testing.",
            backend_recommendation_id=102,
            backend_client_ref_id=self.client_profile.id,
            backend_capture_record_id=None,
            batch_id=batch_id,
            source="generated",
            style_name_snapshot="Soft Layered Bob",
            style_description_snapshot="Soft layered bob for cancel flow testing.",
            keywords_json=["natural"],
            sample_image_url="styles/soft-layered-bob.jpg",
            regeneration_snapshot=None,
            reasoning_snapshot={"source": "test"},
            is_chosen=False,
            chosen_at=None,
            is_sent_to_admin=True,
            sent_at=created_at,
            created_at_ts=None,
        )
        return result, selected_detail, sibling_detail

    def test_cancel_without_recommendation_id_uses_active_consultation_selection(self):
        consultation, selected_row, sibling_row = self._create_active_selection()

        response = self.client.post(
            "/api/v1/analysis/cancel/",
            {"client_id": self.client_profile.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "cancelled")
        self.assertEqual(response.data["next_action"], "client_input")

        selected_row.refresh_from_db()
        sibling_row.refresh_from_db()
        consultation.refresh_from_db()

        self.assertFalse(selected_row.is_chosen)
        self.assertIsNone(selected_row.chosen_at)
        self.assertFalse(selected_row.is_sent_to_admin)
        self.assertIsNone(selected_row.sent_at)
        self.assertFalse(sibling_row.is_chosen)
        self.assertFalse(sibling_row.is_sent_to_admin)
        self.assertIsNone(sibling_row.sent_at)
        self.assertFalse(consultation.is_active)
        self.assertFalse(consultation.is_confirmed)
        self.assertEqual(consultation.status, "CANCELLED")
        self.assertTrue(consultation.is_read)
        self.assertIsNone(consultation.selected_hairstyle_id)
        self.assertIsNone(consultation.selected_image_url)
        self.assertIsNone(consultation.selected_recommendation_id)
        self.assertIsNotNone(consultation.closed_at)

    def test_cancel_with_unknown_recommendation_id_returns_400(self):
        response = self.client.post(
            "/api/v1/analysis/cancel/",
            {
                "client_id": self.client_profile.id,
                "recommendation_id": 999999,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)
