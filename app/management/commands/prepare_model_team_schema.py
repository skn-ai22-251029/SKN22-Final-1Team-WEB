from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


POSTGRES_CREATE_TABLE_STATEMENTS = (
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
        backend_admin_id BIGINT,
        name TEXT,
        store_name TEXT,
        role TEXT,
        phone TEXT,
        business_number TEXT,
        password_hash TEXT,
        is_active BOOLEAN,
        consent_snapshot JSONB,
        consented_at TIMESTAMPTZ
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
        backend_designer_id BIGINT,
        backend_shop_ref_id BIGINT,
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
        backend_client_id BIGINT,
        backend_shop_ref_id BIGINT,
        backend_designer_ref_id BIGINT,
        name TEXT,
        assigned_at TIMESTAMPTZ,
        assignment_source TEXT,
        age_input SMALLINT,
        birth_year_estimate SMALLINT
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
        backend_survey_id BIGINT,
        backend_client_ref_id BIGINT,
        target_length TEXT,
        target_vibe TEXT,
        scalp_type TEXT,
        hair_colour TEXT,
        budget_range TEXT,
        preference_vector_json JSONB,
        created_at_ts TIMESTAMPTZ
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
        backend_analysis_id BIGINT,
        backend_client_ref_id BIGINT,
        backend_designer_ref_id BIGINT,
        backend_capture_record_id BIGINT,
        processed_path TEXT,
        filename TEXT,
        status TEXT,
        face_count INTEGER,
        error_note TEXT,
        updated_at_ts TIMESTAMPTZ,
        deidentified_path TEXT,
        capture_landmark_snapshot JSONB,
        privacy_snapshot JSONB,
        analysis_image_url TEXT,
        analysis_landmark_snapshot JSONB
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
        backend_selection_id BIGINT,
        backend_consultation_id BIGINT,
        backend_client_ref_id BIGINT,
        backend_admin_ref_id BIGINT,
        backend_designer_ref_id BIGINT,
        source TEXT,
        survey_snapshot JSONB,
        analysis_data_snapshot JSONB,
        status TEXT,
        is_active BOOLEAN,
        is_read BOOLEAN,
        closed_at TIMESTAMPTZ,
        selected_recommendation_id BIGINT
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
        backend_recommendation_id BIGINT,
        backend_client_ref_id BIGINT,
        backend_capture_record_id BIGINT,
        batch_id UUID,
        source TEXT,
        style_name_snapshot TEXT,
        style_description_snapshot TEXT,
        keywords_json JSONB,
        sample_image_url TEXT,
        regeneration_snapshot JSONB,
        reasoning_snapshot JSONB,
        is_chosen BOOLEAN,
        chosen_at TIMESTAMPTZ,
        is_sent_to_admin BOOLEAN,
        sent_at TIMESTAMPTZ,
        created_at_ts TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hairstyle (
        hairstyle_id INTEGER PRIMARY KEY,
        chroma_id TEXT NOT NULL,
        style_name TEXT NOT NULL,
        image_url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        backend_style_id BIGINT,
        name TEXT,
        vibe TEXT,
        description TEXT
    )
    """,
)


POSTGRES_STATEMENTS = (
    # shop
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS backend_admin_id BIGINT",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS name VARCHAR(50)",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS store_name VARCHAR(100)",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS role VARCHAR(20)",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS phone VARCHAR(20)",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS business_number VARCHAR(30)",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS is_active BOOLEAN",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS consent_snapshot JSONB",
    "ALTER TABLE IF EXISTS shop ADD COLUMN IF NOT EXISTS consented_at TIMESTAMPTZ",
    # designer
    "ALTER TABLE IF EXISTS designer ADD COLUMN IF NOT EXISTS backend_designer_id BIGINT",
    "ALTER TABLE IF EXISTS designer ADD COLUMN IF NOT EXISTS backend_shop_ref_id BIGINT",
    "ALTER TABLE IF EXISTS designer ADD COLUMN IF NOT EXISTS name VARCHAR(50)",
    "ALTER TABLE IF EXISTS designer ADD COLUMN IF NOT EXISTS phone VARCHAR(20)",
    "ALTER TABLE IF EXISTS designer ADD COLUMN IF NOT EXISTS pin_hash VARCHAR(255)",
    # client
    "ALTER TABLE IF EXISTS client ADD COLUMN IF NOT EXISTS backend_client_id BIGINT",
    "ALTER TABLE IF EXISTS client ADD COLUMN IF NOT EXISTS backend_shop_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client ADD COLUMN IF NOT EXISTS backend_designer_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client ADD COLUMN IF NOT EXISTS name VARCHAR(50)",
    "ALTER TABLE IF EXISTS client ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS client ADD COLUMN IF NOT EXISTS assignment_source VARCHAR(30)",
    "ALTER TABLE IF EXISTS client ADD COLUMN IF NOT EXISTS age_input SMALLINT",
    "ALTER TABLE IF EXISTS client ADD COLUMN IF NOT EXISTS birth_year_estimate SMALLINT",
    # client_survey
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS backend_survey_id BIGINT",
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS backend_client_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS target_length VARCHAR(50)",
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS target_vibe VARCHAR(50)",
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS scalp_type VARCHAR(50)",
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS hair_colour VARCHAR(50)",
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS budget_range VARCHAR(50)",
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS preference_vector_json JSONB",
    "ALTER TABLE IF EXISTS client_survey ADD COLUMN IF NOT EXISTS created_at_ts TIMESTAMPTZ",
    # client_analysis
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS backend_analysis_id BIGINT",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS backend_client_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS backend_designer_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS backend_capture_record_id BIGINT",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS processed_path VARCHAR(500)",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS filename VARCHAR(255)",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS status VARCHAR(50)",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS face_count INTEGER",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS error_note TEXT",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS updated_at_ts TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS deidentified_path VARCHAR(500)",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS capture_landmark_snapshot JSONB",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS privacy_snapshot JSONB",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS analysis_image_url VARCHAR(500)",
    "ALTER TABLE IF EXISTS client_analysis ADD COLUMN IF NOT EXISTS analysis_landmark_snapshot JSONB",
    # client_result
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS backend_selection_id BIGINT",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS backend_consultation_id BIGINT",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS backend_client_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS backend_admin_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS backend_designer_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS source VARCHAR(30)",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS survey_snapshot JSONB",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS analysis_data_snapshot JSONB",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS status VARCHAR(20)",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS is_active BOOLEAN",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS is_read BOOLEAN",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS client_result ADD COLUMN IF NOT EXISTS selected_recommendation_id BIGINT",
    # client_result_detail
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS backend_recommendation_id BIGINT",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS backend_client_ref_id BIGINT",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS backend_capture_record_id BIGINT",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS batch_id UUID",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS source VARCHAR(20)",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS style_name_snapshot VARCHAR(100)",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS style_description_snapshot TEXT",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS keywords_json JSONB",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS sample_image_url VARCHAR(500)",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS regeneration_snapshot JSONB",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS reasoning_snapshot JSONB",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS is_chosen BOOLEAN",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS chosen_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS is_sent_to_admin BOOLEAN",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS client_result_detail ADD COLUMN IF NOT EXISTS created_at_ts TIMESTAMPTZ",
    # hairstyle
    "ALTER TABLE IF EXISTS hairstyle ADD COLUMN IF NOT EXISTS backend_style_id BIGINT",
    "ALTER TABLE IF EXISTS hairstyle ADD COLUMN IF NOT EXISTS name VARCHAR(100)",
    "ALTER TABLE IF EXISTS hairstyle ADD COLUMN IF NOT EXISTS vibe VARCHAR(50)",
    "ALTER TABLE IF EXISTS hairstyle ADD COLUMN IF NOT EXISTS description TEXT",
    # client_session_notes
    "ALTER TABLE IF EXISTS client_session_notes ADD COLUMN IF NOT EXISTS legacy_client_ref_id VARCHAR(255)",
    """
    DO $$
    DECLARE r record;
    BEGIN
        FOR r IN
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'client_session_notes'::regclass
              AND contype = 'f'
        LOOP
            EXECUTE format('ALTER TABLE client_session_notes DROP CONSTRAINT IF EXISTS %I', r.conname);
        END LOOP;
    END
    $$;
    """,
)

SQLITE_TABLE_STATEMENTS = (
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


class Command(BaseCommand):
    help = "Extend model-team tables so they can hold backend metadata during schema unification."

    def handle(self, *args, **options):
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                for statement in POSTGRES_CREATE_TABLE_STATEMENTS:
                    cursor.execute(statement)
                for statement in POSTGRES_STATEMENTS:
                    cursor.execute(statement)

            self.stdout.write(
                self.style.SUCCESS(
                    "Model-team tables were extended with backend merge columns."
                )
            )
            return

        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                for statement in SQLITE_TABLE_STATEMENTS:
                    cursor.execute(statement)

            self.stdout.write(
                self.style.SUCCESS(
                    "Local sqlite model-team tables were created or verified."
                )
            )
            return

        raise CommandError("prepare_model_team_schema supports PostgreSQL and sqlite only.")
