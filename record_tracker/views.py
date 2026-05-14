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
from django.core.cache import cache                          # ✅ Rate limiting
from .models import Professor, AttendanceDay, AttendanceSession
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

def custom_logout(request):
    logout(request)
    return redirect('login')

# ── Helpers ───────────────────────────────────────────────────────────────────

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
    """Convert a timedelta into a readable string like '1h 30m' or '45m'."""
    if td is None:
        return None
    total_minutes = int(td.total_seconds() // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

# ✅ Rate limiting helper
def is_rate_limited(key, max_requests=5, timeout=60):
    """
    Returns True if the request should be blocked (rate limit exceeded).
    key        — unique string per action+IP (e.g. 'timein_127.0.0.1')
    max_requests — how many requests allowed in the window
    timeout    — window in seconds (default 60s = 1 minute)
    """
    count = cache.get(key, 0)
    if count >= max_requests:
        return True
    cache.set(key, count + 1, timeout=timeout)
    return False


def _generate_qr_for_professor(professor, request):
    """
    Generates a QR code image for a professor and saves it to their profile.
    The QR encodes their time-in API URL — scanning it triggers attendance.
    """
    scan_url = request.build_absolute_uri(
        f'/api/attendance-days/quick_time_in/?professor_id={professor.id}'
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


# ── HTML Views ────────────────────────────────────────────────────────────────

@login_required
def hr_dashboard(request):
    today = timezone.localdate()
    total_professors = Professor.objects.count()
    timed_in_today = AttendanceDay.objects.filter(date=today).count()
    timed_out_today = AttendanceDay.objects.filter(
        date=today, time_out__isnull=False
    ).count()
    active_sessions = AttendanceSession.objects.filter(
        day_record__date=today,
        end_time__isnull=True
    ).count()
    recent_attendance = AttendanceDay.objects.filter(
        date=today
    ).select_related('professor__user').order_by('-time_in')[:10]
    recent_sessions = AttendanceSession.objects.filter(
        day_record__date=today
    ).select_related('day_record__professor__user').order_by('-start_time')[:5]
    departments = Professor.objects.values_list('department', flat=True).distinct()
    department_stats = []
    for dept in departments:
        total = Professor.objects.filter(department=dept).count()
        present = AttendanceDay.objects.filter(
            date=today, professor__department=dept
        ).count()
        percentage = round((present / total) * 100) if total > 0 else 0
        department_stats.append({
            'department': dept,
            'total': total,
            'present': present,
            'percentage': percentage,
        })
    today_weekday = today.weekday()
    week_start = today - datetime.timedelta(days=today_weekday)
    weekly_timein = []
    weekly_timeout = []
    for i in range(5):
        day = week_start + datetime.timedelta(days=i)
        weekly_timein.append(AttendanceDay.objects.filter(date=day).count())
        weekly_timeout.append(AttendanceDay.objects.filter(
            date=day, time_out__isnull=False
        ).count())
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
    }
    return render(request, 'record_tracker/dashboard.html', context)


@login_required
def professors_view(request):
    today = timezone.localdate()
    professors = Professor.objects.select_related('user').all()
    total_professors = professors.count()
    total_departments = Professor.objects.values('department').distinct().count()
    present_today = AttendanceDay.objects.filter(date=today).count()
    context = {
        'professors': professors,
        'total_professors': total_professors,
        'total_departments': total_departments,
        'present_today': present_today,
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
    timed_in_today  = AttendanceDay.objects.filter(date=selected_date).count()
    timed_out_today = AttendanceDay.objects.filter(
        date=selected_date, time_out__isnull=False
    ).count()
    absent_today = total_professors - timed_in_today

    raw_records = AttendanceDay.objects.filter(
        date=selected_date
    ).select_related('professor__user').order_by('-time_in')

    attendance_records = []
    for record in raw_records:
        attendance_records.append({
            'record': record,
            'duration_display': format_duration(record.duration),
            'is_late': record.is_late,
            'minutes_late': record.minutes_late,
        })

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
def analytics_view(request):
    today = timezone.localdate()
    total_professors = Professor.objects.count()
    timed_in_today = AttendanceDay.objects.filter(date=today).count()

    today_weekday = today.weekday()
    week_start = today - datetime.timedelta(days=today_weekday)
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    weekly_labels = []
    weekly_timein = []
    weekly_data = []
    for i in range(5):
        day = week_start + datetime.timedelta(days=i)
        count = AttendanceDay.objects.filter(date=day).count()
        weekly_labels.append(days[i])
        weekly_timein.append(count)
        weekly_data.append({'label': days[i], 'count': count})

    departments = Professor.objects.values_list('department', flat=True).distinct()
    department_stats = []
    for dept in departments:
        total = Professor.objects.filter(department=dept).count()
        present = AttendanceDay.objects.filter(
            date=today, professor__department=dept
        ).count()
        department_stats.append({
            'department': dept,
            'total': total,
            'present': present,
        })

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
        first_name       = request.POST.get('first_name')
        last_name        = request.POST.get('last_name')
        email            = request.POST.get('email')
        username         = request.POST.get('username')
        password         = request.POST.get('password')
        employee_id      = request.POST.get('employee_id')
        phone_number     = request.POST.get('phone_number')
        department       = request.POST.get('department')
        expected_time_in = request.POST.get('expected_time_in') or None
        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=first_name,
            last_name=last_name,
            email=email
        )
        Professor.objects.create(
            user=user,
            employee_id=employee_id,
            phone_number=phone_number,
            department=department,
            expected_time_in=expected_time_in,
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
def sessions_view(request):
    today = timezone.localdate()
    sessions_qs = AttendanceSession.objects.filter(
        day_record__date=today
    ).select_related('day_record__professor__user').order_by('-start_time')

    sessions = []
    for s in sessions_qs:
        sessions.append({
            'session': s,
            'duration_display': format_duration(s.duration),
            'is_ongoing': s.is_ongoing,
            'professor_name': s.day_record.professor.user.get_full_name(),
            'professor_initials': (
                s.day_record.professor.user.first_name[:1] +
                s.day_record.professor.user.last_name[:1]
            ).upper(),
            'department': s.day_record.professor.department,
            'subject_code': s.subject_code,
            'classroom': s.classroom,
            'start_time': s.start_time,
            'end_time': s.end_time,
        })

    total_sessions     = len(sessions)
    ongoing_sessions   = sum(1 for s in sessions if s['is_ongoing'])
    completed_sessions = total_sessions - ongoing_sessions

    context = {
        'today': today,
        'sessions': sessions,
        'total_sessions': total_sessions,
        'ongoing_sessions': ongoing_sessions,
        'completed_sessions': completed_sessions,
    }
    return render(request, 'record_tracker/sessions.html', context)


@login_required
def notifications_view(request):
    today = timezone.localdate()
    today_records = AttendanceDay.objects.filter(
        date=today
    ).select_related('professor__user').order_by('-time_in')

    today_notifications = []
    for record in today_records:
        today_notifications.append({
            'type': 'timein',
            'title': f"{record.professor.user.get_full_name()} Timed In",
            'message': f"Arrived at campus. IP: {record.ip_address}"
                       + (f" · Late by {record.minutes_late} min(s)" if record.is_late else ""),
            'time': timezone.localtime(record.time_in).strftime('%I:%M %p'),
        })
        if record.time_out:
            today_notifications.append({
                'type': 'timeout',
                'title': f"{record.professor.user.get_full_name()} Timed Out",
                'message': f"Left campus. Duration: {format_duration(record.duration)}",
                'time': timezone.localtime(record.time_out).strftime('%I:%M %p'),
            })

    earlier_records = AttendanceDay.objects.filter(
        date__lt=today
    ).select_related('professor__user').order_by('-date', '-time_in')[:10]

    earlier_notifications = []
    for record in earlier_records:
        earlier_notifications.append({
            'type': 'timein',
            'title': f"{record.professor.user.get_full_name()} Timed In",
            'message': f"Arrived at campus on {record.date}"
                       + (f" · Was late by {record.minutes_late} min(s)" if record.is_late else ""),
            'time': timezone.localtime(record.time_in).strftime('%b %d · %I:%M %p'),
        })

    context = {
        'today_notifications': today_notifications,
        'earlier_notifications': earlier_notifications,
    }
    return render(request, 'record_tracker/notifications.html', context)


@login_required
def reports_view(request):
    today      = timezone.localdate()
    week_start = today - datetime.timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    professors = Professor.objects.select_related('user').all()
    context = {
        'today': today,
        'week_start': week_start,
        'month_start': month_start,
        'professors': professors,
    }
    return render(request, 'record_tracker/reports.html', context)


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
        if professor.expected_time_in:
            info_line += f"   ·   Expected In: {professor.expected_time_in.strftime('%I:%M %p')}"
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
            p.drawString(margin + 2, y - 5 * mm, "No attendance records found for this period.")
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
            sessions = str(record.sessions.count())
            status_t = 'Timed Out' if record.is_timed_out else 'On Campus'
            late_t   = f"{record.minutes_late}m late" if record.is_late else '—'

            cols = [margin + 2, 65 * mm, 95 * mm, 125 * mm, 150 * mm, 168 * mm, 185 * mm]
            vals = [str(record.date), time_in, time_out, duration, sessions, status_t, late_t]
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

        total           = len(record_list)
        timed_out_count = sum(1 for r in record_list if r.is_timed_out)
        late_count      = sum(1 for r in record_list if r.is_late)
        p.setFillColorRGB(0.902, 0.945, 0.984)
        p.rect(margin, y - 7 * mm, page_width - 2 * margin, 7 * mm, fill=1, stroke=0)
        p.setFont("Helvetica-Bold", 8)
        p.setFillColorRGB(0.016, 0.173, 0.325)
        p.drawString(margin + 2, y - 4.5 * mm,
                     f"Total: {total} records   ·   Timed Out: {timed_out_count}   ·   Late: {late_count}   ·   On Campus: {total - timed_out_count}")
        p.setFont("Helvetica", 7)
        p.setFillColorRGB(0.533, 0.529, 0.502)
        p.drawRightString(page_width - margin, 10 * mm,
                          "SBC Batangas Part-Time Attendance System  ·  Confidential")

    p.save()


@login_required
def generate_pdf(request, professor_id):
    professor = get_object_or_404(Professor, id=professor_id)
    records = AttendanceDay.objects.filter(
        professor=professor
    ).select_related('professor__user').prefetch_related('sessions').order_by('-date')
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="attendance_{professor.employee_id}_full.pdf"'
    )
    _build_pdf(
        response,
        title="Attendance Report — Full History",
        subtitle=f"{professor.user.get_full_name()}  ·  {professor.department}  ·  ID: {professor.employee_id}",
        records_by_professor=[(professor, records)],
    )
    return response


@login_required
def generate_pdf_all(request):
    professors = Professor.objects.select_related('user').all()
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="attendance_all_professors.pdf"'
    pairs = []
    for prof in professors:
        records = AttendanceDay.objects.filter(
            professor=prof
        ).select_related('professor__user').prefetch_related('sessions').order_by('-date')
        pairs.append((prof, records))
    _build_pdf(
        response,
        title="Attendance Report — All Professors",
        subtitle=f"Complete attendance history  ·  Generated {timezone.now().strftime('%B %d, %Y')}",
        records_by_professor=pairs,
    )
    return response


@login_required
def generate_pdf_range(request):
    date_from_str = request.GET.get('date_from', str(timezone.localdate()))
    date_to_str   = request.GET.get('date_to',   str(timezone.localdate()))
    professor_id  = request.GET.get('professor_id', '')
    report_type   = request.GET.get('type', 'custom').capitalize()

    try:
        date_from = datetime.date.fromisoformat(date_from_str)
        date_to   = datetime.date.fromisoformat(date_to_str)
    except ValueError:
        date_from = date_to = timezone.localdate()

    if professor_id:
        professors = Professor.objects.filter(id=professor_id).select_related('user')
    else:
        professors = Professor.objects.select_related('user').all()

    response = HttpResponse(content_type='application/pdf')
    filename  = f"attendance_{report_type}_{date_from}_to_{date_to}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    pairs = []
    for prof in professors:
        records = AttendanceDay.objects.filter(
            professor=prof,
            date__gte=date_from,
            date__lte=date_to,
        ).select_related('professor__user').prefetch_related('sessions').order_by('-date')
        pairs.append((prof, records))

    _build_pdf(
        response,
        title=f"{report_type} Attendance Report",
        subtitle=f"{date_from.strftime('%B %d, %Y')} — {date_to.strftime('%B %d, %Y')}",
        records_by_professor=pairs,
    )
    return response


@login_required
def qr_codes_view(request):
    professors = Professor.objects.select_related('user').all()
    context = {'professors': professors}
    return render(request, 'record_tracker/qr_codes.html', context)


@login_required
def generate_qr(request, professor_id):
    professor = get_object_or_404(Professor, id=professor_id)
    _generate_qr_for_professor(professor, request)
    return redirect('qr_codes')


@login_required
def generate_all_qr(request):
    for professor in Professor.objects.all():
        _generate_qr_for_professor(professor, request)
    return redirect('qr_codes')

def custom_login(request):
    from django.contrib.auth import authenticate, login

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


# ── Professor Portal Views ────────────────────────────────────────────────────

def professor_required(view_func):
    """
    Decorator: user must be logged in AND have a Professor profile.
    HR users who accidentally hit /professor/ get redirected to HR dashboard.
    """
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

    # Today's record
    today_record = AttendanceDay.objects.filter(
        professor=professor, date=today
    ).first()

    # All-time stats
    all_records = AttendanceDay.objects.filter(professor=professor)
    total_days  = all_records.count()

    # This month
    month_start = today.replace(day=1)
    month_records = all_records.filter(date__gte=month_start)
    month_days  = month_records.count()
    late_count  = sum(1 for r in month_records if r.is_late)

    # Total sessions
    total_sessions = AttendanceSession.objects.filter(
        day_record__professor=professor
    ).count()

    # Recent attendance (last 7)
    recent_qs = all_records.select_related('professor__user').order_by('-date')[:7]
    recent_attendance = [
        {
            'record': r,
            'duration_display': format_duration(r.duration),
            'is_late': r.is_late,
            'minutes_late': r.minutes_late,
        }
        for r in recent_qs
    ]

    # Recent sessions (last 5)
    recent_sess_qs = AttendanceSession.objects.filter(
        day_record__professor=professor
    ).select_related('day_record').order_by('-start_time')[:5]
    recent_sessions = [
        {
            'session': s,
            'duration_display': format_duration(s.duration),
            'is_ongoing': s.is_ongoing,
        }
        for s in recent_sess_qs
    ]

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
    }
    return render(request, 'professor/dashboard.html', context)

