from __future__ import annotations

from typing import Any, Protocol


class RuntimeClient(Protocol):
    """Runtime customer object returned by model_team_bridge.

    The physical source is Supabase `client`; `id` is the backend numeric
    reference, while `legacy_client_id` is the Supabase/model-team `client_id`.
    """

    id: int
    legacy_client_id: str | None
    name: str
    phone: str
    gender: str | None
    shop_id: int | None
    designer_id: int | None
    shop: Any | None
    designer: Any | None
    age_input: int | None
    birth_year_estimate: int | None
