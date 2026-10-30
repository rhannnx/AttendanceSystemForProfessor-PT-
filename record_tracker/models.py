from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import datetime, timedelta


class Professor(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    employee_id = models.CharField(max_length=20, unique=True)
    department = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=15)
    qr_code = models.ImageField(upload_to='qr_codes/', null=True, blank=True)
    expected_time_in = models.TimeField(
        null=True, blank=True,
        help_text="General expected time-in. Used as fallback if no schedule exists."
    )

    def __str__(self):
        return f"{self.user.last_name}, {self.user.first_name}"


class Semester(models.Model):
    name       = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date   = models.DateField()
    is_active  = models.BooleanField(default=False)

    class Meta:
        ordering = ['-start_date']
        verbose_name_plural = "Semesters"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_active:
            Semester.objects.exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_active(cls):
        today = timezone.localdate()
        return cls.objects.filter(
            is_active=True,
            start_date__lte=today,
            end_date__gte=today,
        ).first()


class ClassSchedule(models.Model):
    DAY_CHOICES = [
        ('MON', 'Monday'),
        ('TUE', 'Tuesday'),
        ('WED', 'Wednesday'),
        ('THU', 'Thursday'),
        ('FRI', 'Friday'),
        ('SAT', 'Saturday'),
    ]

    WEEKDAY_MAP = {
        0: 'MON', 1: 'TUE', 2: 'WED',
        3: 'THU', 4: 'FRI', 5: 'SAT', 6: 'SUN'
    }

    semester     = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='schedules')
    professor    = models.ForeignKey(Professor, on_delete=models.CASCADE, related_name='schedules')
    subject_code = models.CharField(max_length=20)
    subject_name = models.CharField(max_length=100, blank=True)
    classroom    = models.CharField(max_length=50)
    days         = models.CharField(max_length=30)
    start_time   = models.TimeField()
    end_time     = models.TimeField()
    grace_period = models.IntegerField(default=15)

    class Meta:
        verbose_name_plural = "Class Schedules"

    def __str__(self):
        return f"{self.professor.user.get_full_name()} — {self.subject_code} ({self.days})"

    def get_days_list(self):
        return [d.strip() for d in self.days.split(',') if d.strip()]

    def get_days_display(self):
        day_map = dict(self.DAY_CHOICES)
        return ', '.join([day_map.get(d, d)[:3] for d in self.get_days_list()])

    @property
    def deadline_time(self):
        dt = datetime.combine(datetime.today(), self.start_time)
        return (dt + timedelta(minutes=self.grace_period)).time()

    def is_scheduled_today(self, date=None):
        if date is None:
            date = timezone.localdate()
        day_code = self.WEEKDAY_MAP.get(date.weekday())
        return day_code in self.get_days_list()

    def check_lateness(self, actual_time_in):
        actual = timezone.localtime(actual_time_in).time()
        if actual > self.deadline_time:
            dt_deadline = datetime.combine(datetime.today(), self.deadline_time)
            dt_actual   = datetime.combine(datetime.today(), actual)
            mins = int((dt_actual - dt_deadline).total_seconds() / 60)
            return True, mins
        return False, 0


class AttendanceDay(models.Model):
    professor  = models.ForeignKey(Professor, on_delete=models.CASCADE, related_name='attendance_days')
    date       = models.DateField(auto_now_add=True)
    time_in    = models.DateTimeField(auto_now_add=True)
    time_out   = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        unique_together = ['professor', 'date']
        verbose_name_plural = "Daily Attendance (Presence)"

    def __str__(self):
        return f"{self.professor.user.get_full_name()} - {self.date}"

    @property
    def duration(self):
        if self.time_out:
            return self.time_out - self.time_in
        return None

    @property
    def is_timed_out(self):
        return self.time_out is not None

    @property
    def is_late(self):
        sessions = self.sessions.filter(schedule__isnull=False)
        if sessions.exists():
            return any(s.is_late for s in sessions)
        if not self.professor.expected_time_in:
            return False
        actual_time = timezone.localtime(self.time_in).time()
        return actual_time > self.professor.expected_time_in

    @property
    def minutes_late(self):
        if not self.is_late:
            return 0
        sessions = self.sessions.filter(schedule__isnull=False, is_late_flag=True)
        if sessions.exists():
            return max(s.minutes_late for s in sessions)
        if not self.professor.expected_time_in:
            return 0
        expected = datetime.combine(self.date, self.professor.expected_time_in)
        actual   = datetime.combine(self.date, timezone.localtime(self.time_in).time())
        return max(0, int((actual - expected).total_seconds() / 60))


class AttendanceSession(models.Model):
    day_record   = models.ForeignKey(AttendanceDay, on_delete=models.CASCADE, related_name='sessions')
    schedule     = models.ForeignKey(ClassSchedule, on_delete=models.SET_NULL, null=True, blank=True, related_name='sessions')
    classroom    = models.CharField(max_length=50)
    subject_code = models.CharField(max_length=20)
    start_time   = models.DateTimeField(auto_now_add=True)
    end_time     = models.DateTimeField(null=True, blank=True)
    is_late_flag = models.BooleanField(default=False)
    minutes_late = models.IntegerField(default=0)

    class Meta:
        verbose_name_plural = "Session Logs (Activity)"

    def __str__(self):
        return f"{self.subject_code} ({self.classroom}) - {self.day_record.date}"

    @property
    def duration(self):
        if self.end_time:
            return self.end_time - self.start_time
        return None

    @property
    def is_ongoing(self):
        return self.end_time is None

    @property
    def is_late(self):
        return self.is_late_flag

    @property
    def scheduled_start(self):
        if self.schedule:
            return self.schedule.start_time
        return None

    @property
    def deadline(self):
        if self.schedule:
            return self.schedule.deadline_time
        return None

    # ✅ NEW — Session status based on scheduled time
    @property
    def session_status(self):
        """
        Returns: 'upcoming', 'ongoing', or 'finished'
        Based on current time vs scheduled start/end time.
        """
        now = timezone.localtime(timezone.now()).time()

        if self.schedule:
            scheduled_start = self.schedule.start_time
            scheduled_end   = self.schedule.end_time

            if now < scheduled_start:
                return 'upcoming'
            elif now > scheduled_end:
                return 'finished'
            else:
                return 'ongoing'
        else:
            # No schedule linked — use end_time
            if self.end_time:
                return 'finished'
            return 'ongoing'

    @property
    def session_status_display(self):
        status = self.session_status
        return {
            'upcoming': 'Upcoming',
            'ongoing':  'Ongoing',
            'finished': 'Finished',
        }.get(status, 'Unknown')


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('CREATE', 'Create'),
        ('UPDATE', 'Update'),
        ('DELETE', 'Delete'),
        ('TIMEIN', 'Time In'),
        ('TIMEOUT', 'Time Out'),
        ('QR_GENERATE', 'QR Generate'),
        ('LOGIN', 'Login'),
        ('OTHER', 'Other'),
    ]
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    old_values = models.JSONField(null=True, blank=True)
    new_values = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'

    def __str__(self):
        return f'{self.timestamp} - {self.action} - {self.model_name}'