@professor_required
def professor_attendance(request):
    professor = Professor.objects.get(user=request.user)
    today       = timezone.localdate()
    month_start = today.replace(day=1)

    all_records   = AttendanceDay.objects.filter(professor=professor).order_by('-date')
    month_records = all_records.filter(date__gte=month_start)

    total_days  = all_records.count()
    month_days  = month_records.count()
    late_count  = sum(1 for r in month_records if r.is_late)

    attendance_list = [
        {
            'record': r,
            'duration_display': format_duration(r.duration),
            'is_late': r.is_late,
            'minutes_late': r.minutes_late,
        }
        for r in all_records
    ]

    context = {
        'professor': professor,
        'today': today,
        'total_days': total_days,
        'month_days': month_days,
        'late_count': late_count,
        'attendance_list': attendance_list,
    }
    return render(request, 'professor/dashboard.html', context)

@professor_required
def professor_sessions(request):
    professor = Professor.objects.get(user=request.user)

    sessions_qs = AttendanceSession.objects.filter(
        day_record__professor=professor
    ).select_related('day_record').order_by('-start_time')

    sessions = [
        {
            'session': s,
            'duration_display': format_duration(s.duration),
            'is_ongoing': s.is_ongoing,
        }
        for s in sessions_qs
    ]

    total_sessions = len(sessions)
    ongoing        = sum(1 for s in sessions if s['is_ongoing'])

    context = {
        'professor': professor,
        'sessions': sessions,
        'total_sessions': total_sessions,
        'ongoing': ongoing,
    }
    return render(request, 'professor/dashboard.html', context)


