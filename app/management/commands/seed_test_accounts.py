from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from app.api.v1.admin_services import create_client_note
from app.api.v1.services_django import confirm_style_selection, get_latest_survey, persist_generated_batch, upsert_survey
from app.models_model_team import (
    LegacyClientAnalysis,
    LegacyClientResult,
    LegacyClientResultDetail,
    LegacyDesigner,
    LegacyShop,
)
from app.services.model_team_bridge import (
    complete_legacy_capture_analysis,
    create_admin_record,
    create_designer_record,
    create_legacy_capture_upload_record,
    get_admin_by_phone,
    get_legacy_active_consultation_items,
    get_legacy_admin_id,
    get_legacy_designer_id,
    get_designers_for_admin,
    upsert_client_record,
)
from app.services.legacy_model_sync import _existing_legacy_tables, _table_names


def _build_valid_business_number(prefix: str) -> str:
    normalized = "".join(char for char in prefix if char.isdigit())[:9].ljust(9, "0")
    digits = [int(char) for char in normalized]
    weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    checksum = sum(digit * weight for digit, weight in zip(digits, weights))
    checksum += (digits[8] * 5) // 10
    check_digit = (10 - (checksum % 10)) % 10
    return f"{normalized}{check_digit}"


@dataclass(frozen=True)
class SeededClientSpec:
    phone: str
    name: str
    gender: str
    age_input: int
    designer_index: int | None
    assignment_source: str
    survey: dict


@dataclass(frozen=True)
class DownstreamSeedSpec:
    phone: str
    face_shape: str
    golden_ratio_score: float
    choose_rank: int | None


CLIENT_SPECS: tuple[SeededClientSpec, ...] = (
    SeededClientSpec(
        phone="01090001001",
        name="최하나",
        gender="female",
        age_input=28,
        designer_index=0,
        assignment_source="seeded_designer",
        survey={
            "target_length": "long",
            "target_vibe": "elegant",
            "scalp_type": "waved",
            "hair_colour": "brown",
            "budget_range": "10_20",
        },
    ),
    SeededClientSpec(
        phone="01090001002",
        name="이도훈",
        gender="male",
        age_input=34,
        designer_index=1,
        assignment_source="seeded_designer",
        survey={
            "target_length": "short",
            "target_vibe": "chic",
            "scalp_type": "straight",
            "hair_colour": "black",
            "budget_range": "5_10",
        },
    ),
    SeededClientSpec(
        phone="01090001003",
        name="윤아라",
        gender="female",
        age_input=24,
        designer_index=0,
        assignment_source="shop_manual_assignment",
        survey={
            "target_length": "medium",
            "target_vibe": "natural",
            "scalp_type": "unknown",
            "hair_colour": "ash",
            "budget_range": "5_10",
        },
    ),
    SeededClientSpec(
        phone="01090001004",
        name="한서",
        gender="female",
        age_input=31,
        designer_index=None,
        assignment_source="shop_manual_assignment_pending",
        survey={
            "target_length": "medium",
            "target_vibe": "clean",
            "scalp_type": "sensitive",
            "hair_colour": "dark_brown",
            "budget_range": "10_20",
        },
    ),
)

DOWNSTREAM_SPECS: tuple[DownstreamSeedSpec, ...] = (
    DownstreamSeedSpec(
        phone="01090001001",
        face_shape="oval",
        golden_ratio_score=0.92,
        choose_rank=1,
    ),
    DownstreamSeedSpec(
        phone="01090001002",
        face_shape="square",
        golden_ratio_score=0.88,
        choose_rank=2,
    ),
    DownstreamSeedSpec(
        phone="01090001003",
        face_shape="round",
        golden_ratio_score=0.9,
        choose_rank=None,
    ),
)


