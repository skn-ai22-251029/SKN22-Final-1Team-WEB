from django.shortcuts import render
from django.urls import path

from app.front_views import (
    admin_dashboard_page,
    admin_login_page,
    client_camera_page,
    client_login_page,
    client_recommendation_page,
    client_survey_page,
    health_check,
    home_page,
)


urlpatterns = [
    path("", home_page, name="index"),
    path("health/", health_check, name="health-check"),
    path("docs/", lambda r: render(r, "pages/home.html"), name="docs"),
    path("customer/", client_login_page, name="customer_index"),
    path("customer/survey/", client_survey_page, name="customer_survey"),
    path("customer/camera/", client_camera_page, name="customer_camera"),
    path("customer/recommendations/", client_recommendation_page, name="customer_result"),
    path("customer/logout/", lambda r: render(r, "index.html"), name="customer_logout"),
    path("demo/discovery/", lambda r: render(r, "demo/discovery.html"), name="demo_discovery"),
    path("partner/", admin_login_page, name="partner_index"),
    path("partner/signup/", admin_login_page, name="partner_signup"),
    path("partner/dashboard/", admin_dashboard_page, name="partner_dashboard"),
    # Legacy aliases kept for older links and docs.
    path("client/login/", client_login_page, name="client-login-shell"),
    path("client/survey/", client_survey_page, name="client-survey-shell"),
    path("client/recommendations/", client_recommendation_page, name="client-recommendation-shell"),
    path("admin-panel/login/", admin_login_page, name="admin-login-shell"),
    path("admin-panel/dashboard/", admin_dashboard_page, name="admin-dashboard-shell"),
]