@professor_required
def professor_qr(request):
    professor = Professor.objects.get(user=request.user)
    context   = {'professor': professor}
    return render(request, 'professor/professor_qr_code.html', context)


@professor_required
def professor_profile(request):
    professor = Professor.objects.get(user=request.user)
    password_changed = False
    password_error   = None

    if request.method == 'POST':
        current  = request.POST.get('current_password', '')
        new_pw   = request.POST.get('new_password', '')
        confirm  = request.POST.get('confirm_password', '')

        if not request.user.check_password(current):
            password_error = 'Current password is incorrect.'
        elif len(new_pw) < 6:
            password_error = 'New password must be at least 6 characters.'
        elif new_pw != confirm:
            password_error = 'Passwords do not match.'
        else:
            request.user.set_password(new_pw)
            request.user.save()
            # Re-authenticate so the session stays valid
            from django.contrib.auth import update_session_auth_hash
            update_session_auth_hash(request, request.user)
            password_changed = True

    context = {
        'professor': professor,
        'password_changed': password_changed,
        'password_error': password_error,
    }
    return render(request, 'professor/dashboard.html', context)

# ── API ViewSets ──────────────────────────────────────────────────────────────

class ProfessorViewSet(viewsets.ModelViewSet):
    queryset = Professor.objects.all()
    serializer_class = ProfessorSerializer
    permission_classes = [IsAuthenticated]


