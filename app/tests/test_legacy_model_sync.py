from __future__ import annotations

import io
from unittest import mock

from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from app.models_django import (
    AdminAccount,
    CaptureRecord,
    ClientSessionNote,
    ConsultationRequest,
    Designer,
    FaceAnalysis,
    FormerRecommendation,
    Style,
    StyleSelection,
    Survey,
)
from app.api.v1.services_django import (
    cancel_style_selection,
    confirm_style_selection,
    get_current_recommendations,
    get_former_recommendations,
    get_latest_analysis,
    get_latest_capture,
    get_latest_survey,
    get_trend_recommendations,
    persist_generated_batch,
    regenerate_recommendation_simulation,
    retry_current_recommendations,
    upsert_survey,
)
from app.api.v1.admin_services import (
    close_consultation_session,
    create_client_note,
    get_active_client_sessions,
    get_all_clients,
    get_admin_dashboard_summary,
    get_client_detail,
    get_client_recommendation_report,
    get_admin_trend_report,
    register_admin,
)
from app.api.v1.admin_serializers import ConsultationCloseSerializer, ConsultationNoteCreateSerializer
from app.api.v1.admin_serializers import DesignerSerializer
from app.api.v1.django_serializers import (
    ClientSerializer,
    FaceAnalysisSerializer,
    ConsultationRequestSerializer,
    FormerRecommendationSerializer,
    RetryRecommendationRequestSerializer,
    StyleSelectionSerializer,
)
from app.services.model_team_bridge import (
    get_admin_by_phone,
    get_client_by_phone,
    get_designers_for_admin,
    get_legacy_admin_id,
    get_legacy_confirmed_selection_items,
    get_legacy_client_id,
    get_legacy_designer_id,
    get_legacy_former_recommendation_items,
    get_scoped_client_ids,
)
from app.session_state import (
    ADMIN_ID_SESSION_KEY,
    ADMIN_LEGACY_ID_SESSION_KEY,
    CUSTOMER_ID_SESSION_KEY,
    CUSTOMER_LEGACY_ID_SESSION_KEY,
    DESIGNER_ID_SESSION_KEY,
    DESIGNER_LEGACY_ID_SESSION_KEY,
    get_session_admin,
    get_session_customer,
    get_session_designer,
)


