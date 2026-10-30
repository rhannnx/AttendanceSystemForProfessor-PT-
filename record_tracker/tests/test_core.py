from datetime import date

from django.test import TestCase

from record_tracker.models import Semester


class CoreLogicTests(TestCase):
    def test_semester_single_active(self):
        first = Semester.objects.create(
            name='S1',
            start_date=date(2026, 1, 1),
            end_date=date(2026, 5, 1),
            is_active=True,
        )
        second = Semester.objects.create(
            name='S2',
            start_date=date(2026, 6, 1),
            end_date=date(2026, 10, 1),
            is_active=True,
        )

        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
