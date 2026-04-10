from __future__ import annotations

import ast
import re
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


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

CANONICAL_DROP_CANDIDATES = (
    "admin_accounts",
    "designers",
    "surveys",
    "capture_records",
    "face_analyses",
    "former_recommendations",
    "style_selections",
    "consultation_requests",
    "styles",
)

CANONICAL_TABLE_NAMES = tuple(CANONICAL_DROP_CANDIDATES)
CANONICAL_MODEL_IMPORT_MODULE = "app.models_django"
BACKEND_ONLY_MODEL_IMPORT_EXCEPTIONS = {
    "ClientProfileNote",
    "DesignerDiagnosisCard",
}
SCAN_SKIP_FILES = {
    "models_django.py",
    "models_model_team.py",
    "audit_model_team_cutover.py",
}
SCAN_SKIP_PARTS = {"migrations", "tests", "__pycache__"}
SQL_TABLE_PATTERNS = tuple(
    re.compile(rf"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+{re.escape(table_name)}\b", re.IGNORECASE)
    for table_name in CANONICAL_TABLE_NAMES
)


class Command(BaseCommand):
    help = "Audit model-team cutover readiness without touching front/model team artifacts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Fail when required legacy tables are missing or the legacy integrity check fails.",
        )

    def handle(self, *args, **options):
        strict = bool(options["strict"])
        existing_tables = set(connection.introspection.table_names())
        missing_legacy = [table for table in LEGACY_TABLES if table not in existing_tables]
        code_blockers = self._scan_code_blockers()

        if missing_legacy and strict:
            raise CommandError(f"Missing legacy tables: {', '.join(missing_legacy)}")

        self.stdout.write("model-team cutover audit")
        self.stdout.write(f"legacy tables present: {len(LEGACY_TABLES) - len(missing_legacy)}/{len(LEGACY_TABLES)}")
        if missing_legacy:
            self.stdout.write(self.style.WARNING(f"missing legacy tables: {', '.join(missing_legacy)}"))

        integrity_stdout = StringIO()
        integrity_stderr = StringIO()
        try:
            call_command(
                "verify_seed_integrity",
                strict=True,
                skip_recommendation_smoke=True,
                stdout=integrity_stdout,
                stderr=integrity_stderr,
            )
        except CommandError as exc:
            self.stdout.write(self.style.ERROR("runtime bridge integrity: failed"))
            if integrity_stdout.getvalue().strip():
                self.stdout.write(integrity_stdout.getvalue().strip())
            if integrity_stderr.getvalue().strip():
                self.stdout.write(integrity_stderr.getvalue().strip())
            if strict:
                raise CommandError("Cutover audit failed during runtime bridge integrity check.") from exc
        else:
            self.stdout.write(self.style.SUCCESS("runtime bridge integrity: passed"))
            if integrity_stdout.getvalue().strip():
                self.stdout.write(integrity_stdout.getvalue().strip())

        self.stdout.write("")
        self.stdout.write("canonical drop candidates:")
        for table_name in CANONICAL_DROP_CANDIDATES:
            exists = table_name in existing_tables
            row_count = self._table_row_count(table_name) if exists else 0
            self.stdout.write(f"- {table_name}: exists={exists} rows={row_count}")

        self.stdout.write("")
        self.stdout.write("backend-only exceptions:")
        self.stdout.write(f"- client_session_notes: rows={self._table_row_count('client_session_notes')} (preserve)")

        self.stdout.write("")
        self.stdout.write("code blockers:")
        if not code_blockers:
            self.stdout.write(self.style.SUCCESS("- none"))
        else:
            for blocker in code_blockers:
                self.stdout.write(self.style.WARNING(f"- {blocker}"))

        self.stdout.write("")
        if code_blockers and strict:
            raise CommandError("Cutover audit found remaining canonical code blockers.")

        completion_style = self.style.SUCCESS if not code_blockers else self.style.WARNING
        self.stdout.write(
            completion_style(
                "cutover audit completed. front/model team artifacts were not touched by this command."
            )
        )

    def _scan_code_blockers(self) -> list[str]:
        app_root = Path(__file__).resolve().parents[2]
        blockers: list[str] = []

        for path in sorted(app_root.rglob("*.py")):
            relative = path.relative_to(app_root)
            if path.name in SCAN_SKIP_FILES:
                continue
            if any(part in SCAN_SKIP_PARTS for part in relative.parts):
                continue

            text = path.read_text(encoding="utf-8-sig")

            import_lineno = self._runtime_canonical_import_lineno(text=text)
            if import_lineno is not None:
                blockers.append(f"{relative}:{import_lineno} imports canonical models at runtime")

            sql_blocker = self._canonical_sql_reference(text=text)
            if sql_blocker is not None:
                blockers.append(f"{relative}:{sql_blocker[0]} references {sql_blocker[1]}")

        return blockers

    def _table_row_count(self, table_name: str) -> int:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            row = cursor.fetchone()
        return int(row[0] if row else 0)

    def _runtime_canonical_import_lineno(self, *, text: str) -> int | None:
        tree = ast.parse(text)

        def visit(node: ast.AST, *, under_type_checking: bool = False) -> int | None:
            is_type_checking_guard = (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"
            )

            if isinstance(node, ast.ImportFrom) and node.module == CANONICAL_MODEL_IMPORT_MODULE and not under_type_checking:
                imported_names = {alias.name for alias in node.names}
                if imported_names - BACKEND_ONLY_MODEL_IMPORT_EXCEPTIONS:
                    return node.lineno

            for child in ast.iter_child_nodes(node):
                child_lineno = visit(
                    child,
                    under_type_checking=under_type_checking or is_type_checking_guard,
                )
                if child_lineno is not None:
                    return child_lineno
            return None

        return visit(tree)

    def _canonical_sql_reference(self, *, text: str) -> tuple[int, str] | None:
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in SQL_TABLE_PATTERNS:
                match = pattern.search(line)
                if match:
                    return lineno, match.group(0).strip()
        return None