class Command(BaseCommand):
    help = "Seed reusable partner/customer verification accounts and downstream test data."

    def handle(self, *args, **options):
        self._ensure_legacy_schema()
        seeded_at = timezone.now()

        shop = self._upsert_shop()
        designers = self._upsert_designers(shop=shop)
        clients = self._upsert_clients(shop=shop, designers=designers, seeded_at=seeded_at)
        self._upsert_downstream_data(shop=shop, clients=clients)
        self._upsert_consultation_notes(shop=shop, designers=designers, clients=clients)

        self.stdout.write(self.style.SUCCESS("Reusable test accounts have been seeded."))
        self.stdout.write("")
        self.stdout.write("[Shop Admin]")
        self.stdout.write("  login page: /partner/login/")
        self.stdout.write(f"  business number: {shop.business_number}")
        self.stdout.write("  password: 1234")
        self.stdout.write("  phone: 010-8000-1000")
        self.stdout.write("  store: MirrAI Test Shop")
        self.stdout.write("")
        self.stdout.write("[Designers]")
        self.stdout.write("  Kim Mina / pin 2468")
        self.stdout.write("  Park Joon / pin 1357")
        self.stdout.write("")
        self.stdout.write("[Sample Customers]")
        self.stdout.write("  Choi Hana / 010-9000-1001 / Kim Mina assigned / seeded recommendations ready")
        self.stdout.write("  Lee Dohoon / 010-9000-1002 / Park Joon assigned / seeded recommendations ready")
        self.stdout.write("  Yoon Ara / 010-9000-1003 / Kim Mina assigned / current recommendations ready")
        self.stdout.write("  Han Seo / 010-9000-1004 / assignment pending")

    def _ensure_legacy_schema(self):
        _table_names.cache_clear()
        _existing_legacy_tables.cache_clear()
        existing_tables = set(connection.introspection.table_names())
        required_tables = {
            "shop",
            "designer",
            "client",
            "client_survey",
            "client_analysis",
            "client_result",
            "client_result_detail",
            "hairstyle",
        }
        if required_tables.issubset(existing_tables):
            return
        call_command("prepare_model_team_schema", stdout=self.stdout)
        _table_names.cache_clear()
        _existing_legacy_tables.cache_clear()

    def _upsert_shop(self):
        business_number = _build_valid_business_number("101234567")
        consent_snapshot = {
            "agree_terms": True,
            "agree_privacy": True,
            "agree_third_party_sharing": True,
            "agree_marketing": False,
        }
        existing = LegacyShop.objects.filter(phone="01080001000").order_by("-backend_admin_id", "shop_id").first()
        if existing is None:
            return create_admin_record(
                name="테스트 매장 관리자",
                store_name="MirrAI Test Shop",
                role="owner",
                phone="01080001000",
                business_number=business_number,
                password_hash=make_password("1234"),
                consent_snapshot=consent_snapshot,
                consented_at=timezone.now(),
            )

        existing.login_id = "01080001000"
        existing.shop_name = "MirrAI Test Shop"
        existing.biz_number = business_number
        existing.owner_phone = "01080001000"
        existing.password = make_password("1234")
        existing.admin_pin = "1000"
        existing.name = "테스트 매장 관리자"
        existing.store_name = "MirrAI Test Shop"
        existing.role = "owner"
        existing.phone = "01080001000"
        existing.business_number = business_number
        existing.password_hash = make_password("1234")
        existing.is_active = True
        existing.consent_snapshot = consent_snapshot
        existing.consented_at = timezone.now()
        existing.updated_at = timezone.now().isoformat()
        existing.save()
        return get_admin_by_phone(phone="01080001000")

    def _upsert_designers(self, *, shop) -> list:
        designer_specs = (
            ("김미나", "010-8111-2001", "2468"),
            ("박준", "010-8111-2002", "1357"),
        )
        designers: list = []
        for name, phone, pin in designer_specs:
            normalized_phone = phone.replace("-", "")
            row = (
                LegacyDesigner.objects.filter(
                    backend_shop_ref_id=shop.id,
                    name=name,
                )
                .order_by("-backend_designer_id", "designer_id")
                .first()
            )
            if row is None:
                designers.append(
                    create_designer_record(
                        admin=shop,
                        name=name,
                        phone=normalized_phone,
                        pin_hash=make_password(pin),
                    )
                )
                continue

            row.shop_id = get_legacy_admin_id(admin=shop) or row.shop_id
            row.designer_name = name
            row.login_id = normalized_phone
            row.password = make_password(pin)
            row.is_active = True
            row.updated_at = timezone.now().isoformat()
            row.backend_shop_ref_id = shop.id
            row.name = name
            row.phone = normalized_phone
            row.pin_hash = make_password(pin)
            row.save()
            designers.append(get_designers_for_admin(admin=shop)[len(designers)])
        return designers

    def _upsert_clients(
        self,
        *,
        shop,
        designers: list,
        seeded_at,
    ) -> dict[str, object]:
        clients: dict[str, object] = {}
        current_year = timezone.localdate().year

        for spec in CLIENT_SPECS:
            assigned_designer = designers[spec.designer_index] if spec.designer_index is not None else None
            client = upsert_client_record(
                phone=spec.phone,
                name=spec.name,
                gender=spec.gender,
                age_input=spec.age_input,
                birth_year_estimate=current_year - spec.age_input,
                shop=shop,
                designer=assigned_designer,
                assignment_source=spec.assignment_source,
            )
            upsert_survey(client, spec.survey)
            clients[spec.phone] = client
        return clients

    def _upsert_downstream_data(
        self,
        *,
        shop,
        clients: dict[str, object],
    ) -> None:
        for spec in DOWNSTREAM_SPECS:
            client = clients[spec.phone]
            survey = get_latest_survey(client)
            self._reset_legacy_downstream(client=client)
            capture = self._upsert_capture(client=client)
            analysis = self._upsert_analysis(client=client, capture=capture, spec=spec)

            _, rows = persist_generated_batch(
                client=client,
                capture_record=capture,
                survey=survey,
                analysis=analysis,
            )

            chosen_row = None
            if spec.choose_rank is not None:
                chosen_row = next((row for row in rows if row.rank == spec.choose_rank), None)
                if chosen_row is not None:
                    confirm_style_selection(
                        client=client,
                        recommendation_id=chosen_row.id,
                        admin_id=get_legacy_admin_id(admin=shop),
                        source="seed_test_accounts",
                    )

    def _reset_legacy_downstream(self, *, client) -> None:
        LegacyClientResultDetail.objects.filter(backend_client_ref_id=client.id).delete()
        LegacyClientResult.objects.filter(backend_client_ref_id=client.id).delete()
        LegacyClientAnalysis.objects.filter(backend_client_ref_id=client.id).delete()

    def _upsert_capture(self, *, client):
        return create_legacy_capture_upload_record(
            client=client,
            original_path=f"seed/captures/{client.phone}/original.jpg",
            processed_path=f"seed/captures/{client.phone}/processed.jpg",
            filename=f"seed-client-{client.phone}.jpg",
            status="DONE",
            face_count=1,
            landmark_snapshot={
                "left_eye": {"point": {"x": 0.35, "y": 0.38}},
                "right_eye": {"point": {"x": 0.65, "y": 0.38}},
                "mouth_center": {"point": {"x": 0.5, "y": 0.68}},
                "chin_center": {"point": {"x": 0.5, "y": 0.88}},
            },
            deidentified_path=f"seed/captures/{client.phone}/deidentified.jpg",
            privacy_snapshot={
                "retention": "seed_test_accounts",
                "consent_verified": True,
            },
            error_note="",
        )

    def _upsert_analysis(self, *, client, capture, spec: DownstreamSeedSpec):
        _, analysis = complete_legacy_capture_analysis(
            record_id=capture.id,
            face_shape=spec.face_shape,
            golden_ratio_score=spec.golden_ratio_score,
            landmark_snapshot={
                "face_shape": spec.face_shape,
                "seeded": True,
                "client_phone": client.phone,
            },
        )
        return analysis

    def _upsert_consultation_notes(
        self,
        *,
        shop,
        designers: list,
        clients: dict[str, object],
    ) -> None:
        note_specs = (
            (
                clients["01090001001"],
                designers[0],
                "고객은 자연스러운 레이어드 컷과 부드러운 컬감을 선호한다고 전달했습니다.",
            ),
            (
                clients["01090001002"],
                designers[1],
                "옆머리는 깔끔하게 정리하고, 전체 길이는 짧고 단정한 느낌을 원합니다.",
            ),
        )

        for client, designer, note_content in note_specs:
            legacy_items = get_legacy_active_consultation_items(client=client) or []
            if not legacy_items:
                continue
            create_client_note(
                client=client,
                consultation_id=int(legacy_items[0]["consultation_id"]),
                content=note_content,
                admin=shop,
                designer=designer,
            )
