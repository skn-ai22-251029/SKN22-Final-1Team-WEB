from django.test import Client, SimpleTestCase, override_settings


@override_settings(
    ALLOWED_HOSTS=["mirrai.shop"],
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
)
class ElasticBeanstalkHealthCheckMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def test_elb_healthcheck_root_path_bypasses_host_validation(self):
        response = self.client.get(
            "/",
            HTTP_HOST="10.0.0.10",
            HTTP_USER_AGENT="ELB-HealthChecker/2.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok", "framework": "Django"},
        )

    def test_non_healthcheck_user_agent_still_rejects_untrusted_host(self):
        response = self.client.get(
            "/",
            HTTP_HOST="10.0.0.10",
            HTTP_USER_AGENT="Mozilla/5.0",
        )

        self.assertEqual(response.status_code, 400)

    def test_elb_healthcheck_health_path_bypasses_host_validation(self):
        response = self.client.get(
            "/health/",
            HTTP_HOST="10.0.0.10",
            HTTP_USER_AGENT="ELB-HealthChecker/2.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok", "framework": "Django"},
        )

    def test_health_path_without_elb_user_agent_bypasses_host_validation(self):
        response = self.client.get(
            "/health/",
            HTTP_HOST="10.0.0.10",
            HTTP_USER_AGENT="Mozilla/5.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok", "framework": "Django"},
        )

    def test_health_path_without_trailing_slash_bypasses_redirect_and_host_validation(self):
        response = self.client.get(
            "/health",
            HTTP_HOST="10.0.0.10",
            HTTP_USER_AGENT="Mozilla/5.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok", "framework": "Django"},
        )
