from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


LEGACY_TABLES = (
    "shop",
    "designer",
    "client",
    "client_survey",
    "client_analysis",
    "client_result",
    "client_result_detail",
    "hairstyle",
)

CANONICAL_TABLES = {
    "consultations": "consultation_requests",
    "selections": "style_selections",
    "recommendations": "former_recommendations",
    "analyses": "face_analyses",
    "captures": "capture_records",
    "surveys": "surveys",
    "designers": "designers",
    "admins": "admin_accounts",
    "styles": "styles",
}


@dataclass
class CleanupSummary:
    preserved_admins: int
    preserved_designers: int
    preserved_client_records: int
    preserved_surveys: int
    preserved_captures: int
    preserved_analyses: int
    preserved_recommendations: int
    preserved_selections: int
    preserved_consultations: int
    preserved_styles: int
    deleted_notes: int
    deleted_consultations: int
    deleted_selections: int
    deleted_recommendations: int
    deleted_analyses: int
    deleted_captures: int
    deleted_surveys: int
    deleted_client_records: int
    deleted_designers: int
    deleted_admins: int
    deleted_styles: int


class Command(BaseCommand):
    help = "Remove canonical backend data after cutover. Preserve only client_session_notes by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually delete backend-only canonical rows.")
        parser.add_argument("--strict", action="store_true", help="Require every legacy table to exist.")
        parser.add_argument(
            "--preserve-canonical-refs",
            action="store_true",
            help="Preserve canonical rows that are still referenced by legacy backend_* columns.",
        )

    def handle(self, *args, **options):
        legacy_tables = self._existing_legacy_tables()
        if options["strict"]:
            missing = sorted(set(LEGACY_TABLES) - set(legacy_tables))
            if missing:
                raise CommandError(f"Missing legacy tables: {', '.join(missing)}")
        if not legacy_tables:
            raise CommandError("No legacy tables were found. cleanup_backend_only_data requires model-team tables.")

        preserve = self._build_preserve_sets() if options["preserve_canonical_refs"] else self._empty_preserve_sets()
        summary = self._cleanup_backend_only_data(preserve=preserve, apply=options["apply"])

        mode = "applied" if options["apply"] else "dry-run"
        self.stdout.write(self.style.SUCCESS(f"backend-only cleanup {mode} completed."))
        self.stdout.write(
            "preserve strategy: "
            + (
                "legacy backend_* references"
                if options["preserve_canonical_refs"]
                else "none (client_session_notes only)"
            )
        )
        self.stdout.write(
            "preserved: "
            f"admins={summary.preserved_admins}, designers={summary.preserved_designers}, client_records={summary.preserved_client_records}, "
            f"surveys={summary.preserved_surveys}, captures={summary.preserved_captures}, analyses={summary.preserved_analyses}, "
            f"recommendations={summary.preserved_recommendations}, selections={summary.preserved_selections}, "
            f"consultations={summary.preserved_consultations}, styles={summary.preserved_styles}"
        )
        self.stdout.write(
            "deleted: "
            f"notes={summary.deleted_notes}, consultations={summary.deleted_consultations}, selections={summary.deleted_selections}, "
            f"recommendations={summary.deleted_recommendations}, analyses={summary.deleted_analyses}, captures={summary.deleted_captures}, "
            f"surveys={summary.deleted_surveys}, client_records={summary.deleted_client_records}, designers={summary.deleted_designers}, "
            f"admins={summary.deleted_admins}, styles={summary.deleted_styles}"
        )

    def _existing_legacy_tables(self) -> set[str]:
        with connection.cursor() as cursor:
            return set(connection.introspection.table_names(cursor))

    def _table_columns(self, table_name: str) -> set[str]:
        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(cursor, table_name)
        return {column.name for column in description}

    def _fetch_ids(self, *, table: str, column: str) -> set[int]:
        if table not in self._existing_legacy_tables():
            return set()
        if column not in self._table_columns(table):
            return set()
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
            return {int(row[0]) for row in cursor.fetchall() if row and row[0] is not None}

    def _build_preserve_sets(self) -> dict[str, set[int]]:
        preserve_admins = self._fetch_ids(table="shop", column="backend_admin_id")
        preserve_designers = self._fetch_ids(table="designer", column="backend_designer_id")
        preserve_client_records = self._fetch_ids(table="client", column="backend_client_id")
        preserve_surveys = self._fetch_ids(table="client_survey", column="backend_survey_id")
        preserve_analyses = self._fetch_ids(table="client_analysis", column="backend_analysis_id")
        preserve_captures = self._fetch_ids(table="client_analysis", column="backend_capture_record_id")
        preserve_recommendations = self._fetch_ids(table="client_result_detail", column="backend_recommendation_id")
        preserve_selections = self._fetch_ids(table="client_result", column="backend_selection_id")
        preserve_consultations = self._fetch_ids(table="client_result", column="backend_consultation_id")
        preserve_styles = self._fetch_ids(table="hairstyle", column="backend_style_id")

        return {
            "admins": preserve_admins,
            "designers": preserve_designers,
            "client_records": preserve_client_records,
            "surveys": preserve_surveys,
            "captures": preserve_captures,
            "analyses": preserve_analyses,
            "recommendations": preserve_recommendations,
            "selections": preserve_selections,
            "consultations": preserve_consultations,
            "styles": preserve_styles,
        }

    def _empty_preserve_sets(self) -> dict[str, set[int]]:
        return {
            "admins": set(),
            "designers": set(),
            "client_records": set(),
            "surveys": set(),
            "captures": set(),
            "analyses": set(),
            "recommendations": set(),
            "selections": set(),
            "consultations": set(),
            "styles": set(),
        }

    def _cleanup_backend_only_data(self, *, preserve: dict[str, set[int]], apply: bool) -> CleanupSummary:
        deleted_notes = 0
        deleted_consultations = self._count_table_rows(CANONICAL_TABLES["consultations"], preserve["consultations"])
        deleted_selections = self._count_table_rows(CANONICAL_TABLES["selections"], preserve["selections"])
        deleted_recommendations = self._count_table_rows(CANONICAL_TABLES["recommendations"], preserve["recommendations"])
        deleted_analyses = self._count_table_rows(CANONICAL_TABLES["analyses"], preserve["analyses"])
        deleted_captures = self._count_table_rows(CANONICAL_TABLES["captures"], preserve["captures"])
        deleted_surveys = self._count_table_rows(CANONICAL_TABLES["surveys"], preserve["surveys"])
        deleted_client_records = 0
        deleted_designers = self._count_table_rows(CANONICAL_TABLES["designers"], preserve["designers"])
        deleted_admins = self._count_table_rows(CANONICAL_TABLES["admins"], preserve["admins"])
        deleted_styles = self._count_table_rows(CANONICAL_TABLES["styles"], preserve["styles"])

        if apply:
            with transaction.atomic():
                self._delete_table_rows(CANONICAL_TABLES["consultations"], preserve["consultations"])
                self._delete_table_rows(CANONICAL_TABLES["selections"], preserve["selections"])
                self._delete_table_rows(CANONICAL_TABLES["recommendations"], preserve["recommendations"])
                self._delete_table_rows(CANONICAL_TABLES["analyses"], preserve["analyses"])
                self._delete_table_rows(CANONICAL_TABLES["captures"], preserve["captures"])
                self._delete_table_rows(CANONICAL_TABLES["surveys"], preserve["surveys"])
                self._delete_table_rows(CANONICAL_TABLES["designers"], preserve["designers"])
                self._delete_table_rows(CANONICAL_TABLES["admins"], preserve["admins"])
                self._delete_table_rows(CANONICAL_TABLES["styles"], preserve["styles"])

        return CleanupSummary(
            preserved_admins=len(preserve["admins"]),
            preserved_designers=len(preserve["designers"]),
            preserved_client_records=len(preserve["client_records"]),
            preserved_surveys=len(preserve["surveys"]),
            preserved_captures=len(preserve["captures"]),
            preserved_analyses=len(preserve["analyses"]),
            preserved_recommendations=len(preserve["recommendations"]),
            preserved_selections=len(preserve["selections"]),
            preserved_consultations=len(preserve["consultations"]),
            preserved_styles=len(preserve["styles"]),
            deleted_notes=deleted_notes,
            deleted_consultations=deleted_consultations,
            deleted_selections=deleted_selections,
            deleted_recommendations=deleted_recommendations,
            deleted_analyses=deleted_analyses,
            deleted_captures=deleted_captures,
            deleted_surveys=deleted_surveys,
            deleted_client_records=deleted_client_records,
            deleted_designers=deleted_designers,
            deleted_admins=deleted_admins,
            deleted_styles=deleted_styles,
        )

    def _count_table_rows(self, table_name: str, preserve_ids: set[int]) -> int:
        if table_name not in self._existing_legacy_tables():
            return 0
        sql = f"SELECT COUNT(*) FROM {table_name}"
        params: list[int] = []
        if preserve_ids:
            placeholders = ", ".join(["%s"] * len(preserve_ids))
            sql += f" WHERE id NOT IN ({placeholders})"
            params.extend(sorted(preserve_ids))
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
        return int(row[0] if row else 0)

    def _delete_table_rows(self, table_name: str, preserve_ids: set[int]) -> int:
        if table_name not in self._existing_legacy_tables():
            return 0
        sql = f"DELETE FROM {table_name}"
        params: list[int] = []
        if preserve_ids:
            placeholders = ", ".join(["%s"] * len(preserve_ids))
            sql += f" WHERE id NOT IN ({placeholders})"
            params.extend(sorted(preserve_ids))
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            deleted = cursor.rowcount
        return int(deleted)
