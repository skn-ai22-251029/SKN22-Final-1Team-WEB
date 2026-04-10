from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from app.api.v1.services_django import (
    get_current_recommendations,
    get_trend_recommendations,
)
from app.models_model_team import (
    LegacyClient,
    LegacyClientAnalysis,
    LegacyClientResult,
    LegacyClientResultDetail,
    LegacyClientSurvey,
    LegacyDesigner,
)
from app.services.model_team_bridge import get_admin_by_phone, get_client_by_phone


TEST_SHOP_PHONE = "01080001000"
EXPECTED_BUSINESS_NUMBER = "1012345672"
EXPECTED_CLIENT_PHONES = ("01090001001", "01090001002", "01090001003", "01090001004")
EXPECTED_COUNTS = {
    "designers": 2,
    "clients": 4,
    "surveys": 4,
    "captures": 3,
    "analyses": 3,
    "generated_recommendations": 15,
    "chosen_recommendations": 2,
    "style_selections": 2,
    "active_consultations": 2,
}


@dataclass(frozen=True)
class ClientExpectation:
    phone: str
    captures: int
    analyses: int
    generated_recommendations: int
    chosen_recommendations: int
    style_selections: int
    active_consultations: int
    has_current_recommendations: bool


EXPECTED_CLIENTS = (
    ClientExpectation("01090001001", 1, 1, 5, 1, 1, 1, True),
    ClientExpectation("01090001002", 1, 1, 5, 1, 1, 1, True),
    ClientExpectation("01090001003", 1, 1, 5, 0, 0, 0, True),
    ClientExpectation("01090001004", 0, 0, 0, 0, 0, 0, False),
)

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


def _legacy_tables_present() -> bool:
    existing = set(connection.introspection.table_names())
    return all(table in existing for table in LEGACY_TABLES)


