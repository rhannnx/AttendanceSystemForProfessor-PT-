from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import datetime


class Professor(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    employee_id = models.CharField(max_length=20, unique=True)
    department = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=15)
    qr_code = models.ImageField(upload_to='qr_codes/', null=True, blank=True)
    # ✅ NEW — expected time-in per professor
    expected_time_in = models.TimeField(
        null=True, blank=True,
        help_text="Expected time-in (e.g. 08:00). Used to determine if professor is late."
    )

    def __str__(self):
        return f"{self.user.last_name}, {self.user.first_name}"


class AttendanceDay(models.Model):
    professor = models.ForeignKey(
        Professor,
        on_delete=models.CASCADE,
        related_name='attendance_days'
    )
    date = models.DateField(auto_now_add=True)
    time_in = models.DateTimeField(auto_now_add=True)
    time_out = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        unique_together = ['professor', 'date']
        verbose_name_plural = "Daily Attendance (Presence)"

    def __str__(self):
        return f"{self.professor.user.get_full_name()} - {self.date}"

    @property
    def duration(self):
        """Total time spent on campus."""
        if self.time_out:
            return self.time_out - self.time_in
        return None

    @property
    def is_timed_out(self):
        """Check if professor has already timed out."""
        return self.time_out is not None

    # ✅ NEW — Late detection
    @property
    def is_late(self):
        """Check if professor timed in after their expected time."""
        if not self.professor.expected_time_in:
            return False  # No expected time set → not marked late
        actual_time = timezone.localtime(self.time_in).time()
        return actual_time > self.professor.expected_time_in

    # ✅ NEW — Minutes late
    @property
    def minutes_late(self):
        """How many minutes late the professor is."""
        if not self.is_late:
            return 0
        expected = datetime.combine(
            self.date,
            self.professor.expected_time_in
        )
        actual = datetime.combine(
            self.date,
            timezone.localtime(self.time_in).time()
        )
        diff = actual - expected
        return int(diff.total_seconds() / 60)


class AttendanceSession(models.Model):
    day_record = models.ForeignKey(
        AttendanceDay,
        on_delete=models.CASCADE,
        related_name='sessions'
    )
    classroom = models.CharField(max_length=50)
    subject_code = models.CharField(max_length=20)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "Session Logs (Activity)"

    def __str__(self):
        return f"{self.subject_code} ({self.classroom}) - {self.day_record.date}"

    @property
    def duration(self):
        """How long the session lasted."""
        if self.end_time:
            return self.end_time - self.start_time
        return None

    @property
    def is_ongoing(self):
        """Check if session is still ongoing."""
        return self.end_time is None