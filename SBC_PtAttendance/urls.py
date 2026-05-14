"""
URL configuration for SBC_PtAttendance project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from record_tracker.views import (
    # ── Helpers ───────────────────────────────────────────
    check_ip,
    # ── Auth ──────────────────────────────────────────────
    custom_login, custom_logout,
    # ── HR views ──────────────────────────────────────────
    hr_dashboard, professors_view, attendance_view,
    add_professor, edit_professor, delete_professor,
    sessions_view, notifications_view,
    reports_view, qr_codes_view, analytics_view,
    generate_qr, generate_all_qr,
    generate_pdf, generate_pdf_all, generate_pdf_range,
    # ── Professor portal views ────────────────────────────
    professor_dashboard, professor_attendance,
    professor_sessions, professor_qr, professor_profile,
)

urlpatterns = [
    # ── Auth ──────────────────────────────────────────────────────────────────
    path('login/',  custom_login,  name='login'),
    path('logout/', custom_logout, name='logout'),

    # ── HR Dashboard ──────────────────────────────────────────────────────────
    path('', hr_dashboard, name='dashboard'),

    # ── Professors ────────────────────────────────────────────────────────────
    path('professors/', professors_view, name='professors'),
    path('professors/add/', add_professor, name='add_professor'),
    path('professors/<int:professor_id>/edit/', edit_professor, name='edit_professor'),
    path('professors/<int:professor_id>/delete/', delete_professor, name='delete_professor'),

    # ── Attendance & Sessions ─────────────────────────────────────────────────
    path('attendance/', attendance_view, name='attendance'),
    path('sessions/', sessions_view, name='sessions'),
    path('notifications/', notifications_view, name='notifications'),

    # ── Reports ───────────────────────────────────────────────────────────────
    path('reports/', reports_view, name='reports'),
    path('reports/generate/<int:professor_id>/', generate_pdf, name='generate_pdf'),
    path('reports/generate-all/', generate_pdf_all, name='generate_pdf_all'),
    path('reports/generate-range/', generate_pdf_range, name='generate_pdf_range'),

    # ── QR Codes ──────────────────────────────────────────────────────────────
    path('qr-codes/', qr_codes_view, name='qr_codes'),
    path('qr-codes/generate-all/', generate_all_qr, name='generate_all_qr'),
    path('qr-codes/generate/<int:professor_id>/', generate_qr, name='generate_qr'),

    # ── Analytics ─────────────────────────────────────────────────────────────
    path('analytics/', analytics_view, name='analytics'),

    # ── Professor Portal ──────────────────────────────────────────────────────
    path('professor/', professor_dashboard, name='professor_dashboard'),
    path('professor/attendance/', professor_attendance, name='professor_attendance'),
    path('professor/sessions/', professor_sessions, name='professor_sessions'),
    path('professor/qr/', professor_qr, name='professor_qr'),
    path('professor/profile/', professor_profile, name='professor_profile'),

    # ── System ────────────────────────────────────────────────────────────────
    path('check-ip/', check_ip),
    path('admin/', admin.site.urls),
    path('api/', include('record_tracker.urls')),
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)