class Command(BaseCommand):
    help = "Verify that the reusable seed data is present and internally consistent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Fail if any expected count or per-client expectation is missing.",
        )
        parser.add_argument(
            "--skip-recommendation-smoke",
            action="store_true",
            help="Skip recommendation payload smoke checks that can materialize missing recommendation rows.",
        )

    def handle(self, *args, **options):
        strict = bool(options["strict"])
        skip_recommendation_smoke = bool(options["skip_recommendation_smoke"])
        problems: list[str] = []
        if not _legacy_tables_present():
            message = "model-team legacy tables are missing"
            if strict:
                raise CommandError(message)
            self.stderr.write(message)
            return

        shop = get_admin_by_phone(phone=TEST_SHOP_PHONE)
        if not shop:
            problems.append("shop account is missing")
        else:
            if shop.business_number != EXPECTED_BUSINESS_NUMBER:
                problems.append(
                    f"shop business number mismatch: expected {EXPECTED_BUSINESS_NUMBER}, got {shop.business_number}"
                )
            if not shop.is_active:
                problems.append("shop account is inactive")

        if shop:
            scoped_client_ids = list(
                LegacyClient.objects.filter(backend_shop_ref_id=shop.id).values_list("backend_client_id", flat=True)
            )
            counts = {
                "designers": LegacyDesigner.objects.filter(backend_shop_ref_id=shop.id, is_active=True).count(),
                "clients": LegacyClient.objects.filter(backend_shop_ref_id=shop.id).count(),
                "surveys": LegacyClientSurvey.objects.filter(backend_client_ref_id__in=scoped_client_ids).count(),
                "captures": LegacyClientAnalysis.objects.filter(backend_client_ref_id__in=scoped_client_ids).count(),
                "analyses": LegacyClientAnalysis.objects.filter(backend_client_ref_id__in=scoped_client_ids).count(),
                "generated_recommendations": LegacyClientResultDetail.objects.filter(
                    backend_client_ref_id__in=scoped_client_ids
                ).count(),
                "chosen_recommendations": LegacyClientResultDetail.objects.filter(
                    backend_client_ref_id__in=scoped_client_ids,
                    is_chosen=True,
                ).count(),
                "style_selections": LegacyClientResult.objects.filter(
                    backend_client_ref_id__in=scoped_client_ids,
                    is_confirmed=True,
                ).count(),
                "active_consultations": LegacyClientResult.objects.filter(
                    backend_client_ref_id__in=scoped_client_ids,
                    is_active=True,
                ).count(),
            }
        else:
            counts = {key: 0 for key in EXPECTED_COUNTS}

        for label, expected in EXPECTED_COUNTS.items():
            actual = counts.get(label, 0)
            if actual != expected:
                problems.append(f"{label} mismatch: expected {expected}, got {actual}")

        if shop:
            for phone in EXPECTED_CLIENT_PHONES:
                if not get_client_by_phone(phone=phone):
                    problems.append(f"client is missing: {phone}")

        for expectation in EXPECTED_CLIENTS:
            client = get_client_by_phone(phone=expectation.phone)
            if not client:
                continue

            actual_counts = {
                "captures": LegacyClientAnalysis.objects.filter(backend_client_ref_id=client.id).count(),
                "analyses": LegacyClientAnalysis.objects.filter(backend_client_ref_id=client.id).count(),
                "generated_recommendations": LegacyClientResultDetail.objects.filter(
                    backend_client_ref_id=client.id,
                ).count(),
                "chosen_recommendations": LegacyClientResultDetail.objects.filter(
                    backend_client_ref_id=client.id,
                    is_chosen=True,
                ).count(),
                "style_selections": LegacyClientResult.objects.filter(
                    backend_client_ref_id=client.id,
                    is_confirmed=True,
                ).count(),
                "active_consultations": LegacyClientResult.objects.filter(
                    backend_client_ref_id=client.id,
                    is_active=True,
                ).count(),
            }

            if actual_counts["captures"] != expectation.captures:
                problems.append(
                    f"{client.phone} capture mismatch: expected {expectation.captures}, got {actual_counts['captures']}"
                )
            if actual_counts["analyses"] != expectation.analyses:
                problems.append(
                    f"{client.phone} analysis mismatch: expected {expectation.analyses}, got {actual_counts['analyses']}"
                )
            if actual_counts["generated_recommendations"] != expectation.generated_recommendations:
                problems.append(
                    f"{client.phone} generated recommendation mismatch: expected {expectation.generated_recommendations}, got {actual_counts['generated_recommendations']}"
                )
            if actual_counts["chosen_recommendations"] != expectation.chosen_recommendations:
                problems.append(
                    f"{client.phone} chosen recommendation mismatch: expected {expectation.chosen_recommendations}, got {actual_counts['chosen_recommendations']}"
                )
            if actual_counts["style_selections"] != expectation.style_selections:
                problems.append(
                    f"{client.phone} style selection mismatch: expected {expectation.style_selections}, got {actual_counts['style_selections']}"
                )
            if actual_counts["active_consultations"] != expectation.active_consultations:
                problems.append(
                    f"{client.phone} active consultation mismatch: expected {expectation.active_consultations}, got {actual_counts['active_consultations']}"
                )

            if not skip_recommendation_smoke:
                if expectation.has_current_recommendations:
                    payload = get_current_recommendations(client)
                    if not payload.get("items"):
                        problems.append(f"{client.phone} current recommendations are empty")
                else:
                    payload = get_current_recommendations(client)
                    if payload.get("items"):
                        problems.append(f"{client.phone} should not have current recommendations yet")

        self.stdout.write(f"seed integrity summary: source=legacy counts={counts}")
        if skip_recommendation_smoke:
            self.stdout.write("seed integrity recommendation smoke: skipped")
        else:
            trend_payload = get_trend_recommendations(days=30, client=get_client_by_phone(phone=EXPECTED_CLIENT_PHONES[0]))
            if not trend_payload.get("items"):
                problems.append("trend recommendations are empty")
            self.stdout.write(
                "seed integrity trend items: "
                f"{len(trend_payload.get('items', []))} / scope={trend_payload.get('trend_scope')}"
            )

        if problems:
            for problem in problems:
                self.stderr.write(f"seed integrity issue: {problem}")
            if strict:
                raise CommandError("Seed integrity check failed.")
            self.stdout.write(self.style.WARNING("Seed integrity check completed with warnings."))
            return

        self.stdout.write(self.style.SUCCESS("Seed integrity check passed."))