class AttendanceDayViewSet(viewsets.ModelViewSet):
    queryset = AttendanceDay.objects.all()
    serializer_class = AttendanceDaySerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'])
    def quick_time_in(self, request):
        # ✅ RATE LIMITING — max 5 requests per minute per IP
        ip = get_client_ip(request)
        if is_rate_limited(f'timein_{ip}', max_requests=5, timeout=60):
            return Response(
                {
                    'error': 'Too many time-in attempts. Please wait 1 minute before trying again.',
                    'retry_after': '60 seconds',
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        if not is_on_school_network(request):
            return Response(
                {'error': 'You must be connected to the school WiFi to time in.'},
                status=status.HTTP_403_FORBIDDEN
            )
        professor_id = request.data.get('professor_id')
        if not professor_id:
            return Response(
                {'error': 'professor_id is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            professor = Professor.objects.get(id=professor_id)
            ip_address = get_client_ip(request)
            day, created = AttendanceDay.objects.get_or_create(
                professor=professor,
                date=timezone.localdate(),
                defaults={'ip_address': ip_address}
            )
            if created:
                prof_name = professor.user.get_full_name()
                late_info = f" (Late by {day.minutes_late} min(s))" if day.is_late else ""
                sms_sent   = send_hr_notification(prof_name, f"Timed In (Arrived at Campus){late_info}")
                email_sent = send_hr_email(prof_name, f"Timed In (Arrived at Campus){late_info}", request)
                serializer = self.get_serializer(day)
                return Response({
                    'status': 'Time-in recorded.',
                    'is_late': day.is_late,
                    'minutes_late': day.minutes_late,
                    'sms_status': 'HR notified' if sms_sent else 'SMS failed',
                    'email_status': 'HR emailed' if email_sent else 'Email failed',
                    'data': serializer.data
                }, status=status.HTTP_201_CREATED)
            else:
                serializer = self.get_serializer(day)
                return Response({
                    'status': 'Already timed in today.',
                    'data': serializer.data
                }, status=status.HTTP_200_OK)
        except Professor.DoesNotExist:
            return Response(
                {'error': 'Professor not found.'},
                status=status.HTTP_404_NOT_FOUND
            )

    @action(detail=False, methods=['post'])
    def quick_time_out(self, request):
        # ✅ RATE LIMITING — max 5 requests per minute per IP
        ip = get_client_ip(request)
        if is_rate_limited(f'timeout_{ip}', max_requests=5, timeout=60):
            return Response(
                {
                    'error': 'Too many time-out attempts. Please wait 1 minute before trying again.',
                    'retry_after': '60 seconds',
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        if not is_on_school_network(request):
            return Response(
                {'error': 'You must be connected to the school WiFi to time out.'},
                status=status.HTTP_403_FORBIDDEN
            )
        professor_id = request.data.get('professor_id')
        if not professor_id:
            return Response(
                {'error': 'professor_id is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            professor = Professor.objects.get(id=professor_id)
            day = AttendanceDay.objects.filter(
                professor=professor,
                date=timezone.localdate()
            ).first()
            if not day:
                return Response(
                    {'error': 'No time-in record found for today.'},
                    status=status.HTTP_404_NOT_FOUND
                )
            if day.is_timed_out:
                serializer = self.get_serializer(day)
                return Response({
                    'status': 'Already timed out today.',
                    'data': serializer.data
                }, status=status.HTTP_200_OK)
            day.time_out = timezone.now()
            day.save()
            prof_name  = professor.user.get_full_name()
            sms_sent   = send_hr_notification(prof_name, "Timed Out (Left Campus)")
            email_sent = send_hr_email(prof_name, "Timed Out (Left Campus)", request)
            serializer = self.get_serializer(day)
            return Response({
                'status': 'Time-out recorded.',
                'sms_status': 'HR notified' if sms_sent else 'SMS failed',
                'email_status': 'HR emailed' if email_sent else 'Email failed',
                'data': serializer.data
            }, status=status.HTTP_200_OK)
        except Professor.DoesNotExist:
            return Response(
                {'error': 'Professor not found.'},
                status=status.HTTP_404_NOT_FOUND
            )


class AttendanceSessionViewSet(viewsets.ModelViewSet):
    queryset = AttendanceSession.objects.all()
    serializer_class = AttendanceSessionSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'])
    def log_session(self, request):
        # ✅ RATE LIMITING — max 10 session logs per minute per IP
        ip = get_client_ip(request)
        if is_rate_limited(f'logsession_{ip}', max_requests=10, timeout=60):
            return Response(
                {
                    'error': 'Too many session log attempts. Please wait 1 minute before trying again.',
                    'retry_after': '60 seconds',
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )
        day_record = serializer.validated_data['day_record']
        if not day_record.time_in:
            return Response(
                {'error': 'Cannot log session without timing in first.'},
                status=status.HTTP_403_FORBIDDEN
            )
        if day_record.is_timed_out:
            return Response(
                {'error': 'Cannot log session after timing out.'},
                status=status.HTTP_403_FORBIDDEN
            )
        session    = serializer.save()
        prof_name  = session.day_record.professor.user.get_full_name()
        event_message = (
            f"Session Start: {session.subject_code} "
            f"@ {session.classroom} at "
            f"{timezone.now().strftime('%H:%M')}"
        )
        sms_sent   = send_hr_notification(prof_name, event_message)
        email_sent = send_hr_email(prof_name, event_message, request)
        return Response({
            'status': 'Session logged.',
            'sms_status': 'HR notified' if sms_sent else 'SMS failed',
            'email_status': 'HR emailed' if email_sent else 'Email failed',
        }, status=status.HTTP_201_CREATED)