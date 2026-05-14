from django.contrib import admin
from .models import Professor, AttendanceDay, AttendanceSession


@admin.register(Professor)
class ProfessorAdmin(admin.ModelAdmin):
    list_display = (
        'employee_id',
        'get_full_name',        # ✅ Show full name instead of raw user object
        'department',
        'phone_number',
    )
    search_fields = (
        'employee_id',
        'user__last_name',
        'user__first_name',
        'department',           # ✅ HR can also search by department
    )
    ordering = ('employee_id',)

    @admin.display(description='Full Name')
    def get_full_name(self, obj):
        return obj.user.get_full_name()


@admin.register(AttendanceDay)
class AttendanceDayAdmin(admin.ModelAdmin):
    list_display = (
        'professor',
        'date',
        'time_in',
        'time_out',
        'ip_address',
        'get_duration',
        'is_timed_out',      # ✅ OK in list_display
    )
    list_filter = (
        'date',
        'professor__department',
        # ❌ Removed 'is_timed_out' — can't filter by a property
    )
    search_fields = (
        'professor__user__last_name',
        'professor__user__first_name',
        'professor__employee_id',
    )
    readonly_fields = (
        'time_in',
        'date',
        'ip_address',
    )
    date_hierarchy = 'date'
    ordering = ('-date', '-time_in')

    @admin.display(description='Duration on Campus')
    def get_duration(self, obj):
        if obj.duration:
            total_seconds = int(obj.duration.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            return f"{hours}h {minutes}m"
        return "Still on campus" if not obj.is_timed_out else "—"


@admin.register(AttendanceSession)
class AttendanceSessionAdmin(admin.ModelAdmin):
    list_display = (
        'get_professor_name',   # ✅ Show professor name directly
        'subject_code',
        'classroom',
        'start_time',
        'end_time',
        'get_duration',         # ✅ Show session duration
        'is_ongoing',           # ✅ Quick status check
    )
    list_filter = (
        'classroom',
        'subject_code',
        'day_record__professor__department',  # ✅ Filter by department
    )
    search_fields = (
        'subject_code',
        'classroom',
        'day_record__professor__user__last_name',   # ✅ Search by professor name
        'day_record__professor__user__first_name',
        'day_record__professor__employee_id',
    )
    readonly_fields = (
        'start_time',           # ✅ Prevent manual editing
    )
    date_hierarchy = 'start_time'   # ✅ Browse sessions by date
    ordering = ('-start_time',)

    @admin.display(description='Professor')
    def get_professor_name(self, obj):
        return obj.day_record.professor.user.get_full_name()

    @admin.display(description='Session Duration')
    def get_duration(self, obj):
        if obj.duration:
            total_seconds = int(obj.duration.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            return f"{hours}h {minutes}m"
        return "Ongoing" if obj.is_ongoing else "—"