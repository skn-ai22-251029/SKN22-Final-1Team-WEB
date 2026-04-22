from __future__ import annotations

import json
from dataclasses import dataclass

from django.core.management.base import BaseCommand

from app.models_model_team import LegacyClient, LegacyClientResult, LegacyClientResultDetail


def _normalize_phone(value: str | None) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def _normalize_name(value: str | None) -> str:
    return "".join(str(value or "").split()).casefold()


@dataclass
class AuditStats:
    client_count: int = 0
    result_count: int = 0
    detail_count: int = 0
    duplicate_identity_group_count: int = 0
    duplicate_identity_client_count: int = 0
    orphan_result_count: int = 0
    inconsistent_result_ref_count: int = 0
    fix_result_backend_client_ref_count: int = 0
    fix_result_legacy_client_ref_count: int = 0
    fix_result_admin_scope_ref_count: int = 0
    fix_detail_backend_client_ref_count: int = 0


class Command(BaseCommand):
    help = (
        "Audit client/history scope consistency. "
        "By default runs dry-run; pass --apply to persist safe reference fixes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply safe fixes for result/detail client/admin reference mismatches.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Print report as JSON.",
        )

    def handle(self, *args, **options):
        apply_changes: bool = bool(options.get("apply"))
        as_json: bool = bool(options.get("as_json"))

        stats = AuditStats()
        report: dict[str, object] = {"mode": "apply" if apply_changes else "dry-run"}

        clients = list(
            LegacyClient.objects.all().values(
                "client_id",
                "backend_client_id",
                "shop_id",
                "backend_shop_ref_id",
                "name",
                "client_name",
                "phone",
            )
        )
        stats.client_count = len(clients)

        by_backend_client_id: dict[int, dict] = {}
        by_legacy_client_id: dict[str, dict] = {}
        identity_groups: dict[tuple[str, str], list[dict]] = {}

        for row in clients:
            backend_client_id = row.get("backend_client_id")
            legacy_client_id = str(row.get("client_id") or "").strip()
            if backend_client_id is not None:
                by_backend_client_id[int(backend_client_id)] = row
            if legacy_client_id:
                by_legacy_client_id[legacy_client_id] = row

            normalized_phone = _normalize_phone(row.get("phone"))
            normalized_name = _normalize_name(row.get("name") or row.get("client_name"))
            if normalized_phone and normalized_name:
                identity_groups.setdefault((normalized_phone, normalized_name), []).append(row)

        duplicate_groups_payload: list[dict] = []
        for (phone_key, name_key), group_rows in identity_groups.items():
            shop_keys = {
                (
                    str(item.get("shop_id") or ""),
                    int(item["backend_shop_ref_id"]) if item.get("backend_shop_ref_id") is not None else None,
                )
                for item in group_rows
            }
            if len(group_rows) > 1 and len(shop_keys) > 1:
                duplicate_groups_payload.append(
                    {
                        "normalized_phone": phone_key,
                        "normalized_name": name_key,
                        "shop_count": len(shop_keys),
                        "client_count": len(group_rows),
                        "clients": [
                            {
                                "client_id": item.get("client_id"),
                                "backend_client_id": item.get("backend_client_id"),
                                "shop_id": item.get("shop_id"),
                                "backend_shop_ref_id": item.get("backend_shop_ref_id"),
                                "name": item.get("name") or item.get("client_name"),
                                "phone": item.get("phone"),
                            }
                            for item in group_rows
                        ],
                    }
                )

        stats.duplicate_identity_group_count = len(duplicate_groups_payload)
        stats.duplicate_identity_client_count = sum(int(item.get("client_count") or 0) for item in duplicate_groups_payload)
        report["duplicate_identity_groups"] = duplicate_groups_payload

        result_updates_applied = 0
        detail_updates_applied = 0
        orphan_result_ids: list[int] = []
        inconsistent_result_ids: list[int] = []
        resolved_backend_client_by_result_id: dict[int, int | None] = {}

        result_rows = list(
            LegacyClientResult.objects.all().values(
                "result_id",
                "client_id",
                "backend_client_ref_id",
                "backend_admin_ref_id",
            )
        )
        stats.result_count = len(result_rows)

        for row in result_rows:
            result_id = int(row.get("result_id"))
            legacy_client_id = str(row.get("client_id") or "").strip()
            backend_client_ref_id = row.get("backend_client_ref_id")

            client_from_backend = (
                by_backend_client_id.get(int(backend_client_ref_id))
                if backend_client_ref_id is not None
                else None
            )
            client_from_legacy = by_legacy_client_id.get(legacy_client_id) if legacy_client_id else None

            if client_from_backend is None and client_from_legacy is None:
                stats.orphan_result_count += 1
                orphan_result_ids.append(result_id)
                resolved_backend_client_by_result_id[result_id] = None
                continue

            if (
                client_from_backend is not None
                and client_from_legacy is not None
                and int(client_from_backend.get("backend_client_id"))
                != int(client_from_legacy.get("backend_client_id"))
            ):
                stats.inconsistent_result_ref_count += 1
                inconsistent_result_ids.append(result_id)
                resolved_backend_client_by_result_id[result_id] = int(client_from_backend.get("backend_client_id"))
                continue

            resolved_client = client_from_backend or client_from_legacy
            resolved_backend_client_id = (
                int(resolved_client.get("backend_client_id"))
                if resolved_client and resolved_client.get("backend_client_id") is not None
                else None
            )
            resolved_legacy_client_id = str(resolved_client.get("client_id") or "") if resolved_client else ""
            resolved_backend_shop_ref_id = (
                int(resolved_client.get("backend_shop_ref_id"))
                if resolved_client and resolved_client.get("backend_shop_ref_id") is not None
                else None
            )
            resolved_backend_client_by_result_id[result_id] = resolved_backend_client_id

            update_fields: dict[str, object] = {}

            if backend_client_ref_id is None and resolved_backend_client_id is not None:
                stats.fix_result_backend_client_ref_count += 1
                update_fields["backend_client_ref_id"] = resolved_backend_client_id

            if not legacy_client_id and resolved_legacy_client_id:
                stats.fix_result_legacy_client_ref_count += 1
                update_fields["client_id"] = resolved_legacy_client_id

            current_backend_admin_ref = row.get("backend_admin_ref_id")
            if (
                resolved_backend_shop_ref_id is not None
                and current_backend_admin_ref is not None
                and int(current_backend_admin_ref) != int(resolved_backend_shop_ref_id)
            ):
                stats.fix_result_admin_scope_ref_count += 1
                update_fields["backend_admin_ref_id"] = resolved_backend_shop_ref_id
            elif resolved_backend_shop_ref_id is not None and current_backend_admin_ref is None:
                stats.fix_result_admin_scope_ref_count += 1
                update_fields["backend_admin_ref_id"] = resolved_backend_shop_ref_id

            if apply_changes and update_fields:
                LegacyClientResult.objects.filter(result_id=result_id).update(**update_fields)
                result_updates_applied += 1

        detail_rows = list(
            LegacyClientResultDetail.objects.all().values(
                "detail_id",
                "result_id",
                "backend_client_ref_id",
            )
        )
        stats.detail_count = len(detail_rows)
        for row in detail_rows:
            result_id = int(row.get("result_id"))
            detail_id = int(row.get("detail_id"))
            current_backend_client_ref_id = row.get("backend_client_ref_id")
            expected_backend_client_ref_id = resolved_backend_client_by_result_id.get(result_id)
            if expected_backend_client_ref_id is None:
                continue

            if current_backend_client_ref_id is None:
                stats.fix_detail_backend_client_ref_count += 1
                if apply_changes:
                    LegacyClientResultDetail.objects.filter(detail_id=detail_id).update(
                        backend_client_ref_id=expected_backend_client_ref_id
                    )
                    detail_updates_applied += 1
                continue

            if int(current_backend_client_ref_id) != int(expected_backend_client_ref_id):
                stats.fix_detail_backend_client_ref_count += 1
                if apply_changes:
                    LegacyClientResultDetail.objects.filter(detail_id=detail_id).update(
                        backend_client_ref_id=expected_backend_client_ref_id
                    )
                    detail_updates_applied += 1

        report["orphan_result_ids"] = orphan_result_ids[:100]
        report["inconsistent_result_ids"] = inconsistent_result_ids[:100]
        report["stats"] = stats.__dict__
        report["applied"] = {
            "result_rows_updated": result_updates_applied,
            "detail_rows_updated": detail_updates_applied,
        }

        if as_json:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return

        mode_label = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(self.style.SUCCESS(f"[{mode_label}] client/history scope audit"))
        self.stdout.write(f"- clients: {stats.client_count}")
        self.stdout.write(f"- results: {stats.result_count}")
        self.stdout.write(f"- details: {stats.detail_count}")
        self.stdout.write(f"- duplicate identity groups (cross-shop): {stats.duplicate_identity_group_count}")
        self.stdout.write(f"- duplicate identity clients (cross-shop): {stats.duplicate_identity_client_count}")
        self.stdout.write(f"- orphan results: {stats.orphan_result_count}")
        self.stdout.write(f"- inconsistent result refs: {stats.inconsistent_result_ref_count}")
        self.stdout.write(
            f"- fix candidates: result.backend_client_ref={stats.fix_result_backend_client_ref_count}, "
            f"result.client_id={stats.fix_result_legacy_client_ref_count}, "
            f"result.backend_admin_ref={stats.fix_result_admin_scope_ref_count}, "
            f"detail.backend_client_ref={stats.fix_detail_backend_client_ref_count}"
        )
        self.stdout.write(
            f"- applied updates: result={result_updates_applied}, detail={detail_updates_applied}"
        )
