from __future__ import annotations

from importlib.util import find_spec
import socket
from urllib.request import urlopen


def unique_values(*groups) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for group in groups:
        for raw_value in group or []:
            value = str(raw_value or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
    return values


def metadata_local_ipv4(*, timeout: float = 0.2) -> str | None:
    try:
        with urlopen("http://169.254.169.254/latest/meta-data/local-ipv4", timeout=timeout) as response:
            return response.read().decode("utf-8").strip()
    except Exception:
        return None


def build_allowed_hosts(*, default_hosts: list[str], env_hosts: list[str]) -> list[str]:
    dynamic_hosts = [
        metadata_local_ipv4(),
        socket.gethostname(),
    ]
    try:
        dynamic_hosts.append(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    return unique_values(default_hosts, env_hosts, dynamic_hosts)


def resolve_active_database_url(
    *,
    supabase_use_remote_db: bool,
    supabase_db_url: str,
    local_database_url: str,
    database_url: str,
) -> str:
    if supabase_use_remote_db and str(supabase_db_url or "").strip():
        return str(supabase_db_url).strip()
    if str(local_database_url or "").strip():
        return str(local_database_url).strip()
    if str(database_url or "").strip():
        return str(database_url).strip()
    return "sqlite:///db.sqlite3"


def build_cache_settings(*, redis_url: str, timeout: int, key_prefix: str) -> dict:
    redis_url = str(redis_url or "").strip()
    redis_available = find_spec("redis") is not None

    if redis_url and redis_available:
        return {
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": redis_url,
                "TIMEOUT": timeout,
                "KEY_PREFIX": key_prefix,
                "OPTIONS": {
                    "socket_connect_timeout": 5,
                    "socket_timeout": 5,
                    "retry_on_timeout": True,
                    "health_check_interval": 30,
                },
            }
        }

    # 로컬/테스트 환경에서는 redis 패키지나 서버가 없어도 안전하게 동작하도록 locmem으로 내려간다.
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "mirrai-local-cache",
            "TIMEOUT": timeout,
            "KEY_PREFIX": key_prefix,
        }
    }
