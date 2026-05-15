from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.cache import cache
from .models import Professor, AttendanceDay, AttendanceSession, Semester, ClassSchedule
from .serializers import ProfessorSerializer, AttendanceDaySerializer, AttendanceSessionSerializer
from .utils import send_hr_notification, send_hr_email, is_on_school_network
import datetime
import qrcode
import io
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from django.contrib.auth import authenticate, login, logout


# ── Helpers ───────────────────────────────────────────────────────────────────

def custom_logout(request):
    logout(request)
    return redirect('login')


def check_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR', '')
    return JsonResponse({'your_ip': ip})


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def format_duration(td):
    if td is None:
        return None
    total_minutes = int(td.total_seconds() // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def is_rate_limited(key, max_requests=5, timeout=60):
    count = cache.get(key, 0)
    if count >= max_requests:
        return True
    cache.set(key, count + 1, timeout=timeout)
    return False


def _generate_qr_for_professor(professor, request):
    """QR points to public /timein/ page — handles both time-in AND time-out."""
    scan_url = request.build_absolute_uri(
        f'/timein/?professor_id={professor.id}'
    )
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(scan_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#042C53", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    filename = f"qr_professor_{professor.id}_{professor.employee_id}.png"
    professor.qr_code.save(filename, ContentFile(buffer.read()), save=True)


def _auto_create_sessions(day_record):
    """Auto-create sessions from schedule when professor times in."""
    today = day_record.date
    active_semester = Semester.get_active()
    if not active_semester:
        return

    schedules = ClassSchedule.objects.filter(
        professor=day_record.professor,
        semester=active_semester,
    )

    for schedule in schedules:
        if not schedule.is_scheduled_today(today):
            continue
        if AttendanceSession.objects.filter(
            day_record=day_record,
            schedule=schedule,
        ).exists():
            continue
        is_late, minutes_late = schedule.check_lateness(day_record.time_in)
        AttendanceSession.objects.create(
            day_record=day_record,
            schedule=schedule,
            subject_code=schedule.subject_code,
            classroom=schedule.classroom,
            is_late_flag=is_late,
            minutes_late=minutes_late,
        )


# ── Public QR Pages ───────────────────────────────────────────────────────────

def public_time_in(request):
    """
    ✅ SMART QR view — handles BOTH time-in and time-out.
    No login required. Security via WiFi check + rate limiting.

    Logic:
    → No record today       → TIME IN
    → Timed in, not out     → TIME OUT (auto)
    → Already timed out     → Show "Completed" message
    """
    professor_id = request.GET.get('professor_id')

    if not professor_id:
        return render(request, 'record_tracker/timein_result.html', {
            'status': 'error',
            'error_message': 'Invalid QR code. Professor ID missing.',
        })

    ip = get_client_ip(request)

    # ✅ Rate limiting — shared key for both timein/timeout via QR
    if is_rate_limited(f'qr_scan_{ip}', max_requests=5, timeout=60):
        return render(request, 'record_tracker/timein_result.html', {
            'status': 'error',
            'error_message': 'Too many attempts. Please wait 1 minute.',
        })

    # ✅ WiFi check
    if not is_on_school_network(request):
        return render(request, 'record_tracker/timein_result.html', {
            'status': 'error',
            'error_message': 'You must be connected to the school WiFi to record attendance.',
        })

    try:
        professor = Professor.objects.select_related('user').get(id=professor_id)
    except Professor.DoesNotExist:
        return render(request, 'record_tracker/timein_result.html', {
            'status': 'error',
            'error_message': 'Professor not found. Please contact HR.',
        })

    today = timezone.localdate()
    prof_name = professor.user.get_full_name()

    # Check existing record
    existing = AttendanceDay.objects.filter(professor=professor, date=today).first()

    # ── CASE 1: No record → TIME IN ──────────────────────────────────────────
    if not existing:
        day = AttendanceDay.objects.create(
            professor=professor,
            date=today,
            ip_address=ip,
        )
        _auto_create_sessions(day)
        day.refresh_from_db()

        late_info = f" (Late by {day.minutes_late} min(s))" if day.is_late else ""
        send_hr_notification(prof_name, f"Timed In (Arrived at Campus){late_info}")
        send_hr_email(prof_name, f"Timed In (Arrived at Campus){late_info}", request)

        sessions_display = []
        for s in day.sessions.select_related('schedule').all():
            sessions_display.append({
                'subject_code': s.subject_code,
                'classroom':    s.classroom,
                'start_time':   s.schedule.start_time.strftime('%I:%M %p') if s.schedule else '—',
                'end_time':     s.schedule.end_time.strftime('%I:%M %p') if s.schedule else '—',
                'is_late':      s.is_late,
                'minutes_late': s.minutes_late,
                'status':       s.session_status,
            })

        expected_time = '—'
        first_session = day.sessions.filter(
            schedule__isnull=False
        ).order_by('schedule__start_time').first()
        if first_session and first_session.schedule:
            expected_time = first_session.schedule.start_time.strftime('%I:%M %p')
        elif professor.expected_time_in:
            expected_time = professor.expected_time_in.strftime('%I:%M %p')

        return render(request, 'record_tracker/timein_result.html', {
            'status':        'created',
            'action':        'timein',
            'professor_name': prof_name,
            'department':    professor.department,
            'employee_id':   professor.employee_id,
            'time_in':       timezone.localtime(day.time_in).strftime('%I:%M %p'),
            'date':          today.strftime('%B %d, %Y'),
            'is_late':       day.is_late,
            'minutes_late':  day.minutes_late,
            'expected_time': expected_time,
            'sessions':      sessions_display,
        })

    # ── CASE 2: Timed in but NOT timed out → TIME OUT ────────────────────────
    elif not existing.is_timed_out:
        existing.time_out = timezone.now()
        existing.save()

        send_hr_notification(prof_name, "Timed Out (Left Campus)")
        send_hr_email(prof_name, "Timed Out (Left Campus)", request)

        return render(request, 'record_tracker/timein_result.html', {
            'status':        'timeout',
            'action':        'timeout',
            'professor_name': prof_name,
            'department':    professor.department,
            'employee_id':   professor.employee_id,
            'time_in':       timezone.localtime(existing.time_in).strftime('%I:%M %p'),
            'time_out':      timezone.localtime(existing.time_out).strftime('%I:%M %p'),
            'date':          today.strftime('%B %d, %Y'),
            'duration':      format_duration(existing.duration),
        })

    # ── CASE 3: Already timed out → Show completed message ───────────────────
    else:
        return render(request, 'record_tracker/timein_result.html', {
            'status':        'completed',
            'professor_name': prof_name,
            'time_in':       timezone.localtime(existing.time_in).strftime('%I:%M %p'),
            'time_out':      timezone.localtime(existing.time_out).strftime('%I:%M %p'),
            'date':          today.strftime('%B %d, %Y'),
            'duration':      format_duration(existing.duration),
        })


def public_time_out(request):
    """Legacy direct time-out URL — still works as fallback."""
    professor_id = request.GET.get('professor_id')
    if not professor_id:
        return render(request, 'record_tracker/timeout_result.html', {
            'status': 'error',
            'error_message': 'Invalid QR code.',
        })

    ip = get_client_ip(request)
    if is_rate_limited(f'qr_timeout_{ip}', max_requests=5, timeout=60):
        return render(request, 'record_tracker/timeout_result.html', {
            'status': 'error',
            'error_message': 'Too many attempts. Please wait 1 minute.',
        })

    if not is_on_school_network(request):
        return render(request, 'record_tracker/timeout_result.html', {
            'status': 'error',
            'error_message': 'You must be connected to the school WiFi.',
        })

    try:
        professor = Professor.objects.select_related('user').get(id=professor_id)
    except Professor.DoesNotExist:
        return render(request, 'record_tracker/timeout_result.html', {
            'status': 'error',
            'error_message': 'Professor not found.',
        })

    today = timezone.localdate()
    day   = AttendanceDay.objects.filter(professor=professor, date=today).first()

    if not day:
        return render(request, 'record_tracker/timeout_result.html', {
            'status': 'no_timein',
            'professor_name': professor.user.get_full_name(),
            'date': today.strftime('%B %d, %Y'),
        })

    if day.is_timed_out:
        return render(request, 'record_tracker/timeout_result.html', {
            'status': 'already',
            'professor_name': professor.user.get_full_name(),
            'time_out': timezone.localtime(day.time_out).strftime('%I:%M %p'),
            'date': today.strftime('%B %d, %Y'),
        })

    day.time_out = timezone.now()
    day.save()
    prof_name = professor.user.get_full_name()
    send_hr_notification(prof_name, "Timed Out (Left Campus)")
    send_hr_email(prof_name, "Timed Out (Left Campus)", request)

    return render(request, 'record_tracker/timeout_result.html', {
        'status': 'recorded',
        'professor_name': prof_name,
        'time_in':  timezone.localtime(day.time_in).strftime('%I:%M %p'),
        'time_out': timezone.localtime(day.time_out).strftime('%I:%M %p'),
        'date':     today.strftime('%B %d, %Y'),
        'duration': format_duration(day.duration),
    })


# ── Auth ──────────────────────────────────────────────────────────────────────

def custom_login(request):
    if request.user.is_authenticated:
        if Professor.objects.filter(user=request.user).exists():
            return redirect('professor_dashboard')
        return redirect('dashboard')
    error = None
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            if Professor.objects.filter(user=user).exists():
                return redirect('professor_dashboard')
            return redirect('dashboard')
        else:
            error = 'Invalid username or password.'
    return render(request, 'registration/login.html', {'error': error})


# ── HR Views ──────────────────────────────────────────────────────────────────

@login_required
def hr_dashboard(request):
    today = timezone.localdate()
    total_professors = Professor.objects.count()
    timed_in_today   = AttendanceDay.objects.filter(date=today).count()
    timed_out_today  = AttendanceDay.objects.filter(date=today, time_out__isnull=False).count()
    active_sessions  = AttendanceSession.objects.filter(day_record__date=today, end_time__isnull=True).count()
    recent_attendance = AttendanceDay.objects.filter(date=today).select_related('professor__user').order_by('-time_in')[:10]
    recent_sessions   = AttendanceSession.objects.filter(day_record__date=today).select_related('day_record__professor__user').order_by('-start_time')[:5]
    departments = Professor.objects.values_list('department', flat=True).distinct()
    department_stats = []
    for dept in departments:
        total   = Professor.objects.filter(department=dept).count()
        present = AttendanceDay.objects.filter(date=today, professor__department=dept).count()
        percentage = round((present / total) * 100) if total > 0 else 0
        department_stats.append({'department': dept, 'total': total, 'present': present, 'percentage': percentage})
    today_weekday = today.weekday()
    week_start = today - datetime.timedelta(days=today_weekday)
    weekly_timein, weekly_timeout = [], []
    for i in range(5):
        day = week_start + datetime.timedelta(days=i)
        weekly_timein.append(AttendanceDay.objects.filter(date=day).count())
        weekly_timeout.append(AttendanceDay.objects.filter(date=day, time_out__isnull=False).count())
    context = {
        'today': today,
        'total_professors': total_professors,
        'timed_in_today': timed_in_today,
        'timed_out_today': timed_out_today,
        'active_sessions': active_sessions,
        'recent_attendance': recent_attendance,
        'recent_sessions': recent_sessions,
        'department_stats': department_stats,
        'weekly_timein': weekly_timein,
        'weekly_timeout': weekly_timeout,
        'active_semester': Semester.get_active(),
    }
    return render(request, 'record_tracker/dashboard.html', context)


@login_required
def professors_view(request):
    today = timezone.localdate()
    professors = Professor.objects.select_related('user').all()
    context = {
        'professors': professors,
        'total_professors': professors.count(),
        'total_departments': Professor.objects.values('department').distinct().count(),
        'present_today': AttendanceDay.objects.filter(date=today).count(),
    }
    return render(request, 'record_tracker/professors.html', context)


@login_required
def attendance_view(request):
    date_str = request.GET.get('date', '')
    try:
        selected_date = datetime.date.fromisoformat(date_str)
    except ValueError:
        selected_date = timezone.localdate()
    today = timezone.localdate()
    total_professors = Professor.objects.count()
    timed_in_today   = AttendanceDay.objects.filter(date=selected_date).count()
    timed_out_today  = AttendanceDay.objects.filter(date=selected_date, time_out__isnull=False).count()
    absent_today     = total_professors - timed_in_today
    raw_records = AttendanceDay.objects.filter(date=selected_date).select_related('professor__user').order_by('-time_in')
    attendance_records = [{
        'record': record,
        'duration_display': format_duration(record.duration),
        'is_late': record.is_late,
        'minutes_late': record.minutes_late,
    } for record in raw_records]
    context = {
        'today': today,
        'selected_date': selected_date,
        'prev_date': (selected_date - datetime.timedelta(days=1)).isoformat(),
        'next_date': (selected_date + datetime.timedelta(days=1)).isoformat(),
        'is_future': selected_date >= today,
        'total_professors': total_professors,
        'timed_in_today': timed_in_today,
        'timed_out_today': timed_out_today,
        'absent_today': absent_today,
        'attendance_records': attendance_records,
    }
    return render(request, 'record_tracker/attendance.html', context)


@login_required
def sessions_view(request):
    today = timezone.localdate()
    sessions_qs = AttendanceSession.objects.filter(
        day_record__date=today
    ).select_related('day_record__professor__user', 'schedule').order_by('schedule__start_time', '-start_time')

    sessions = []
    for s in sessions_qs:
        sessions.append({
            'session':            s,
            'duration_display':   format_duration(s.duration),
            'is_ongoing':         s.is_ongoing,
            'professor_name':     s.day_record.professor.user.get_full_name(),
            'professor_initials': (s.day_record.professor.user.first_name[:1] + s.day_record.professor.user.last_name[:1]).upper(),
            'department':         s.day_record.professor.department,
            'subject_code':       s.subject_code,
            'classroom':          s.classroom,
            'start_time':         s.start_time,
            'end_time':           s.end_time,
            'is_late':            s.is_late,
            'minutes_late':       s.minutes_late,
            'scheduled_start':    s.scheduled_start,
            'scheduled_end':      s.schedule.end_time if s.schedule else None,
            'deadline':           s.deadline,
            'session_status':     s.session_status,       # ✅ NEW
        })

    total_sessions     = len(sessions)
    ongoing_sessions   = sum(1 for s in sessions if s['session_status'] == 'ongoing')
    finished_sessions  = sum(1 for s in sessions if s['session_status'] == 'finished')
    upcoming_sessions  = sum(1 for s in sessions if s['session_status'] == 'upcoming')
    late_sessions      = sum(1 for s in sessions if s['is_late'])

    context = {
        'today': today,
        'sessions': sessions,
        'total_sessions': total_sessions,
        'ongoing_sessions': ongoing_sessions,
        'completed_sessions': finished_sessions,
        'upcoming_sessions': upcoming_sessions,
        'late_sessions': late_sessions,
    }
    return render(request, 'record_tracker/sessions.html', context)


@login_required
def analytics_view(request):
    today = timezone.localdate()
    total_professors = Professor.objects.count()
    timed_in_today   = AttendanceDay.objects.filter(date=today).count()
    today_weekday    = today.weekday()
    week_start       = today - datetime.timedelta(days=today_weekday)
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    weekly_labels, weekly_timein, weekly_data = [], [], []
    for i in range(5):
        day   = week_start + datetime.timedelta(days=i)
        count = AttendanceDay.objects.filter(date=day).count()
        weekly_labels.append(days[i])
        weekly_timein.append(count)
        weekly_data.append({'label': days[i], 'count': count})
    departments = Professor.objects.values_list('department', flat=True).distinct()
    department_stats = []
    for dept in departments:
        total   = Professor.objects.filter(department=dept).count()
        present = AttendanceDay.objects.filter(date=today, professor__department=dept).count()
        department_stats.append({'department': dept, 'total': total, 'present': present})
    context = {
        'today': today,
        'total_professors': total_professors,
        'timed_in_today': timed_in_today,
        'weekly_labels': weekly_labels,
        'weekly_timein': weekly_timein,
        'weekly_data': weekly_data,
        'department_stats': department_stats,
    }
    return render(request, 'record_tracker/analytics.html', context)


@login_required
def add_professor(request):
    if request.method == 'POST':
        user = User.objects.create_user(
            username=request.POST.get('username'),
            password=request.POST.get('password'),
            first_name=request.POST.get('first_name'),
            last_name=request.POST.get('last_name'),
            email=request.POST.get('email'),
        )
        Professor.objects.create(
            user=user,
            employee_id=request.POST.get('employee_id'),
            phone_number=request.POST.get('phone_number'),
            department=request.POST.get('department'),
            expected_time_in=request.POST.get('expected_time_in') or None,
        )
        return redirect('professors')
    return redirect('professors')


@login_required
def edit_professor(request, professor_id):
    professor = get_object_or_404(Professor, id=professor_id)
    if request.method == 'POST':
        professor.user.first_name = request.POST.get('first_name', '').strip()
        professor.user.last_name  = request.POST.get('last_name', '').strip()
        professor.user.email      = request.POST.get('email', '').strip()
        new_password = request.POST.get('password', '').strip()
        if new_password:
            professor.user.set_password(new_password)
        professor.user.save()
        professor.employee_id      = request.POST.get('employee_id', '').strip()
        professor.phone_number     = request.POST.get('phone_number', '').strip()
        professor.department       = request.POST.get('department', '').strip()
        professor.expected_time_in = request.POST.get('expected_time_in') or None
        professor.save()
    return redirect('professors')


@login_required
def delete_professor(request, professor_id):
    professor = get_object_or_404(Professor, id=professor_id)
    if request.method == 'POST':
        professor.user.delete()
    return redirect('professors')


@login_required
def notifications_view(request):
    today = timezone.localdate()
    today_records = AttendanceDay.objects.filter(date=today).select_related('professor__user').order_by('-time_in')
    today_notifications = []
    for record in today_records:
        today_notifications.append({
            'type': 'timein',
            'title': f"{record.professor.user.get_full_name()} Timed In",
            'message': f"Arrived at campus. IP: {record.ip_address}" + (f" · Late by {record.minutes_late} min(s)" if record.is_late else ""),
            'time': timezone.localtime(record.time_in).strftime('%I:%M %p'),
        })
        if record.time_out:
            today_notifications.append({
                'type': 'timeout',
                'title': f"{record.professor.user.get_full_name()} Timed Out",
                'message': f"Left campus. Duration: {format_duration(record.duration)}",
                'time': timezone.localtime(record.time_out).strftime('%I:%M %p'),
            })
    earlier_records = AttendanceDay.objects.filter(date__lt=today).select_related('professor__user').order_by('-date', '-time_in')[:10]
    earlier_notifications = [{
        'type': 'timein',
        'title': f"{record.professor.user.get_full_name()} Timed In",
        'message': f"Arrived at campus on {record.date}" + (f" · Late by {record.minutes_late} min(s)" if record.is_late else ""),
        'time': timezone.localtime(record.time_in).strftime('%b %d · %I:%M %p'),
    } for record in earlier_records]
    context = {
        'today_notifications': today_notifications,
        'earlier_notifications': earlier_notifications,
    }
    return render(request, 'record_tracker/notifications.html', context)


@login_required
def reports_view(request):
    today = timezone.localdate()
    professors = Professor.objects.select_related('user').all()
    context = {
        'today': today,
        'week_start': today - datetime.timedelta(days=today.weekday()),
        'month_start': today.replace(day=1),
        'professors': professors,
    }
    return render(request, 'record_tracker/reports.html', context)


# ── Semester Views ────────────────────────────────────────────────────────────

@login_required
def semesters_view(request):
    semesters = Semester.objects.all().order_by('-start_date')
    active    = Semester.get_active()
    context   = {'semesters': semesters, 'active_semester': active}
    return render(request, 'record_tracker/semesters.html', context)


@login_required
def add_semester(request):
    if request.method == 'POST':
        Semester.objects.create(
            name=request.POST.get('name', '').strip(),
            start_date=request.POST.get('start_date'),
            end_date=request.POST.get('end_date'),
            is_active=request.POST.get('is_active') == 'on',
        )
    return redirect('semesters')


@login_required
def edit_semester(request, semester_id):
    semester = get_object_or_404(Semester, id=semester_id)
    if request.method == 'POST':
        semester.name       = request.POST.get('name', '').strip()
        semester.start_date = request.POST.get('start_date')
        semester.end_date   = request.POST.get('end_date')
        semester.is_active  = request.POST.get('is_active') == 'on'
        semester.save()
    return redirect('semesters')


@login_required
def delete_semester(request, semester_id):
    semester = get_object_or_404(Semester, id=semester_id)
    if request.method == 'POST':
        semester.delete()
    return redirect('semesters')


# ── Schedule Views ────────────────────────────────────────────────────────────

@login_required
def schedules_view(request):
    professors      = Professor.objects.select_related('user').all()
    semesters       = Semester.objects.all().order_by('-start_date')
    active_semester = Semester.get_active()
    professor_id    = request.GET.get('professor_id', '')
    semester_id     = request.GET.get('semester_id', '')
    schedules_qs    = ClassSchedule.objects.select_related('professor__user', 'semester').order_by('professor__user__last_name', 'start_time')
    if professor_id:
        schedules_qs = schedules_qs.filter(professor_id=professor_id)
    if semester_id:
        schedules_qs = schedules_qs.filter(semester_id=semester_id)
    elif active_semester:
        schedules_qs = schedules_qs.filter(semester=active_semester)
    grouped = {}
    for s in schedules_qs:
        prof_name = s.professor.user.get_full_name()
        if prof_name not in grouped:
            grouped[prof_name] = {'professor': s.professor, 'schedules': []}
        grouped[prof_name]['schedules'].append(s)
    context = {
        'grouped_schedules': grouped,
        'professors': professors,
        'semesters': semesters,
        'active_semester': active_semester,
        'selected_professor_id': professor_id,
        'selected_semester_id': semester_id or (str(active_semester.id) if active_semester else ''),
        'total_schedules': schedules_qs.count(),
        'schedule_days': ClassSchedule.DAY_CHOICES,
    }
    return render(request, 'record_tracker/schedules.html', context)


@login_required
def add_schedule(request):
    if request.method == 'POST':
        professor = get_object_or_404(Professor, id=request.POST.get('professor_id'))
        semester  = get_object_or_404(Semester, id=request.POST.get('semester_id'))
        days_list = request.POST.getlist('days')
        ClassSchedule.objects.create(
            professor=professor,
            semester=semester,
            subject_code=request.POST.get('subject_code', '').strip().upper(),
            subject_name=request.POST.get('subject_name', '').strip(),
            classroom=request.POST.get('classroom', '').strip(),
            days=','.join(days_list),
            start_time=request.POST.get('start_time'),
            end_time=request.POST.get('end_time'),
            grace_period=15,
        )
    return redirect('schedules')


@login_required
def edit_schedule(request, schedule_id):
    schedule = get_object_or_404(ClassSchedule, id=schedule_id)
    if request.method == 'POST':
        days_list = request.POST.getlist('days')
        schedule.subject_code = request.POST.get('subject_code', '').strip().upper()
        schedule.subject_name = request.POST.get('subject_name', '').strip()
        schedule.classroom    = request.POST.get('classroom', '').strip()
        schedule.days         = ','.join(days_list)
        schedule.start_time   = request.POST.get('start_time')
        schedule.end_time     = request.POST.get('end_time')
        schedule.save()
    return redirect('schedules')


@login_required
def delete_schedule(request, schedule_id):
    schedule = get_object_or_404(ClassSchedule, id=schedule_id)
    if request.method == 'POST':
        schedule.delete()
    return redirect('schedules')


# ── PDF ───────────────────────────────────────────────────────────────────────

def _build_pdf(response, title, subtitle, records_by_professor):
    page_width, page_height = A4
    p = canvas.Canvas(response, pagesize=A4)
    margin = 18 * mm

    def draw_page_header(canvas_obj, title_text, subtitle_text, generated_str):
        canvas_obj.setFillColorRGB(0.016, 0.173, 0.325)
        canvas_obj.rect(0, page_height - 28 * mm, page_width, 28 * mm, fill=1, stroke=0)
        canvas_obj.setFillColorRGB(0.216, 0.541, 0.867)
        canvas_obj.rect(0, page_height - 29.5 * mm, page_width, 1.5 * mm, fill=1, stroke=0)
        canvas_obj.setFillColorRGB(1, 1, 1)
        canvas_obj.setFont("Helvetica-Bold", 13)
        canvas_obj.drawString(margin, page_height - 11 * mm, title_text)
        canvas_obj.setFont("Helvetica", 8.5)
        canvas_obj.setFillColorRGB(0.71, 0.83, 0.95)
        canvas_obj.drawString(margin, page_height - 17 * mm, subtitle_text)
        canvas_obj.setFont("Helvetica", 7.5)
        canvas_obj.setFillColorRGB(0.71, 0.83, 0.95)
        ts_width = canvas_obj.stringWidth(generated_str, "Helvetica", 7.5)
        canvas_obj.drawString(page_width - margin - ts_width, page_height - 17 * mm, generated_str)

    def draw_table_header(canvas_obj, y):
        canvas_obj.setFillColorRGB(0.902, 0.945, 0.984)
        canvas_obj.rect(margin, y - 7 * mm, page_width - 2 * margin, 7 * mm, fill=1, stroke=0)
        canvas_obj.setFont("Helvetica-Bold", 7.5)
        canvas_obj.setFillColorRGB(0.098, 0.369, 0.647)
        cols = [margin + 2, 65 * mm, 95 * mm, 125 * mm, 150 * mm, 168 * mm, 185 * mm]
        hdrs = ["DATE", "TIME IN", "TIME OUT", "DURATION", "SESSIONS", "STATUS", "LATE"]
        for x, h in zip(cols, hdrs):
            canvas_obj.drawString(x, y - 5 * mm, h)
        return y - 9 * mm

    generated_str = f"Generated {timezone.now().strftime('%B %d, %Y  %I:%M %p')}  ·  SBC Batangas HR System"
    first_page = True

    for professor, records in records_by_professor:
        if not first_page:
            p.showPage()
        first_page = False
        draw_page_header(p, title, subtitle, generated_str)
        y = page_height - 36 * mm
        p.setFillColorRGB(0.024, 0.173, 0.325)
        p.setFont("Helvetica-Bold", 11)
        p.drawString(margin, y, professor.user.get_full_name())
        p.setFont("Helvetica", 8.5)
        p.setFillColorRGB(0.533, 0.529, 0.502)
        info_line = f"Employee ID: {professor.employee_id}   ·   Department: {professor.department}   ·   Phone: {professor.phone_number}"
        p.drawString(margin, y - 5.5 * mm, info_line)
        p.setStrokeColorRGB(0.71, 0.831, 0.945)
        p.setLineWidth(0.5)
        p.line(margin, y - 8 * mm, page_width - margin, y - 8 * mm)
        y -= 13 * mm
        y = draw_table_header(p, y)
        p.setFont("Helvetica", 8)
        record_list = list(records)
        if not record_list:
            p.setFillColorRGB(0.533, 0.529, 0.502)
            p.drawString(margin + 2, y - 5 * mm, "No attendance records found.")
            continue
        for i, record in enumerate(record_list):
            if y < 28 * mm:
                p.showPage()
                draw_page_header(p, title, subtitle, generated_str)
                y = page_height - 36 * mm
                y = draw_table_header(p, y)
                p.setFont("Helvetica", 8)
            if i % 2 == 0:
                p.setFillColorRGB(0.976, 0.984, 0.996)
                p.rect(margin, y - 6.5 * mm, page_width - 2 * margin, 6.5 * mm, fill=1, stroke=0)
            p.setFillColorRGB(0.024, 0.173, 0.325)
            time_in  = timezone.localtime(record.time_in).strftime('%I:%M %p') if record.time_in else '—'
            time_out = timezone.localtime(record.time_out).strftime('%I:%M %p') if record.time_out else '—'
            duration = format_duration(record.duration) or '—'
            late_t   = f"{record.minutes_late}m late" if record.is_late else '—'
            cols = [margin + 2, 65 * mm, 95 * mm, 125 * mm, 150 * mm, 168 * mm, 185 * mm]
            vals = [str(record.date), time_in, time_out, duration, str(record.sessions.count()), 'Timed Out' if record.is_timed_out else 'On Campus', late_t]
            for x, val in zip(cols, vals):
                p.drawString(x, y - 4.5 * mm, val)
            if record.is_timed_out:
                p.setFillColorRGB(0.114, 0.620, 0.459)
            elif record.is_late:
                p.setFillColorRGB(0.886, 0.294, 0.290)
            else:
                p.setFillColorRGB(0.216, 0.541, 0.867)
            p.circle(166 * mm, y - 3.5 * mm, 1.5 * mm, fill=1, stroke=0)
            y -= 7 * mm
        total = len(record_list)
        timed_out_count = sum(1 for r in record_list if r.is_timed_out)
        late_count = sum(1 for r in record_list if r.is_late)
        p.setFillColorRGB(0.902, 0.945, 0.984)
        p.rect(margin, y - 7 * mm, page_width - 2 * margin, 7 * mm, fill=1, stroke=0)
        p.setFont("Helvetica-Bold", 8)
        p.setFillColorRGB(0.016, 0.173, 0.325)
        p.drawString(margin + 2, y - 4.5 * mm, f"Total: {total}   ·   Timed Out: {timed_out_count}   ·   Late: {late_count}   ·   On Campus: {total - timed_out_count}")
        p.setFont("Helvetica", 7)
        p.setFillColorRGB(0.533, 0.529, 0.502)
        p.drawRightString(page_width - margin, 10 * mm, "SBC Batangas Part-Time Attendance System  ·  Confidential")
    p.save()


@login_required
def generate_pdf(request, professor_id):
    professor = get_object_or_404(Professor, id=professor_id)
    records   = AttendanceDay.objects.filter(professor=professor).select_related('professor__user').prefetch_related('sessions').order_by('-date')
    response  = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="attendance_{professor.employee_id}_full.pdf"'
    _build_pdf(response, "Attendance Report — Full History", f"{professor.user.get_full_name()}  ·  {professor.department}", [(professor, records)])
    return response


@login_required
def generate_pdf_all(request):
    professors = Professor.objects.select_related('user').all()
    response   = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="attendance_all_professors.pdf"'
    pairs = [(prof, AttendanceDay.objects.filter(professor=prof).select_related('professor__user').prefetch_related('sessions').order_by('-date')) for prof in professors]
    _build_pdf(response, "Attendance Report — All Professors", f"Complete history  ·  {timezone.now().strftime('%B %d, %Y')}", pairs)
    return response


@login_required
def generate_pdf_range(request):
    date_from_str = request.GET.get('date_from', str(timezone.localdate()))
    date_to_str   = request.GET.get('date_to', str(timezone.localdate()))
    professor_id  = request.GET.get('professor_id', '')
    report_type   = request.GET.get('type', 'custom').capitalize()
    try:
        date_from = datetime.date.fromisoformat(date_from_str)
        date_to   = datetime.date.fromisoformat(date_to_str)
    except ValueError:
        date_from = date_to = timezone.localdate()
    professors = Professor.objects.filter(id=professor_id).select_related('user') if professor_id else Professor.objects.select_related('user').all()
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="attendance_{report_type}_{date_from}_to_{date_to}.pdf"'
    pairs = [(prof, AttendanceDay.objects.filter(professor=prof, date__gte=date_from, date__lte=date_to).select_related('professor__user').prefetch_related('sessions').order_by('-date')) for prof in professors]
    _build_pdf(response, f"{report_type} Attendance Report", f"{date_from.strftime('%B %d, %Y')} — {date_to.strftime('%B %d, %Y')}", pairs)
    return response


@login_required
def qr_codes_view(request):
    professors = Professor.objects.select_related('user').all()
    return render(request, 'record_tracker/qr_codes.html', {'professors': professors})


@login_required
def generate_qr(request, professor_id):
    professor = get_object_or_404(Professor, id=professor_id)
    _generate_qr_for_professor(professor, request)
    if hasattr(request.user, 'professor'):
        return redirect('professor_qr')
    return redirect('qr_codes')


@login_required
def generate_all_qr(request):
    for professor in Professor.objects.all():
        _generate_qr_for_professor(professor, request)
    return redirect('qr_codes')


# ── Professor Portal ──────────────────────────────────────────────────────────

def professor_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not hasattr(request.user, 'professor'):
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped


@professor_required
def professor_dashboard(request):
    professor = Professor.objects.get(user=request.user)
    today     = timezone.localdate()
    today_record  = AttendanceDay.objects.filter(professor=professor, date=today).first()
    all_records   = AttendanceDay.objects.filter(professor=professor)
    total_days    = all_records.count()
    month_start   = today.replace(day=1)
    month_records = all_records.filter(date__gte=month_start)
    month_days    = month_records.count()
    late_count    = sum(1 for r in month_records if r.is_late)
    total_sessions = AttendanceSession.objects.filter(day_record__professor=professor).count()
    recent_qs = all_records.select_related('professor__user').order_by('-date')[:7]
    recent_attendance = [{'record': r, 'duration_display': format_duration(r.duration), 'is_late': r.is_late, 'minutes_late': r.minutes_late} for r in recent_qs]
    recent_sess_qs = AttendanceSession.objects.filter(day_record__professor=professor).select_related('day_record', 'schedule').order_by('-start_time')[:5]
    recent_sessions = [{'session': s, 'duration_display': format_duration(s.duration), 'is_ongoing': s.is_ongoing, 'is_late': s.is_late, 'minutes_late': s.minutes_late, 'session_status': s.session_status} for s in recent_sess_qs]
    active_semester = Semester.get_active()
    today_schedules = []
    if active_semester:
        for sched in ClassSchedule.objects.filter(professor=professor, semester=active_semester):
            if sched.is_scheduled_today(today):
                today_schedules.append(sched)
    context = {
        'professor': professor,
        'today': today,
        'today_record': today_record,
        'total_days': total_days,
        'month_days': month_days,
        'late_count': late_count,
        'total_sessions': total_sessions,
        'recent_attendance': recent_attendance,
        'recent_sessions': recent_sessions,
        'today_schedules': today_schedules,
        'active_semester': active_semester,
    }
    return render(request, 'professor/dashboard.html', context)


@professor_required
def professor_attendance(request):
    professor = Professor.objects.get(user=request.user)
    today     = timezone.localdate()
    month_start   = today.replace(day=1)
    all_records   = AttendanceDay.objects.filter(professor=professor).order_by('-date')
    month_records = all_records.filter(date__gte=month_start)
    total_days    = all_records.count()
    month_days    = month_records.count()
    late_count    = sum(1 for r in month_records if r.is_late)
    attendance_list = [{'record': r, 'duration_display': format_duration(r.duration), 'is_late': r.is_late, 'minutes_late': r.minutes_late} for r in all_records]
    context = {
        'professor': professor,
        'today': today,
        'total_days': total_days,
        'month_days': month_days,
        'late_count': late_count,
        'attendance_list': attendance_list,
    }
    return render(request, 'professor/attendance.html', context)


@professor_required
def professor_sessions(request):
    professor   = Professor.objects.get(user=request.user)
    sessions_qs = AttendanceSession.objects.filter(
        day_record__professor=professor
    ).select_related('day_record', 'schedule').order_by('-start_time')

    sessions = [{
        'session':         s,
        'duration_display': format_duration(s.duration),
        'is_ongoing':      s.is_ongoing,
        'is_late':         s.is_late,
        'minutes_late':    s.minutes_late,
        'scheduled_start': s.scheduled_start,
        'scheduled_end':   s.schedule.end_time if s.schedule else None,
        'deadline':        s.deadline,
        'session_status':  s.session_status,    # ✅ NEW
    } for s in sessions_qs]

    total_sessions = len(sessions)
    ongoing        = sum(1 for s in sessions if s['session_status'] == 'ongoing')
    finished       = sum(1 for s in sessions if s['session_status'] == 'finished')
    upcoming       = sum(1 for s in sessions if s['session_status'] == 'upcoming')
    late_sessions  = sum(1 for s in sessions if s['is_late'])

    active_semester = Semester.get_active()
    full_schedule   = []
    if active_semester:
        full_schedule = ClassSchedule.objects.filter(
            professor=professor, semester=active_semester
        ).order_by('start_time')

    context = {
        'professor': professor,
        'sessions': sessions,
        'total_sessions': total_sessions,
        'ongoing': ongoing,
        'finished': finished,
        'upcoming': upcoming,
        'late_sessions': late_sessions,
        'full_schedule': full_schedule,
        'active_semester': active_semester,
    }
    return render(request, 'professor/sessions.html', context)


@professor_required
def professor_qr(request):
    professor = Professor.objects.get(user=request.user)
    return render(request, 'professor/qr_code.html', {'professor': professor})


@professor_required
def professor_profile(request):
    professor        = Professor.objects.get(user=request.user)
    password_changed = False
    password_error   = None
    if request.method == 'POST':
        current = request.POST.get('current_password', '')
        new_pw  = request.POST.get('new_password', '')
        confirm = request.POST.get('confirm_password', '')
        if not request.user.check_password(current):
            password_error = 'Current password is incorrect.'
        elif len(new_pw) < 6:
            password_error = 'New password must be at least 6 characters.'
        elif new_pw != confirm:
            password_error = 'Passwords do not match.'
        else:
            request.user.set_password(new_pw)
            request.user.save()
            from django.contrib.auth import update_session_auth_hash
            update_session_auth_hash(request, request.user)
            password_changed = True
    context = {'professor': professor, 'password_changed': password_changed, 'password_error': password_error}
    return render(request, 'professor/profile.html', context)


# ── API ViewSets ──────────────────────────────────────────────────────────────

class ProfessorViewSet(viewsets.ModelViewSet):
    queryset         = Professor.objects.all()
    serializer_class = ProfessorSerializer
    permission_classes = [IsAuthenticated]


class AttendanceDayViewSet(viewsets.ModelViewSet):
    queryset         = AttendanceDay.objects.all()
    serializer_class = AttendanceDaySerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'])
    def quick_time_in(self, request):
        ip = get_client_ip(request)
        if is_rate_limited(f'timein_{ip}', max_requests=5, timeout=60):
            return Response({'error': 'Too many time-in attempts. Wait 1 minute.', 'retry_after': '60 seconds'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if not is_on_school_network(request):
            return Response({'error': 'You must be connected to the school WiFi to time in.'}, status=status.HTTP_403_FORBIDDEN)
        professor_id = request.data.get('professor_id')
        if not professor_id:
            return Response({'error': 'professor_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            professor  = Professor.objects.get(id=professor_id)
            ip_address = get_client_ip(request)
            day, created = AttendanceDay.objects.get_or_create(
                professor=professor,
                date=timezone.localdate(),
                defaults={'ip_address': ip_address}
            )
            if created:
                _auto_create_sessions(day)
                day.refresh_from_db()
                prof_name  = professor.user.get_full_name()
                late_info  = f" (Late by {day.minutes_late} min(s))" if day.is_late else ""
                sms_sent   = send_hr_notification(prof_name, f"Timed In (Arrived at Campus){late_info}")
                email_sent = send_hr_email(prof_name, f"Timed In (Arrived at Campus){late_info}", request)
                return Response({
                    'status': 'Time-in recorded.',
                    'is_late': day.is_late,
                    'minutes_late': day.minutes_late,
                    'sessions_created': day.sessions.count(),
                    'sms_status': 'HR notified' if sms_sent else 'SMS failed',
                    'email_status': 'HR emailed' if email_sent else 'Email failed',
                    'data': self.get_serializer(day).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({'status': 'Already timed in today.', 'data': self.get_serializer(day).data}, status=status.HTTP_200_OK)
        except Professor.DoesNotExist:
            return Response({'error': 'Professor not found.'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'])
    def quick_time_out(self, request):
        ip = get_client_ip(request)
        if is_rate_limited(f'timeout_{ip}', max_requests=5, timeout=60):
            return Response({'error': 'Too many time-out attempts. Wait 1 minute.', 'retry_after': '60 seconds'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if not is_on_school_network(request):
            return Response({'error': 'You must be connected to the school WiFi to time out.'}, status=status.HTTP_403_FORBIDDEN)
        professor_id = request.data.get('professor_id')
        if not professor_id:
            return Response({'error': 'professor_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            professor = Professor.objects.get(id=professor_id)
            day = AttendanceDay.objects.filter(professor=professor, date=timezone.localdate()).first()
            if not day:
                return Response({'error': 'No time-in record found for today.'}, status=status.HTTP_404_NOT_FOUND)
            if day.is_timed_out:
                return Response({'status': 'Already timed out today.', 'data': self.get_serializer(day).data}, status=status.HTTP_200_OK)
            day.time_out = timezone.now()
            day.save()
            prof_name  = professor.user.get_full_name()
            sms_sent   = send_hr_notification(prof_name, "Timed Out (Left Campus)")
            email_sent = send_hr_email(prof_name, "Timed Out (Left Campus)", request)
            return Response({'status': 'Time-out recorded.', 'sms_status': 'HR notified' if sms_sent else 'SMS failed', 'email_status': 'HR emailed' if email_sent else 'Email failed', 'data': self.get_serializer(day).data}, status=status.HTTP_200_OK)
        except Professor.DoesNotExist:
            return Response({'error': 'Professor not found.'}, status=status.HTTP_404_NOT_FOUND)


class AttendanceSessionViewSet(viewsets.ModelViewSet):
    queryset         = AttendanceSession.objects.all()
    serializer_class = AttendanceSessionSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'])
    def log_session(self, request):
        ip = get_client_ip(request)
        if is_rate_limited(f'logsession_{ip}', max_requests=10, timeout=60):
            return Response({'error': 'Too many session log attempts. Wait 1 minute.', 'retry_after': '60 seconds'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        day_record = serializer.validated_data['day_record']
        if not day_record.time_in:
            return Response({'error': 'Cannot log session without timing in first.'}, status=status.HTTP_403_FORBIDDEN)
        if day_record.is_timed_out:
            return Response({'error': 'Cannot log session after timing out.'}, status=status.HTTP_403_FORBIDDEN)
        session    = serializer.save()
        prof_name  = session.day_record.professor.user.get_full_name()
        event_message = f"Session Start: {session.subject_code} @ {session.classroom} at {timezone.now().strftime('%H:%M')}"
        sms_sent   = send_hr_notification(prof_name, event_message)
        email_sent = send_hr_email(prof_name, event_message, request)
        return Response({'status': 'Session logged.', 'sms_status': 'HR notified' if sms_sent else 'SMS failed', 'email_status': 'HR emailed' if email_sent else 'Email failed'}, status=status.HTTP_201_CREATED)