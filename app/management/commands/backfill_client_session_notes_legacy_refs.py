from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from app.models_model_team import LegacyClient


class Command(BaseCommand):
    help = "Backfill client_session_notes.legacy_client_ref_id from legacy client rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the backfilled legacy_client_ref_id values.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Optional limit for the number of note rows to inspect.",
        )

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        limit = options["limit"]

        self._ensure_required_schema()

        pending_rows = self._pending_rows(limit=limit)
        backend_client_ids = sorted({int(client_ref_id) for _, client_ref_id in pending_rows if client_ref_id is not None})

        legacy_map: dict[int, str] = {}
        duplicate_backend_ids: set[int] = set()
        for backend_client_id, legacy_client_id in LegacyClient.objects.filter(
            backend_client_id__in=backend_client_ids
        ).order_by("backend_client_id", "client_id").values_list("backend_client_id", "client_id"):
            if backend_client_id is None or not legacy_client_id:
                continue
            normalized_backend_id = int(backend_client_id)
            if normalized_backend_id in legacy_map and legacy_map[normalized_backend_id] != legacy_client_id:
                duplicate_backend_ids.add(normalized_backend_id)
                continue
            legacy_map.setdefault(normalized_backend_id, str(legacy_client_id))

        matched_rows: list[tuple[int, str]] = []
        missing_mapping: list[tuple[int, int | None]] = []
        for note_id, client_ref_id in pending_rows:
            normalized_client_ref_id = int(client_ref_id) if client_ref_id is not None else None
            legacy_client_ref_id = legacy_map.get(normalized_client_ref_id) if normalized_client_ref_id is not None else None
            if legacy_client_ref_id:
                matched_rows.append((int(note_id), legacy_client_ref_id))
            else:
                missing_mapping.append((int(note_id), normalized_client_ref_id))

        if apply and matched_rows:
            with transaction.atomic():
                for note_id, legacy_client_ref_id in matched_rows:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE client_session_notes
                            SET legacy_client_ref_id = %s
                            WHERE id = %s
                            """,
                            [legacy_client_ref_id, note_id],
                        )

        self.stdout.write("client_session_notes legacy_client_ref_id backfill")
        self.stdout.write(f"- mode: {'apply' if apply else 'dry-run'}")
        self.stdout.write(f"- scanned_rows: {len(pending_rows)}")
        self.stdout.write(f"- matched_rows: {len(matched_rows)}")
        self.stdout.write(f"- missing_mapping_rows: {len(missing_mapping)}")
        self.stdout.write(f"- duplicate_backend_client_ids: {len(duplicate_backend_ids)}")
        self.stdout.write(f"- updated_rows: {len(matched_rows) if apply else 0}")

        if duplicate_backend_ids:
            sample = ", ".join(str(value) for value in sorted(duplicate_backend_ids)[:10])
            self.stdout.write(self.style.WARNING(f"- duplicate backend_client_id values detected: {sample}"))

        if missing_mapping:
            sample = ", ".join(
                f"note_id={note_id}/client_ref_id={client_ref_id}"
                for note_id, client_ref_id in missing_mapping[:10]
            )
            self.stdout.write(self.style.WARNING(f"- missing mappings sample: {sample}"))

        if not apply:
            self.stdout.write(self.style.WARNING("- dry-run only; rerun with --apply to persist changes"))

    def _ensure_required_schema(self) -> None:
        table_names = set(connection.introspection.table_names())
        if "client_session_notes" not in table_names:
            raise CommandError("client_session_notes table does not exist.")
        if "client" not in table_names:
            raise CommandError("legacy client table does not exist.")

        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(cursor, "client_session_notes")
            }

        if "legacy_client_ref_id" not in columns:
            raise CommandError(
                "legacy_client_ref_id column is missing. Apply the stage-1 client_session_notes migration first."
            )

    def _pending_rows(self, *, limit: int | None) -> list[tuple[int, int | None]]:
        sql = """
            SELECT id, client_id
            FROM client_session_notes
            WHERE legacy_client_ref_id IS NULL OR TRIM(COALESCE(legacy_client_ref_id, '')) = ''
            ORDER BY id
        """
        params: list[object] = []
        if limit:
            sql += " LIMIT %s"
            params.append(int(limit))
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return [(int(row[0]), int(row[1]) if row[1] is not None else None) for row in cursor.fetchall()]
