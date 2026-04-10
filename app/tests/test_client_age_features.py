from datetime import date

from django.db import connection
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app.api.v1.admin_services import get_admin_trend_report
from app.api.v1.services_django import ensure_catalog_styles
from app.models_model_team import LegacyClient, LegacyClientResult, LegacyClientResultDetail
from app.services.age_profile import current_age_from_birth_year, estimate_birth_year_from_age
from app.services.model_team_bridge import get_client_by_identifier, get_legacy_client_id, upsert_client_record
from app.tests.test_legacy_model_sync import LEGACY_TABLE_DDL, LEGACY_TABLES


class ClientAgeFeatureTests(APITestCase):
    def setUp(self):
        self._preexisting_tables = set(connection.introspection.table_names())
        with connection.cursor() as cursor:
            for ddl in LEGACY_TABLE_DDL:
                cursor.execute(ddl)

    def tearDown(self):
        created_tables = [table for table in LEGACY_TABLES if table not in self._preexisting_tables]
        with connection.cursor() as cursor:
            for table in created_tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")

    def _create_client(self, *, name: str, phone: str, gender: str = "F", age_input: int | None = None):
        birth_year_estimate = timezone.localdate().year - age_input + 1 if age_input else None
        return upsert_client_record(
            name=name,
            phone=phone,
            gender=gender,
            age_input=age_input,
            birth_year_estimate=birth_year_estimate,
        )

    def _create_confirmed_selection(self, *, client, style_id: int):
        created_at = timezone.now()
        result_id = (LegacyClientResult.objects.order_by("-result_id").values_list("result_id", flat=True).first() or 0) + 1
        detail_id = (
            LegacyClientResultDetail.objects.order_by("-detail_id").values_list("detail_id", flat=True).first() or 0
        ) + 1
        legacy_client_id = get_legacy_client_id(client=client)
        LegacyClientResult.objects.create(
            result_id=result_id,
            analysis_id=0,
            client_id=legacy_client_id,
            selected_hairstyle_id=style_id,
            selected_image_url=f"https://example.com/styles/{style_id}.jpg",
            is_confirmed=True,
            created_at=created_at.isoformat(),
            updated_at=created_at.isoformat(),
            backend_selection_id=result_id,
            backend_consultation_id=result_id,
            backend_client_ref_id=client.id,
            backend_admin_ref_id=getattr(client, "shop_id", None),
            backend_designer_ref_id=getattr(client, "designer_id", None),
            source="current_recommendations",
            survey_snapshot={},
            analysis_data_snapshot={},
            status="PENDING",
            is_active=True,
            is_read=False,
            closed_at=None,
            selected_recommendation_id=detail_id,
        )
        LegacyClientResultDetail.objects.create(
            detail_id=detail_id,
            result_id=result_id,
            hairstyle_id=style_id,
            rank=1,
            similarity_score=90.0,
            final_score=90.0,
            simulated_image_url=f"https://example.com/styles/{style_id}.jpg",
            recommendation_reason="age trend test",
            backend_recommendation_id=detail_id,
            backend_client_ref_id=client.id,
            backend_capture_record_id=None,
            batch_id=None,
            source="current_recommendations",
            style_name_snapshot=f"Style {style_id}",
            style_description_snapshot="age trend test style",
            keywords_json=["trend"],
            sample_image_url=f"https://example.com/styles/{style_id}.jpg",
            regeneration_snapshot=None,
            reasoning_snapshot={"source": "test"},
            is_chosen=True,
            chosen_at=created_at,
            is_sent_to_admin=True,
            sent_at=created_at,
            created_at_ts=None,
        )

    def test_register_client_accepts_ages_alias_and_persists_age_profile(self):
        current_year = timezone.localdate().year

        response = self.client.post(
            "/api/v1/auth/register/",
            {
                "name": "Age Tester",
                "gender": "F",
                "phone": "01012345678",
                "ages": 26,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        client = get_client_by_identifier(identifier=response.data["client_id"])
        self.assertIsNotNone(client)
        legacy_client = LegacyClient.objects.get(backend_client_id=response.data["client_id"])
        self.assertEqual(client.age_input, 26)
        self.assertEqual(client.birth_year_estimate, current_year - 26 + 1)
        self.assertEqual(legacy_client.client_id, client.legacy_client_id)
        self.assertEqual(legacy_client.backend_client_id, client.id)
        self.assertEqual(response.data["age"], 26)
        self.assertEqual(response.data["age_decade"], "20대")
        self.assertEqual(response.data["age_segment"], "중반")
        self.assertEqual(response.data["age_group"], "20대 중반")

    def test_age_rolls_forward_on_new_year_without_db_rewrite(self):
        birth_year_estimate = estimate_birth_year_from_age(26, reference_date=date(2026, 12, 31))

        age_on_dec_31 = current_age_from_birth_year(birth_year_estimate, reference_date=date(2026, 12, 31))
        age_on_jan_1 = current_age_from_birth_year(birth_year_estimate, reference_date=date(2027, 1, 1))

        self.assertEqual(age_on_dec_31, 26)
        self.assertEqual(age_on_jan_1, 27)

    def test_trend_endpoint_scopes_to_client_age_group(self):
        ensure_catalog_styles()
        young_a = self._create_client(
            name="Young A",
            phone="01020000001",
            gender="F",
            age_input=23,
        )
        young_b = self._create_client(
            name="Young B",
            phone="01020000002",
            gender="F",
            age_input=21,
        )
        older = self._create_client(
            name="Older",
            phone="01020000003",
            gender="F",
            age_input=38,
        )

        self._create_confirmed_selection(client=young_a, style_id=201)
        self._create_confirmed_selection(client=young_b, style_id=201)
        self._create_confirmed_selection(client=older, style_id=204)

        response = self.client.get(f"/api/v1/analysis/trend/?days=30&client_id={young_a.id}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["trend_scope"], "age_group")
        self.assertEqual(response.data["age_profile"]["age_group"], "20대 초반")
        self.assertEqual(response.data["items"][0]["style_id"], 201)

    def test_admin_trend_report_supports_age_filters_and_breakdown(self):
        ensure_catalog_styles()
        twenties = self._create_client(
            name="Twenties",
            phone="01030000001",
            gender="F",
            age_input=24,
        )
        thirties = self._create_client(
            name="Thirties",
            phone="01030000002",
            gender="M",
            age_input=37,
        )

        self._create_confirmed_selection(client=twenties, style_id=201)
        self._create_confirmed_selection(client=thirties, style_id=204)

        report = get_admin_trend_report(days=30, filters={"age_decade": "20대"})

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["kpi"]["total_confirmations"], 1)
        self.assertEqual(report["age_decade_distribution"][0]["age_decade"], "20대")
        self.assertEqual(report["ranking"][0]["style_id"], 201)
