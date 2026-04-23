from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

from app.session_state import (
    clear_customer_session,
    clear_designer_session,
    has_customer_session,
    has_designer_session,
)


class ElasticBeanstalkHealthCheckMiddleware(MiddlewareMixin):
    """
    Return a lightweight 200 response for ALB/ELB health checks before host,
    session, database, and template work can run.
    """

    HEALTH_PATHS = {"/health", "/health/", "/api/health", "/api/health/"}
    ROOT_HEALTH_PATHS = {"/"}
    HEALTHCHECK_USER_AGENT_PREFIXES = (
        "ELB-HealthChecker/",
        "HealthChecker/",
        "Amazon-Route53-Health-Check-Service",
    )

    def process_request(self, request):
        user_agent = str(request.META.get("HTTP_USER_AGENT") or "")
        is_health_path = request.path in self.HEALTH_PATHS
        is_root_healthcheck = request.path in self.ROOT_HEALTH_PATHS and user_agent.startswith(
            self.HEALTHCHECK_USER_AGENT_PREFIXES
        )

        if not is_health_path and not is_root_healthcheck:
            return None

        response = JsonResponse({"status": "ok", "framework": "Django"})
        response["Cache-Control"] = "no-store"
        return response


class BrowserSessionCleanupMiddleware(MiddlewareMixin):
    """
    Clear customer/designer session state on a fresh browser session while
    leaving the admin session intact.
    """

    def _flush_stale_cached_session_if_needed(self, request):
        session_key = getattr(request.session, "session_key", None)
        if not session_key:
            return False

        try:
            session_exists = request.session.exists(session_key)
        except Exception:
            return False

        if session_exists:
            return False

        request.session.flush()
        return True

    def process_request(self, request):
        if self._flush_stale_cached_session_if_needed(request):
            return None

        if request.COOKIES.get("browser_active"):
            return None

        has_customer = has_customer_session(request=request)
        has_designer = has_designer_session(request=request)

        # Avoid touching anonymous sessions on the first visit. This prevents
        # unnecessary session saves and stale cached-session update errors.
        if not has_customer and not has_designer:
            return None

        if has_customer:
            clear_customer_session(request=request)
        if has_designer:
            clear_designer_session(request=request)
        return None

    def process_response(self, request, response):
        if not request.COOKIES.get("browser_active"):
            response.set_cookie("browser_active", "1", max_age=None, httponly=True)
        return response
