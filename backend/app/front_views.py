from django.http import JsonResponse
from django.shortcuts import render


def health_check(request):
    return JsonResponse({"status": "django_running", "framework": "Django"})


def home_page(request):
    return render(request, "index.html", {"start_url": "/customer/", "partner_url": "/partner/login/"})


def client_login_page(request):
    return render(request, "customer/index.html")


def client_survey_page(request):
    return render(request, "customer/survey.html")


def client_camera_page(request):
    return render(request, "customer/camera.html")


def client_recommendation_page(request):
    return render(request, "customer/result.html")


def admin_login_page(request):
    return render(request, "admin/login.html")


def admin_dashboard_page(request):
    return render(request, "admin/index.html")
