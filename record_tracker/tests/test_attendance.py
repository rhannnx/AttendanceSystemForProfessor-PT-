import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from record_tracker.models import AttendanceDay, AttendanceSession, AuditLog, Professor
from record_tracker.views import (
    _generate_qr_for_professor,
    _get_qr_serializer,
    generate_qr_token,
)


class AttendanceTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.hr = User.objects.create_user(username='hr', password='testpass')
        self.professor_user = User.objects.create_user(
            username='professor',
            password='testpass',
            first_name='Maria',
            last_name='Santos',
        )
        self.professor = Professor.objects.create(
            user=self.professor_user,
            employee_id='EMP001',
            department='Computer Science',
            phone_number='09170000001',
        )
        self.other_professor_user = User.objects.create_user(
            username='other-professor',
            password='testpass',
            first_name='Jose',
            last_name='Reyes',
        )
        self.other_professor = Professor.objects.create(
            user=self.other_professor_user,
            employee_id='EMP002',
            department='Mathematics',
            phone_number='09170000002',
        )

    def create_day(self, professor=None):
        return AttendanceDay.objects.create(
            professor=professor or self.professor,
            ip_address='127.0.0.1',
        )


class QRGenerationTests(AttendanceTestBase):
    def test_regenerating_qr_replaces_previous_file(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            request = RequestFactory().get('/qr-codes/generate/1/')
            request.user = self.hr

            _generate_qr_for_professor(self.professor, request)
            self.professor.refresh_from_db()
            first_name = self.professor.qr_code.name
            self.assertTrue((Path(media_root) / first_name).exists())

            _generate_qr_for_professor(self.professor, request)
            self.professor.refresh_from_db()
            second_name = self.professor.qr_code.name
            self.assertTrue((Path(media_root) / second_name).exists())

            self.assertEqual(first_name, second_name)
            qr_files = list((Path(media_root) / 'qr_codes').glob('*.png'))
            self.assertEqual(len(qr_files), 1)


class AttendanceEditTests(AttendanceTestBase):
    def test_hr_can_view_and_edit_attendance_with_audit(self):
        day = self.create_day()
        self.client.force_login(self.hr)

        response = self.client.get(reverse('edit_attendance_day', args=[day.id]))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse('edit_attendance_day', args=[day.id]),
            {
                'time_in': '09:00',
                'time_out': '10:30',
                'reason': 'Corrected time from manual logbook',
            },
        )

        expected_url = f"{reverse('attendance')}?date={day.date.isoformat()}"
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

        day.refresh_from_db()
        self.assertEqual(timezone.localtime(day.time_in).strftime('%H:%M'), '09:00')
        self.assertEqual(timezone.localtime(day.time_out).strftime('%H:%M'), '10:30')

        audit = AuditLog.objects.get(
            action='UPDATE',
            model_name='AttendanceDay',
            object_id=str(day.pk),
        )
        self.assertEqual(audit.user, self.hr)
        self.assertEqual(audit.reason, 'Corrected time from manual logbook')
        self.assertIn('T09:00:00', audit.new_values['time_in'])
        self.assertIn('T10:30:00', audit.new_values['time_out'])

    def test_edit_rejects_time_out_before_time_in(self):
        day = self.create_day()
        original_time_in = day.time_in
        self.client.force_login(self.hr)

        response = self.client.post(
            reverse('edit_attendance_day', args=[day.id]),
            {
                'time_in': '10:00',
                'time_out': '09:00',
                'reason': 'Invalid entry',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Time-out must be later than time-in.')
        self.assertFalse(AuditLog.objects.filter(action='UPDATE').exists())

        day.refresh_from_db()
        self.assertEqual(day.time_in, original_time_in)

    def test_edit_requires_a_reason(self):
        day = self.create_day()
        self.client.force_login(self.hr)

        response = self.client.post(
            reverse('edit_attendance_day', args=[day.id]),
            {
                'time_in': '09:00',
                'time_out': '10:00',
                'reason': '',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please provide a reason for this edit.')
        self.assertFalse(AuditLog.objects.filter(action='UPDATE').exists())

    def test_professor_cannot_access_attendance_edit_or_audit_pages(self):
        day = self.create_day()
        self.client.force_login(self.professor_user)

        edit_response = self.client.get(reverse('edit_attendance_day', args=[day.id]))
        audit_response = self.client.get(reverse('audit_logs'))

        self.assertRedirects(
            edit_response,
            reverse('professor_dashboard'),
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            audit_response,
            reverse('professor_dashboard'),
            fetch_redirect_response=False,
        )

    def test_hr_can_access_audit_page(self):
        self.client.force_login(self.hr)

        response = self.client.get(reverse('audit_logs'))

        self.assertEqual(response.status_code, 200)


@override_settings(
    ALLOWED_IP_RANGES=['127.0.0.1'],
    WIFI_BYPASS=False,
    TRUST_X_FORWARDED_FOR=False,
)
class PublicQrTests(AttendanceTestBase):
    def test_legacy_professor_id_url_is_rejected(self):
        response = self.client.get(
            reverse('public_time_in'),
            {'professor_id': self.professor.id},
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['status'], 'error')
        self.assertEqual(AttendanceDay.objects.count(), 0)

    @patch('record_tracker.views.send_hr_notification', return_value=True)
    @patch('record_tracker.views.send_hr_email', return_value=True)
    def test_valid_daily_token_times_in(self, mock_email, mock_sms):
        token = generate_qr_token(self.professor.id)

        response = self.client.get(
            reverse('public_time_in'),
            {'token': token},
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['status'], 'created')
        self.assertEqual(AttendanceDay.objects.count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(action='TIMEIN', model_name='AttendanceDay').exists()
        )
        mock_sms.assert_called_once()
        mock_email.assert_called_once()

    def test_expired_daily_token_is_rejected(self):
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        token = _get_qr_serializer().dumps({
            'pid': self.professor.id,
            'd': yesterday,
        })

        response = self.client.get(
            reverse('public_time_in'),
            {'token': token},
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['status'], 'error')
        self.assertIn('expired', response.context['error_message'].lower())
        self.assertEqual(AttendanceDay.objects.count(), 0)

    def test_legacy_timeout_url_is_rejected(self):
        response = self.client.get(
            reverse('public_time_out'),
            {'professor_id': self.professor.id},
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['status'], 'error')
        self.assertFalse(AttendanceDay.objects.exists())


@override_settings(
    ALLOWED_IP_RANGES=['127.0.0.1'],
    WIFI_BYPASS=False,
    TRUST_X_FORWARDED_FOR=False,
)
class ApiPermissionTests(AttendanceTestBase):
    def test_professor_cannot_create_attendance_through_generic_api(self):
        self.client.force_login(self.professor_user)

        response = self.client.post(
            reverse('attendanceday-list'),
            {'professor': self.professor.id},
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(AttendanceDay.objects.count(), 0)

    def test_professor_cannot_update_or_delete_own_attendance_through_api(self):
        day = self.create_day()
        self.client.force_login(self.professor_user)
        detail_url = reverse('attendanceday-detail', args=[day.id])

        update_response = self.client.patch(
            detail_url,
            data=json.dumps({'time_out': timezone.now().isoformat()}),
            content_type='application/json',
        )
        delete_response = self.client.delete(detail_url)

        self.assertEqual(update_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)
        self.assertTrue(AttendanceDay.objects.filter(pk=day.pk).exists())

    def test_professor_list_is_scoped_to_own_records(self):
        own_day = self.create_day()
        self.create_day(self.other_professor)
        self.client.force_login(self.professor_user)

        response = self.client.get(reverse('attendanceday-list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]['id'], own_day.id)

    def test_professor_cannot_quick_time_in_for_another_professor(self):
        self.client.force_login(self.professor_user)

        response = self.client.post(
            reverse('attendanceday-quick-time-in'),
            {'professor_id': self.other_professor.id},
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(AttendanceDay.objects.count(), 0)

    @patch('record_tracker.views.send_hr_notification', return_value=True)
    @patch('record_tracker.views.send_hr_email', return_value=True)
    def test_professor_can_quick_time_in_for_self_without_id(self, mock_email, mock_sms):
        self.client.force_login(self.professor_user)

        response = self.client.post(
            reverse('attendanceday-quick-time-in'),
            {},
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(AttendanceDay.objects.get().professor, self.professor)
        self.assertTrue(
            AuditLog.objects.filter(action='TIMEIN', model_name='AttendanceDay').exists()
        )
        mock_sms.assert_called_once()
        mock_email.assert_called_once()

    def test_professor_cannot_quick_time_out_for_another_professor(self):
        self.create_day(self.other_professor)
        self.client.force_login(self.professor_user)

        response = self.client.post(
            reverse('attendanceday-quick-time-out'),
            {'professor_id': self.other_professor.id},
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(AttendanceDay.objects.get().time_out)

    def test_professor_cannot_create_session_through_generic_api(self):
        day = self.create_day()
        self.client.force_login(self.professor_user)

        response = self.client.post(
            reverse('attendancesession-list'),
            {
                'day_record': day.id,
                'classroom': 'Room 101',
                'subject_code': 'CS101',
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_professor_cannot_log_session_for_another_professor(self):
        day = self.create_day(self.other_professor)
        self.client.force_login(self.professor_user)

        response = self.client.post(
            reverse('attendancesession-log-session'),
            {
                'day_record': day.id,
                'classroom': 'Room 101',
                'subject_code': 'CS101',
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    @patch('record_tracker.views.send_hr_notification', return_value=True)
    @patch('record_tracker.views.send_hr_email', return_value=True)
    def test_professor_can_log_session_for_self(self, mock_email, mock_sms):
        day = self.create_day()
        self.client.force_login(self.professor_user)

        response = self.client.post(
            reverse('attendancesession-log-session'),
            {
                'day_record': day.id,
                'classroom': 'Room 101',
                'subject_code': 'CS101',
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(AttendanceSession.objects.count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(action='CREATE', model_name='AttendanceSession').exists()
        )
        mock_sms.assert_called_once()
        mock_email.assert_called_once()