LEGACY_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS shop (
        shop_id TEXT PRIMARY KEY,
        login_id TEXT NOT NULL,
        shop_name TEXT NOT NULL,
        biz_number TEXT,
        owner_phone TEXT,
        password TEXT NOT NULL,
        admin_pin TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        backend_admin_id INTEGER,
        name TEXT,
        store_name TEXT,
        role TEXT,
        phone TEXT,
        business_number TEXT,
        password_hash TEXT,
        is_active BOOLEAN,
        consent_snapshot TEXT,
        consented_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS designer (
        designer_id TEXT PRIMARY KEY,
        shop_id TEXT NOT NULL,
        designer_name TEXT NOT NULL,
        login_id TEXT NOT NULL,
        password TEXT NOT NULL,
        is_active BOOLEAN NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        backend_designer_id INTEGER,
        backend_shop_ref_id INTEGER,
        name TEXT,
        phone TEXT,
        pin_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client (
        client_id TEXT PRIMARY KEY,
        shop_id TEXT NOT NULL,
        client_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        gender TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        backend_client_id INTEGER,
        backend_shop_ref_id INTEGER,
        backend_designer_ref_id INTEGER,
        name TEXT,
        assigned_at TEXT,
        assignment_source TEXT,
        age_input INTEGER,
        birth_year_estimate INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_survey (
        survey_id INTEGER PRIMARY KEY,
        client_id TEXT NOT NULL,
        hair_length TEXT,
        hair_mood TEXT,
        hair_condition TEXT,
        hair_color TEXT,
        budget TEXT,
        preference_vector TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        backend_survey_id INTEGER,
        backend_client_ref_id INTEGER,
        target_length TEXT,
        target_vibe TEXT,
        scalp_type TEXT,
        hair_colour TEXT,
        budget_range TEXT,
        preference_vector_json TEXT,
        created_at_ts TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_analysis (
        analysis_id INTEGER PRIMARY KEY,
        client_id TEXT NOT NULL,
        designer_id TEXT NOT NULL,
        original_image_url TEXT,
        face_type TEXT,
        face_ratio_vector TEXT NOT NULL,
        golden_ratio_score REAL,
        landmark_data TEXT,
        created_at TEXT NOT NULL,
        backend_analysis_id INTEGER,
        backend_client_ref_id INTEGER,
        backend_designer_ref_id INTEGER,
        backend_capture_record_id INTEGER,
        processed_path TEXT,
        filename TEXT,
        status TEXT,
        face_count INTEGER,
        error_note TEXT,
        updated_at_ts TEXT,
        deidentified_path TEXT,
        capture_landmark_snapshot TEXT,
        privacy_snapshot TEXT,
        analysis_image_url TEXT,
        analysis_landmark_snapshot TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_result (
        result_id INTEGER PRIMARY KEY,
        analysis_id INTEGER NOT NULL,
        client_id TEXT NOT NULL,
        selected_hairstyle_id INTEGER,
        selected_image_url TEXT,
        is_confirmed BOOLEAN NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        backend_selection_id INTEGER,
        backend_consultation_id INTEGER,
        backend_client_ref_id INTEGER,
        backend_admin_ref_id INTEGER,
        backend_designer_ref_id INTEGER,
        source TEXT,
        survey_snapshot TEXT,
        analysis_data_snapshot TEXT,
        status TEXT,
        is_active BOOLEAN,
        is_read BOOLEAN,
        closed_at TEXT,
        selected_recommendation_id INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_result_detail (
        detail_id INTEGER PRIMARY KEY,
        result_id INTEGER NOT NULL,
        hairstyle_id INTEGER NOT NULL,
        rank INTEGER NOT NULL,
        similarity_score REAL NOT NULL,
        final_score REAL,
        simulated_image_url TEXT,
        recommendation_reason TEXT,
        backend_recommendation_id INTEGER,
        backend_client_ref_id INTEGER,
        backend_capture_record_id INTEGER,
        batch_id TEXT,
        source TEXT,
        style_name_snapshot TEXT,
        style_description_snapshot TEXT,
        keywords_json TEXT,
        sample_image_url TEXT,
        regeneration_snapshot TEXT,
        reasoning_snapshot TEXT,
        is_chosen BOOLEAN,
        chosen_at TEXT,
        is_sent_to_admin BOOLEAN,
        sent_at TEXT,
        created_at_ts TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hairstyle (
        hairstyle_id INTEGER PRIMARY KEY,
        chroma_id TEXT NOT NULL,
        style_name TEXT NOT NULL,
        image_url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        backend_style_id INTEGER,
        name TEXT,
        vibe TEXT,
        description TEXT
    )
    """,
)

LEGACY_TABLES = (
    "client_result_detail",
    "client_result",
    "client_analysis",
    "client_survey",
    "client",
    "designer",
    "shop",
    "hairstyle",
)


@override_settings(SUPABASE_USE_REMOTE_STORAGE=False)
class LegacyModelSyncTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.factory = RequestFactory()
        self._preexisting_tables = set(connection.introspection.table_names())
        self._create_legacy_tables()

    def tearDown(self):
        self._drop_legacy_tables()

    def _create_legacy_tables(self):
        with connection.cursor() as cursor:
            for ddl in LEGACY_TABLE_DDL:
                cursor.execute(ddl)

    def _drop_legacy_tables(self):
        created_tables = [table for table in LEGACY_TABLES if table not in self._preexisting_tables]
        with connection.cursor() as cursor:
            for table in created_tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")

    def _count(self, table: str) -> int:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            return int(cursor.fetchone()[0])

    def _fetch_one(self, sql: str, params: tuple | list = ()):
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    def _table_has_column(self, table: str, column: str) -> bool:
        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(cursor, table)
        return any(field.name == column for field in description)

    def _build_request_with_session(self):
        request = self.factory.get("/")
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()
        return request

    def _seeded_admin(self):
        admin = get_admin_by_phone(phone="01080001000")
        self.assertIsNotNone(admin)
        return admin

    def _seeded_client(self, phone: str):
        client = get_client_by_phone(phone=phone)
        self.assertIsNotNone(client)
        return client

    def _seeded_designer(self, admin=None):
        admin = admin or self._seeded_admin()
        designers = get_designers_for_admin(admin=admin)
        self.assertTrue(designers)
        return designers[0]

    def _set_admin_client_session(self, admin):
        session = self.client.session
        session["admin_id"] = admin.id
        session["admin_legacy_id"] = get_legacy_admin_id(admin=admin)
        session["admin_name"] = admin.name
        session.save()

    def _insert_legacy_source_rows(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shop (
                    shop_id, login_id, shop_name, biz_number, owner_phone, password, admin_pin, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "shop-1",
                    "01080001000",
                    "Model Team Shop",
                    "1012345672",
                    "01080001000",
                    "hashed-password",
                    "1000",
                    "2026-04-01T00:00:00",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO designer (
                    designer_id, shop_id, designer_name, login_id, password, is_active, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "designer-1",
                    "shop-1",
                    "Kim Mina",
                    "01081112001",
                    "hashed-pin",
                    True,
                    "2026-04-01T00:00:00",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO client (
                    client_id, shop_id, client_name, phone, gender, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "client-1",
                    "shop-1",
                    "Choi Hana",
                    "01090001001",
                    "F",
                    "2026-04-01T00:00:00",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO client_survey (
                    survey_id, client_id, hair_length, hair_mood, hair_condition, hair_color, budget, preference_vector, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    1,
                    "client-1",
                    "long",
                    "elegant",
                    "waved",
                    "brown",
                    "10_20",
                    "[0.7, 0.2, 0.1]",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO hairstyle (
                    hairstyle_id, chroma_id, style_name, image_url, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    301,
                    "301",
                    "Soft Layer Perm",
                    "https://example.com/styles/301.jpg",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO hairstyle (
                    hairstyle_id, chroma_id, style_name, image_url, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    302,
                    "302",
                    "Classic Bob",
                    "https://example.com/styles/302.jpg",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO client_analysis (
                    analysis_id, client_id, designer_id, original_image_url, face_type, face_ratio_vector, golden_ratio_score, landmark_data, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    11,
                    "client-1",
                    "designer-1",
                    "https://example.com/captures/client-1.jpg",
                    "oval",
                    "[0.45, 0.33, 0.22]",
                    0.93,
                    "{\"jaw\":\"soft\",\"nose\":\"balanced\"}",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO client_result (
                    result_id, analysis_id, client_id, selected_hairstyle_id, selected_image_url, is_confirmed, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    21,
                    11,
                    "client-1",
                    301,
                    "https://example.com/results/client-1-selected.jpg",
                    True,
                    "2026-04-01T00:00:00",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO client_result_detail (
                    detail_id, result_id, hairstyle_id, rank, similarity_score, final_score, simulated_image_url, recommendation_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    101,
                    21,
                    301,
                    1,
                    0.91,
                    0.95,
                    "https://example.com/results/client-1-style-301.jpg",
                    "Best for soft oval balance",
                ),
            )
            cursor.execute(
                """
                INSERT INTO client_result_detail (
                    detail_id, result_id, hairstyle_id, rank, similarity_score, final_score, simulated_image_url, recommendation_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    102,
                    21,
                    302,
                    2,
                    0.84,
                    0.88,
                    "https://example.com/results/client-1-style-302.jpg",
                    "Safer shorter fallback",
                ),
            )

    def test_seed_command_populates_legacy_model_tables(self):
        call_command("seed_test_accounts")

        self.assertEqual(self._count("shop"), 1)
        self.assertEqual(self._count("designer"), 2)
        self.assertEqual(self._count("client"), 4)
        self.assertEqual(self._count("client_survey"), 4)
        self.assertEqual(self._count("client_analysis"), 3)
        self.assertEqual(self._count("client_result"), 3)
        self.assertEqual(self._count("client_result_detail"), 15)
        self.assertGreaterEqual(self._count("hairstyle"), 1)

        shop_row = self._fetch_one(
            "SELECT login_id, shop_name, biz_number, owner_phone, admin_pin FROM shop LIMIT 1"
        )
        self.assertEqual(shop_row[0], "01080001000")
        self.assertEqual(shop_row[1], "MirrAI Test Shop")
        self.assertEqual(shop_row[2], "1012345672")
        self.assertEqual(shop_row[3], "01080001000")
        self.assertEqual(shop_row[4], "1000")

        client_row = self._fetch_one(
            "SELECT client_name, phone, gender FROM client WHERE phone = %s",
            ("01090001004",),
        )
        self.assertIsNotNone(client_row)
        self.assertEqual(client_row[1], "01090001004")
        self.assertEqual(client_row[2], "F")

    def test_explicit_sync_command_runs_after_seed(self):
        call_command("seed_test_accounts")
        call_command("sync_legacy_model_tables", strict=True)

        self.assertEqual(self._count("shop"), 1)
        self.assertEqual(self._count("client_result_detail"), 15)

    def test_explicit_sync_command_keeps_seeded_client_table_as_source_of_truth(self):
        with mock.patch("app.management.commands.seed_test_accounts.Command._upsert_consultation_notes"):
            call_command("seed_test_accounts")

        call_command("sync_legacy_model_tables", strict=True)

        self.assertEqual(self._count("client"), 4)
        self.assertEqual(
            self._fetch_one(
                "SELECT COUNT(*) FROM client WHERE backend_client_id IS NOT NULL",
            )[0],
            4,
        )

    def test_model_team_bridge_uses_legacy_tables_for_low_risk_reads(self):
        call_command("seed_test_accounts")

        admin = self._seeded_admin()
        designers = get_designers_for_admin(admin=admin)
        scoped_client_ids = get_scoped_client_ids(admin=admin)

        resolved_admin = get_admin_by_phone(phone="01080001000")
        self.assertIsNotNone(resolved_admin)
        self.assertEqual(resolved_admin.id, admin.id)
        self.assertEqual(resolved_admin.phone, admin.phone)
        self.assertEqual([designer.name for designer in designers], ["김미나", "박준"])
        self.assertEqual(sorted(scoped_client_ids or []), [1, 2, 3, 4])

    def test_register_admin_blocks_duplicate_values_from_legacy_shop_rows(self):
        self._insert_legacy_source_rows()

        with self.assertRaisesMessage(ValueError, "이미 등록된 관리자 연락처입니다."):
            register_admin(
                payload={
                    "name": "Legacy Duplicate",
                    "store_name": "Legacy Duplicate Shop",
                    "role": "owner",
                    "phone": "010-8000-1000",
                    "business_number": "9234567805",
                    "password": "pw1234!!",
                    "agree_terms": True,
                    "agree_privacy": True,
                    "agree_third_party_sharing": True,
                }
            )

        with self.assertRaisesMessage(ValueError, "이미 등록된 사업자등록번호입니다."):
            register_admin(
                payload={
                    "name": "Legacy Duplicate",
                    "store_name": "Legacy Duplicate Shop",
                    "role": "owner",
                    "phone": "010-8111-0000",
                    "business_number": "1012345672",
                    "password": "pw1234!!",
                    "agree_terms": True,
                    "agree_privacy": True,
                    "agree_third_party_sharing": True,
                }
            )

    def test_session_state_can_restore_from_legacy_ids_only(self):
        call_command("seed_test_accounts")

        admin = self._seeded_admin()
        designer = self._seeded_designer(admin=admin)
        client = self._seeded_client("01090001001")
        request = self._build_request_with_session()

        request.session[ADMIN_LEGACY_ID_SESSION_KEY] = get_legacy_admin_id(admin=admin)
        request.session[DESIGNER_LEGACY_ID_SESSION_KEY] = get_legacy_designer_id(designer=designer)
        request.session[CUSTOMER_LEGACY_ID_SESSION_KEY] = get_legacy_client_id(client=client)
        request.session.pop(ADMIN_ID_SESSION_KEY, None)
        request.session.pop(DESIGNER_ID_SESSION_KEY, None)
        request.session.pop(CUSTOMER_ID_SESSION_KEY, None)

        restored_admin = get_session_admin(request=request)
        restored_designer = get_session_designer(request=request)
        restored_client = get_session_customer(request=request)
        self.assertIsNotNone(restored_admin)
        self.assertIsNotNone(restored_designer)
        self.assertIsNotNone(restored_client)
        self.assertEqual(restored_admin.id, admin.id)
        self.assertEqual(restored_designer.id, designer.id)
        self.assertEqual(restored_client.id, client.id)

    def test_services_can_read_survey_analysis_and_history_from_legacy_tables(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001001")
        Survey.objects.filter(client_id=client.id).delete()
        FaceAnalysis.objects.filter(client_id=client.id).delete()
        FormerRecommendation.objects.filter(client_id=client.id).delete()

        survey = get_latest_survey(client)
        analysis = get_latest_analysis(client)
        former_payload = get_former_recommendations(client)
        current_payload = get_current_recommendations(client)

        self.assertIsNotNone(survey)
        self.assertEqual(getattr(survey, "target_length", None), "long")
        self.assertIsNotNone(analysis)
        self.assertIsNotNone(getattr(analysis, "face_shape", None))
        self.assertEqual(former_payload["status"], "ready")
        self.assertEqual(former_payload["client_id"], client.id)
        self.assertEqual(former_payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertTrue(former_payload["items"])
        self.assertEqual(current_payload["status"], "ready")
        self.assertEqual(current_payload["client_id"], client.id)
        self.assertEqual(current_payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertTrue(current_payload["items"])

    def test_latest_entrypoints_prefer_legacy_rows_even_when_canonical_exists(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001001")
        legacy_client_id = get_legacy_client_id(client=client)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE client_survey
                SET target_length = %s
                WHERE client_id = %s
                """,
                ["legacy-length", legacy_client_id],
            )
            cursor.execute(
                """
                UPDATE client_analysis
                SET face_type = %s,
                    processed_path = %s
                WHERE client_id = %s
                """,
                ["legacy-face-shape", "https://example.com/legacy-capture.jpg", legacy_client_id],
            )

        survey = get_latest_survey(client)
        analysis = get_latest_analysis(client)
        capture = get_latest_capture(client)

        self.assertEqual(getattr(survey, "target_length", None), "legacy-length")
        self.assertEqual(getattr(analysis, "face_shape", None), "legacy-face-shape")
        self.assertEqual(getattr(capture, "processed_path", None), "https://example.com/legacy-capture.jpg")

    def test_latest_survey_analysis_and_capture_do_not_fallback_to_canonical_when_legacy_tables_exist(self):
        client = get_client_by_phone(phone="01093334444")
        self.assertIsNone(client)

        client = type(
            "RuntimeOnlyClient",
            (),
            {
                "id": 999999,
                "legacy_client_id": "missing-legacy-client",
                "shop_id": None,
                "designer_id": None,
                "name": "Runtime Only Client",
                "phone": "01093334444",
            },
        )()

        self.assertIsNone(get_latest_survey(client))
        self.assertIsNone(get_latest_analysis(client))
        self.assertIsNone(get_latest_capture(client))

    def test_capture_status_endpoint_can_fallback_to_legacy_capture_by_client_id(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001001")
        CaptureRecord.objects.filter(client_id=client.id).delete()

        response = self.client.get(f"/api/v1/capture/status/?client_id={get_legacy_client_id(client=client)}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertEqual(payload["status"], "done")
        self.assertIn("storage_snapshot", payload)

    def test_capture_status_endpoint_can_fallback_to_legacy_capture_by_record_id(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001001")
        legacy_capture = get_latest_capture(client)
        self.assertIsNotNone(legacy_capture)
        CaptureRecord.objects.filter(client_id=client.id).delete()

        response = self.client.get(f"/api/v1/capture/status/?record_id={legacy_capture.id}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertEqual(payload["status"], "done")

    def test_dashboard_and_sessions_can_fallback_to_legacy_consultation_and_selection_rows(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        StyleSelection.objects.all().delete()
        ConsultationRequest.objects.all().delete()

        dashboard = get_admin_dashboard_summary(admin=shop)
        sessions = get_active_client_sessions(admin=shop)

        self.assertEqual(dashboard["status"], "ready")
        self.assertGreaterEqual(dashboard["today_metrics"]["confirmed_styles"], 1)
        self.assertTrue(dashboard["top_styles_today"])
        self.assertGreaterEqual(dashboard["today_metrics"]["active_clients"], 1)
        self.assertTrue(sessions["items"])
        self.assertIn("legacy_client_id", sessions["items"][0])

    def test_trend_reports_can_fallback_to_legacy_selection_rows(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        StyleSelection.objects.all().delete()

        admin_report = get_admin_trend_report(days=30, admin=shop)
        customer_trend = get_trend_recommendations(days=30, client=client)

        self.assertEqual(admin_report["status"], "ready")
        self.assertTrue(admin_report["ranking"])
        self.assertGreaterEqual(admin_report["kpi"]["total_confirmations"], 1)
        self.assertEqual(customer_trend["status"], "ready")
        self.assertEqual(customer_trend["client_id"], client.id)
        self.assertEqual(customer_trend["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertTrue(customer_trend["items"])

    def test_legacy_trend_report_view_can_build_visitor_stats_from_legacy_activity(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        CaptureRecord.objects.all().delete()
        ConsultationRequest.objects.all().delete()
        self._set_admin_client_session(shop)

        response = self.client.get("/api/v1/analysis/report/?days=7")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("visitor_stats", payload)
        self.assertEqual(len(payload["visitor_stats"]), 7)
        self.assertGreater(sum(item["count"] for item in payload["visitor_stats"]), 0)

    def test_persist_generated_batch_updates_legacy_result_tables_immediately(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        legacy_client_id = get_legacy_client_id(client=client)
        capture = CaptureRecord.objects.filter(client_id=client.id, status="DONE").order_by("-created_at", "-id").first()
        survey = get_latest_survey(client)
        analysis = get_latest_analysis(client)

        FormerRecommendation.objects.filter(client_id=client.id).delete()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM client_result_detail")
            cursor.execute("DELETE FROM client_result WHERE client_id = %s", [legacy_client_id])

        batch_id, rows = persist_generated_batch(
            client=client,
            capture_record=capture,
            survey=survey,
            analysis=analysis,
        )

        self.assertTrue(batch_id)
        self.assertEqual(len(rows), 5)
        self.assertEqual(FormerRecommendation.objects.filter(client_id=client.id).count(), 0)

        result_count = self._fetch_one(
            "SELECT COUNT(*) FROM client_result WHERE client_id = %s",
            [legacy_client_id],
        )[0]
        detail_count = self._fetch_one(
            """
            SELECT COUNT(*)
            FROM client_result_detail
            WHERE result_id IN (
                SELECT result_id FROM client_result WHERE client_id = %s
            )
            """,
            [legacy_client_id],
        )[0]

        self.assertEqual(result_count, 1)
        self.assertEqual(detail_count, 5)

    def test_upsert_survey_updates_only_target_legacy_survey_row(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        Survey.objects.filter(client_id=client.id).delete()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO client_survey (
                    survey_id, client_id, hair_length, hair_mood, hair_condition, hair_color, budget, preference_vector, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    909092,
                    "foreign-client",
                    "short",
                    "chic",
                    "straight",
                    "black",
                    "0_10",
                    "[0.1, 0.1, 0.1]",
                    "2026-04-01T00:00:00",
                ),
            )

        survey = upsert_survey(
            client,
            {
                "target_length": "long",
                "target_vibe": "elegant",
                "scalp_type": "waved",
                "hair_colour": "brown",
                "budget_range": "10_20",
            },
        )

        sentinel_count = self._fetch_one(
            "SELECT COUNT(*) FROM client_survey WHERE survey_id = %s",
            [909092],
        )[0]
        target_row = self._fetch_one(
            """
            SELECT client_id, target_length, target_vibe, scalp_type, hair_colour, budget_range
            FROM client_survey
            WHERE survey_id = %s
            """,
            [survey.id],
        )

        self.assertEqual(sentinel_count, 1)
        self.assertIsNotNone(target_row)
        self.assertEqual(target_row[0], get_legacy_client_id(client=client))
        self.assertEqual(target_row[1], "long")
        self.assertEqual(target_row[2], "elegant")
        self.assertEqual(target_row[3], "waved")
        self.assertEqual(target_row[4], "brown")
        self.assertEqual(target_row[5], "10_20")
        self.assertEqual(Survey.objects.filter(client_id=client.id).count(), 0)

    def test_runtime_state_update_keeps_unrelated_legacy_result_rows(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        capture = CaptureRecord.objects.filter(client_id=client.id, status="DONE").order_by("-created_at", "-id").first()
        survey = get_latest_survey(client)
        analysis = get_latest_analysis(client)

        FormerRecommendation.objects.filter(client_id=client.id).delete()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO client_result (
                    result_id, analysis_id, client_id, selected_hairstyle_id, selected_image_url, is_confirmed, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    909090,
                    999,
                    "foreign-client",
                    301,
                    "https://example.com/results/foreign-selected.jpg",
                    False,
                    "2026-04-01T00:00:00",
                    "2026-04-01T00:00:00",
                ),
            )
            cursor.execute(
                """
                INSERT INTO client_result_detail (
                    detail_id, result_id, hairstyle_id, rank, similarity_score, final_score, simulated_image_url, recommendation_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    909091,
                    909090,
                    301,
                    1,
                    0.75,
                    0.75,
                    "https://example.com/results/foreign-style.jpg",
                    "foreign sentinel row",
                ),
            )

        persist_generated_batch(
            client=client,
            capture_record=capture,
            survey=survey,
            analysis=analysis,
        )

        sentinel_result = self._fetch_one(
            "SELECT COUNT(*) FROM client_result WHERE result_id = %s",
            [909090],
        )[0]
        sentinel_detail = self._fetch_one(
            "SELECT COUNT(*) FROM client_result_detail WHERE detail_id = %s",
            [909091],
        )[0]

        self.assertEqual(sentinel_result, 1)
        self.assertEqual(sentinel_detail, 1)

    def test_current_recommendations_reuses_legacy_rows_without_regenerating_canonical_batch(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        legacy_items = get_legacy_former_recommendation_items(client=client)
        self.assertTrue(legacy_items)
        FormerRecommendation.objects.filter(client_id=client.id).delete()

        payload = get_current_recommendations(client)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["retry_state"], "available")
        self.assertEqual(payload["items"][0]["style_name"], legacy_items[0]["style_name"])
        self.assertEqual(FormerRecommendation.objects.filter(client_id=client.id).count(), 0)

    def test_retry_current_recommendations_can_run_with_legacy_only_generated_rows(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        FormerRecommendation.objects.filter(client_id=client.id).delete()
        ConsultationRequest.objects.filter(client_id=client.id).delete()
        StyleSelection.objects.filter(client_id=client.id).delete()

        payload = retry_current_recommendations(client)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["recommendation_stage"], "retry")
        self.assertEqual(FormerRecommendation.objects.filter(client_id=client.id).count(), 0)
        self.assertTrue(payload["items"])

    def test_regenerate_simulation_can_bridge_from_legacy_only_recommendation(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        legacy_item = (get_legacy_former_recommendation_items(client=client) or [None])[0]
        self.assertIsNotNone(legacy_item)
        FormerRecommendation.objects.filter(client_id=client.id).delete()

        payload = regenerate_recommendation_simulation(
            recommendation_id=int(legacy_item["recommendation_id"]),
        )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["style_id"], legacy_item["style_id"])
        self.assertFalse(payload["can_regenerate_simulation"])
        self.assertEqual(payload["recommendation_id"], int(legacy_item["recommendation_id"]))
        self.assertEqual(FormerRecommendation.objects.filter(client_id=client.id).count(), 0)

    def test_confirm_style_selection_updates_legacy_result_state_immediately(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001003")
        recommendation = get_legacy_former_recommendation_items(client=client)[0]

        payload = confirm_style_selection(
            client=client,
            recommendation_id=int(recommendation["recommendation_id"]),
            admin_id=get_legacy_admin_id(admin=shop),
            source="current_recommendations",
        )

        legacy_client_id = get_legacy_client_id(client=client)
        result_row = self._fetch_one(
            """
            SELECT selected_hairstyle_id, is_confirmed
            FROM client_result
            WHERE client_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            [legacy_client_id],
        )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["client_id"], client.id)
        self.assertEqual(payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertIsNotNone(result_row)
        self.assertEqual(int(result_row[0]), recommendation["style_id"])
        self.assertTrue(bool(result_row[1]))

    def test_current_recommendations_and_retry_respect_legacy_active_consultation_without_canonical_rows(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        legacy_client_id = get_legacy_client_id(client=client)
        ConsultationRequest.objects.filter(client_id=client.id).delete()
        StyleSelection.objects.filter(client_id=client.id).delete()

        chosen_style_id = self._fetch_one(
            """
            SELECT hairstyle_id
            FROM client_result_detail
            WHERE result_id IN (
                SELECT result_id
                FROM client_result
                WHERE client_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
            )
            ORDER BY rank, detail_id
            LIMIT 1
            """,
            [legacy_client_id],
        )[0]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE client_result
                SET selected_hairstyle_id = %s,
                    is_confirmed = %s
                WHERE client_id = %s
                """,
                [chosen_style_id, True, legacy_client_id],
            )

        payload = get_current_recommendations(client)

        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["consultation_locked"])
        self.assertEqual(payload["retry_state"], "consultation_locked")
        with self.assertRaisesMessage(
            ValueError,
            "Retry is not available after the consultation flow has started.",
        ):
            retry_current_recommendations(client)

    def test_confirm_and_cancel_can_bridge_from_legacy_recommendation_without_canonical_row(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        legacy_item = get_legacy_former_recommendation_items(client=client)[0]
        FormerRecommendation.objects.filter(client_id=client.id).delete()
        StyleSelection.objects.filter(client_id=client.id).delete()
        ConsultationRequest.objects.filter(client_id=client.id).delete()

        confirm_payload = confirm_style_selection(
            client=client,
            recommendation_id=int(legacy_item["recommendation_id"]),
            source="current_recommendations",
        )

        self.assertEqual(confirm_payload["status"], "success")
        self.assertEqual(confirm_payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertEqual(FormerRecommendation.objects.filter(client_id=client.id).count(), 0)
        self.assertEqual(StyleSelection.objects.filter(client_id=client.id).count(), 0)
        self.assertEqual(ConsultationRequest.objects.filter(client_id=client.id).count(), 0)
        active_result = self._fetch_one(
            """
            SELECT is_active, is_confirmed, selected_recommendation_id
            FROM client_result
            WHERE client_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            [get_legacy_client_id(client=client)],
        )
        self.assertIsNotNone(active_result)
        self.assertTrue(bool(active_result[0]))
        self.assertTrue(bool(active_result[1]))
        self.assertEqual(int(active_result[2]), int(legacy_item["recommendation_id"]))

        cancel_payload = cancel_style_selection(
            client=client,
            recommendation_id=int(legacy_item["recommendation_id"]),
            source="current_recommendations",
        )

        self.assertEqual(cancel_payload["status"], "cancelled")
        self.assertEqual(cancel_payload["legacy_client_id"], get_legacy_client_id(client=client))
        cancelled_result = self._fetch_one(
            """
            SELECT is_active, status, is_confirmed
            FROM client_result
            WHERE client_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            [get_legacy_client_id(client=client)],
        )
        self.assertEqual(FormerRecommendation.objects.filter(client_id=client.id).count(), 0)
        self.assertEqual(StyleSelection.objects.filter(client_id=client.id).count(), 0)
        self.assertEqual(ConsultationRequest.objects.filter(client_id=client.id).count(), 0)
        self.assertIsNotNone(cancelled_result)
        self.assertFalse(bool(cancelled_result[0]))
        self.assertEqual(str(cancelled_result[1]), "CANCELLED")
        self.assertFalse(bool(cancelled_result[2]))

    def test_serializers_expose_legacy_ids_and_accept_legacy_identifier_input(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001001")
        recommendation = get_legacy_former_recommendation_items(client=client)[0]
        consultation = next(
            (
                item
                for item in (get_active_client_sessions(admin=client.shop)["items"] or [])
                if item.get("legacy_client_id") == get_legacy_client_id(client=client)
            ),
            None,
        )

        client_payload = ClientSerializer(client).data
        recommendation_payload = FormerRecommendationSerializer(recommendation).data
        consultation_payload = ConsultationRequestSerializer(consultation).data
        retry_serializer = RetryRecommendationRequestSerializer(
            data={"client_id": get_legacy_client_id(client=client)}
        )

        self.assertEqual(client_payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertEqual(recommendation_payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertEqual(consultation_payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertTrue(retry_serializer.is_valid(), retry_serializer.errors)

    def test_serializers_can_represent_legacy_rows_directly(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        CaptureRecord.objects.filter(client_id=client.id).delete()
        FaceAnalysis.objects.filter(client_id=client.id).delete()
        StyleSelection.objects.filter(client_id=client.id).delete()
        ConsultationRequest.objects.filter(client_id=client.id).delete()

        legacy_analysis = get_latest_analysis(client)
        legacy_selection = (get_legacy_confirmed_selection_items(admin=shop, client=client) or [None])[0]
        legacy_consultation = next(
            (
                item
                for item in (get_active_client_sessions(admin=shop)["items"] or [])
                if item.get("legacy_client_id") == get_legacy_client_id(client=client)
            ),
            None,
        )

        self.assertIsNotNone(legacy_analysis)
        self.assertIsNotNone(legacy_selection)
        self.assertIsNotNone(legacy_consultation)

        analysis_payload = FaceAnalysisSerializer(legacy_analysis).data
        selection_payload = StyleSelectionSerializer(legacy_selection).data
        consultation_payload = ConsultationRequestSerializer(legacy_consultation).data

        self.assertEqual(analysis_payload["face_shape"], getattr(legacy_analysis, "face_shape", None))
        self.assertEqual(selection_payload["legacy_client_id"], get_legacy_client_id(client=client))
        self.assertEqual(consultation_payload["legacy_client_id"], get_legacy_client_id(client=client))

    def test_client_and_designer_serializers_can_represent_legacy_payloads(self):
        client_payload = ClientSerializer(
            {
                "client_id": 77,
                "legacy_client_id": "client-legacy-77",
                "client_name": "Legacy Client",
                "gender": "female",
                "phone": "01011112222",
                "age_input": 29,
                "created_at": timezone.now(),
            }
        ).data
        designer_payload = DesignerSerializer(
            {
                "backend_designer_id": 88,
                "designer_id": "designer-legacy-88",
                "designer_name": "Legacy Designer",
                "login_id": "01099990000",
                "is_active": True,
            }
        ).data

        self.assertEqual(client_payload["legacy_client_id"], "client-legacy-77")
        self.assertEqual(client_payload["current_age"], 29)
        self.assertEqual(designer_payload["legacy_id"], "designer-legacy-88")
        self.assertEqual(designer_payload["name"], "Legacy Designer")

    def test_client_detail_and_recommendation_report_can_fallback_to_legacy_rows(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        CaptureRecord.objects.filter(client_id=client.id).delete()
        FaceAnalysis.objects.filter(client_id=client.id).delete()
        FormerRecommendation.objects.filter(client_id=client.id).delete()
        StyleSelection.objects.filter(client_id=client.id).delete()
        ConsultationRequest.objects.filter(client_id=client.id).delete()

        detail = get_client_detail(client=client, admin=shop)
        report = get_client_recommendation_report(client=client, admin=shop)

        self.assertEqual(detail["status"], "ready")
        self.assertTrue(detail["capture_history"])
        self.assertTrue(detail["analysis_history"])
        self.assertTrue(detail["style_selection_history"])
        self.assertTrue(detail["chosen_recommendation_history"])
        self.assertIsNotNone(detail["active_consultation"])
        self.assertEqual(report["status"], "ready")
        self.assertIsNotNone(report["final_selected_style"])
        self.assertTrue(report["latest_generated_batch"]["items"])
        self.assertIn("selection_id", detail["style_selection_history"][0])
        self.assertIn("recommendation_id", detail["chosen_recommendation_history"][0])
        self.assertIn("recommendation_id", report["final_selected_style"])

    def test_client_detail_prefers_legacy_histories_even_when_canonical_rows_exist(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        legacy_client_id = get_legacy_client_id(client=client)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE client_analysis
                SET face_type = %s,
                    processed_path = %s
                WHERE client_id = %s
                """,
                ["legacy-history-face", "https://example.com/legacy-history-capture.jpg", legacy_client_id],
            )

        detail = get_client_detail(client=client, admin=shop)

        self.assertEqual(detail["capture_history"][0]["processed_image_url"], "https://example.com/legacy-history-capture.jpg")
        self.assertEqual(detail["analysis_history"][0]["face_shape"], "legacy-history-face")

    def test_recommendation_report_prefers_legacy_rows_even_when_canonical_exists(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        legacy_client_id = get_legacy_client_id(client=client)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE client_result_detail
                SET style_name_snapshot = %s
                WHERE result_id IN (
                    SELECT result_id
                    FROM client_result
                    WHERE client_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                )
                AND rank = 1
                """,
                ["Legacy Preferred Style", legacy_client_id],
            )

        report = get_client_recommendation_report(client=client, admin=shop)

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["latest_generated_batch"]["items"][0]["style_name"], "Legacy Preferred Style")

    def test_recommendation_report_does_not_fallback_to_canonical_when_legacy_rows_are_empty(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        legacy_client_id = get_legacy_client_id(client=client)
        style = Style.objects.order_by("id").first()
        if style is None:
            style = Style.objects.create(
                name="Canonical Test Style",
                vibe="natural",
                description="canonical fallback style",
                image_url="https://example.com/styles/canonical-test.jpg",
            )
        self.assertIsNotNone(style)

        FormerRecommendation.objects.create(
            client_id=client.id,
            style=style,
            style_id_snapshot=style.id,
            style_name_snapshot=style.name,
            style_description_snapshot=style.description,
            source="generated",
            batch_id="11111111-1111-1111-1111-111111111111",
            rank=1,
            match_score=0.91,
            reasoning_snapshot={"summary": "canonical only"},
            is_chosen=True,
            chosen_at=timezone.now(),
        )

        self.assertTrue(FormerRecommendation.objects.filter(client_id=client.id).exists())
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM client_result_detail WHERE result_id IN (SELECT result_id FROM client_result WHERE client_id = %s)",
                [legacy_client_id],
            )
            cursor.execute("DELETE FROM client_result WHERE client_id = %s", [legacy_client_id])

        report = get_client_recommendation_report(client=client, admin=shop)

        self.assertEqual(report["status"], "ready")
        self.assertIsNone(report["final_selected_style"])
        self.assertEqual(report["latest_generated_batch"]["items"], [])

    def test_former_recommendations_prefers_legacy_rows_even_when_canonical_exists(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001001")
        legacy_client_id = get_legacy_client_id(client=client)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE client_result_detail
                SET style_name_snapshot = %s
                WHERE result_id IN (
                    SELECT result_id
                    FROM client_result
                    WHERE client_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                )
                AND rank = 1
                """,
                ["Legacy Former Preferred", legacy_client_id],
            )

        payload = get_former_recommendations(client)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["items"][0]["style_name"], "Legacy Former Preferred")

    def test_former_recommendations_do_not_fallback_to_canonical_when_legacy_rows_are_empty(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001001")
        legacy_client_id = get_legacy_client_id(client=client)
        style = Style.objects.order_by("id").first()
        if style is None:
            style = Style.objects.create(
                name="Canonical Test Style",
                vibe="natural",
                description="canonical fallback style",
                image_url="https://example.com/styles/canonical-test.jpg",
            )
        self.assertIsNotNone(style)

        FormerRecommendation.objects.create(
            client_id=client.id,
            style=style,
            style_id_snapshot=style.id,
            style_name_snapshot=style.name,
            style_description_snapshot=style.description,
            source="generated",
            batch_id="22222222-2222-2222-2222-222222222222",
            rank=1,
            match_score=0.82,
            reasoning_snapshot={"summary": "canonical only"},
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM client_result_detail WHERE result_id IN (SELECT result_id FROM client_result WHERE client_id = %s)",
                [legacy_client_id],
            )
            cursor.execute("DELETE FROM client_result WHERE client_id = %s", [legacy_client_id])

        payload = get_former_recommendations(client)

        self.assertEqual(payload["status"], "empty")
        self.assertEqual(payload["items"], [])

    def test_current_recommendations_prefers_legacy_rows_even_when_canonical_exists(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        legacy_client_id = get_legacy_client_id(client=client)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE client_result_detail
                SET style_name_snapshot = %s
                WHERE result_id IN (
                    SELECT result_id
                    FROM client_result
                    WHERE client_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                )
                AND rank = 1
                """,
                ["Legacy Current Preferred", legacy_client_id],
            )

        payload = get_current_recommendations(client)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["items"][0]["style_name"], "Legacy Current Preferred")

    def test_current_recommendations_do_not_regenerate_from_canonical_when_legacy_rows_are_empty(self):
        call_command("seed_test_accounts")

        client = self._seeded_client("01090001003")
        legacy_client_id = get_legacy_client_id(client=client)

        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM client_result_detail WHERE result_id IN (SELECT result_id FROM client_result WHERE client_id = %s)",
                [legacy_client_id],
            )
            cursor.execute("DELETE FROM client_result WHERE client_id = %s", [legacy_client_id])

        payload = get_current_recommendations(client)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["legacy_client_id"], legacy_client_id)
        self.assertTrue(payload["items"])
        self.assertEqual(FormerRecommendation.objects.filter(client_id=client.id).count(), 0)

    def test_all_clients_can_mark_active_consultation_from_legacy_rows(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        ConsultationRequest.objects.filter(client_id=client.id).delete()

        payload = get_all_clients(admin=shop)
        target = next(item for item in payload["items"] if item["client_id"] == client.id)

        self.assertTrue(target["has_active_consultation"])
        self.assertIsNotNone(target["last_consulted_at"])

    def test_active_client_sessions_and_detail_include_normalized_legacy_consultation_fields(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        ConsultationRequest.objects.filter(client_id=client.id).delete()

        sessions = get_active_client_sessions(admin=shop)
        target = next(item for item in sessions["items"] if item["legacy_client_id"] == get_legacy_client_id(client=client))
        detail = get_client_detail(client=client, admin=shop)

        self.assertIn("created_at", target)
        self.assertIn("last_activity_at", target)
        self.assertEqual(detail["active_consultation"]["legacy_client_id"], get_legacy_client_id(client=client))

    def test_client_list_and_detail_do_not_fallback_to_canonical_consultation_when_legacy_tables_exist(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        legacy_client_id = get_legacy_client_id(client=client)

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE client_result
                SET is_active = %s,
                    is_confirmed = %s,
                    status = %s,
                    closed_at = CURRENT_TIMESTAMP
                WHERE client_id = %s
                """,
                [False, False, "CLOSED", legacy_client_id],
            )

        ConsultationRequest.objects.update_or_create(
            client_id=client.id,
            defaults={
                "admin_id": shop.id,
                "designer_id": client.designer_id,
                "status": "PENDING",
                "is_active": True,
                "is_read": False,
                "source": "canonical_only_test",
            },
        )

        clients_payload = get_all_clients(admin=shop)
        target = next(item for item in clients_payload["items"] if item["client_id"] == client.id)
        detail = get_client_detail(client=client, admin=shop)

        self.assertFalse(target["has_active_consultation"])
        self.assertIsNone(target["last_consulted_at"])
        self.assertIsNone(detail["active_consultation"])

    def test_consultation_note_and_close_can_bridge_from_legacy_active_rows(self):
        call_command("seed_test_accounts")

        shop = self._seeded_admin()
        client = self._seeded_client("01090001001")
        ConsultationRequest.objects.filter(client_id=client.id).delete()

        legacy_items = get_active_client_sessions(admin=shop)["items"]
        target = next(item for item in legacy_items if item["legacy_client_id"] == get_legacy_client_id(client=client))
        consultation_id = target["consultation_id"]

        note_payload = create_client_note(
            client=client,
            consultation_id=consultation_id,
            content="Legacy consultation bridge note",
            admin=shop,
        )
        close_payload = close_consultation_session(
            consultation_id=consultation_id,
            admin=shop,
        )

        self.assertEqual(note_payload["status"], "success")
        self.assertEqual(close_payload["status"], "success")
        self.assertTrue(
            ClientSessionNote.objects.filter(
                client_ref_id=client.id,
                consultation_ref_id=consultation_id,
                content="Legacy consultation bridge note",
            ).exists()
        )
        if self._table_has_column("client_session_notes", "legacy_client_ref_id"):
            self.assertTrue(
                ClientSessionNote.objects.filter(
                    client_ref_id=client.id,
                    consultation_ref_id=consultation_id,
                    legacy_client_ref_id=get_legacy_client_id(client=client),
                    content="Legacy consultation bridge note",
                ).exists()
            )
        consultation = ConsultationRequest.objects.get(id=consultation_id)
        self.assertFalse(consultation.is_active)
        self.assertEqual(consultation.status, "CLOSED")
        self.assertTrue(consultation.is_read)
        if self._table_has_column("client_result", "status"):
            result_row = self._fetch_one(
                """
                SELECT status, is_active, is_read
                FROM client_result
                WHERE client_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                [get_legacy_client_id(client=client)],
            )
            self.assertEqual(str(result_row[0]), "CLOSED")
            self.assertFalse(bool(result_row[1]))
            self.assertTrue(bool(result_row[2]))

    def test_admin_consultation_serializers_accept_string_identifiers(self):
        note_serializer = ConsultationNoteCreateSerializer(
            data={
                "client_id": "01090001001",
                "consultation_id": "12345",
                "content": "legacy consultation note",
                "admin_id": "shop-1",
            }
        )
        close_serializer = ConsultationCloseSerializer(data={"consultation_id": "12345"})

        self.assertTrue(note_serializer.is_valid(), note_serializer.errors)
        self.assertEqual(note_serializer.validated_data["consultation_id"], 12345)
        self.assertEqual(note_serializer.validated_data["admin_id"], "shop-1")
        self.assertTrue(close_serializer.is_valid(), close_serializer.errors)
        self.assertEqual(close_serializer.validated_data["consultation_id"], 12345)

    def test_import_command_reports_legacy_summary_from_model_team_tables(self):
        self._insert_legacy_source_rows()

        stdout = io.StringIO()
        call_command("import_model_team_tables", strict=True, stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("Model-team tables have been imported into canonical tables.", output)
        self.assertIn("shop=1", output)
        self.assertIn("designer=1", output)
        self.assertIn("client=1", output)
        self.assertIn("survey=1", output)
        self.assertIn("analysis=1", output)
        self.assertIn("result=2", output)
        self.assertIn("consultation=0", output)
        self.assertIn("hairstyle=2", output)

    def test_import_command_is_idempotent_for_same_model_team_rows(self):
        self._insert_legacy_source_rows()

        stdout_first = io.StringIO()
        stdout_second = io.StringIO()
        call_command("import_model_team_tables", strict=True, stdout=stdout_first)
        call_command("import_model_team_tables", strict=True, stdout=stdout_second)

        self.assertIn("shop=1", stdout_first.getvalue())
        self.assertIn("designer=1", stdout_first.getvalue())
        self.assertIn("client=1", stdout_first.getvalue())
        self.assertIn("result=2", stdout_first.getvalue())
        self.assertEqual(stdout_first.getvalue().splitlines()[-1], stdout_second.getvalue().splitlines()[-1])


