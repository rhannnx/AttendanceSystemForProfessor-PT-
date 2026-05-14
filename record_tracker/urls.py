from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ProfessorViewSet, AttendanceDayViewSet, AttendanceSessionViewSet

router = DefaultRouter()
router.register(r'professors', ProfessorViewSet)
router.register(r'attendance-days', AttendanceDayViewSet)
router.register(r'sessions', AttendanceSessionViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
