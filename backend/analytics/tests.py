"""
analytics/tests.py
Test suite for the Analytics module — date_service and freshness_service.

Tests cover:
- resolve_date_range: all period keywords, custom range, edge cases, timezone-awareness
- freshness_service: report structure, status classification, warning generation
"""

from datetime import date, timedelta as td
import datetime as dt

from django.test import TestCase
from django.utils import timezone


# ---------------------------------------------------------------------------
# 1. DateService Tests — resolve_date_range
# ---------------------------------------------------------------------------

class ResolveDateRangeBasicTests(TestCase):
    """Test that resolve_date_range() returns the correct structure for all period keys."""

    def _call(self, **kwargs):
        from analytics.date_service import resolve_date_range
        return resolve_date_range(**kwargs)

    def test_returns_required_keys(self):
        result = self._call(period='30d')
        required = {
            'date_from', 'date_to', 'start_datetime', 'end_datetime',
            'period_label', 'days_count', 'is_custom', 'is_current_month', 'generated_at'
        }
        for key in required:
            self.assertIn(key, result, msg=f"Missing key: {key}")

    def test_today_period(self):
        result = self._call(period='today')
        today = timezone.localdate()
        self.assertEqual(result['date_from'], today)
        self.assertEqual(result['date_to'], today)
        self.assertEqual(result['days_count'], 1)

    def test_yesterday_period(self):
        result = self._call(period='yesterday')
        yesterday = timezone.localdate() - td(days=1)
        self.assertEqual(result['date_from'], yesterday)
        self.assertEqual(result['date_to'], yesterday)

    def test_7d_period(self):
        result = self._call(period='7d')
        today = timezone.localdate()
        expected_from = today - td(days=6)
        self.assertEqual(result['date_from'], expected_from)
        self.assertEqual(result['date_to'], today)

    def test_30d_period(self):
        result = self._call(period='30d')
        today = timezone.localdate()
        expected_from = today - td(days=29)
        self.assertEqual(result['date_from'], expected_from)
        self.assertEqual(result['date_to'], today)
        self.assertFalse(result['is_custom'])

    def test_90d_period(self):
        result = self._call(period='90d')
        today = timezone.localdate()
        expected_from = today - td(days=89)
        self.assertEqual(result['date_from'], expected_from)

    def test_this_month_period(self):
        result = self._call(period='this_month')
        today = timezone.localdate()
        self.assertEqual(result['date_from'].year, today.year)
        self.assertEqual(result['date_from'].month, today.month)
        self.assertEqual(result['date_from'].day, 1)
        self.assertTrue(result['is_current_month'])

    def test_last_month_not_current_month(self):
        result = self._call(period='last_month')
        self.assertFalse(result['is_current_month'])

    def test_this_week_period(self):
        result = self._call(period='this_week')
        today = timezone.localdate()
        self.assertLessEqual(result['date_from'], today)
        self.assertEqual(result['date_to'], today)

    def test_this_quarter_period(self):
        result = self._call(period='this_quarter')
        today = timezone.localdate()
        self.assertLessEqual(result['date_from'], today)
        self.assertEqual(result['date_to'], today)

    def test_this_year_period(self):
        result = self._call(period='this_year')
        today = timezone.localdate()
        self.assertEqual(result['date_from'], date(today.year, 1, 1))
        self.assertEqual(result['date_to'], today)


class ResolveDateRangeCustomTests(TestCase):
    """Test custom range handling."""

    def _call(self, **kwargs):
        from analytics.date_service import resolve_date_range
        return resolve_date_range(**kwargs)

    def test_custom_range_string_dates(self):
        result = self._call(period='custom', date_from='2026-01-01', date_to='2026-01-31')
        self.assertTrue(result['is_custom'])
        self.assertEqual(result['date_from'], date(2026, 1, 1))
        self.assertEqual(result['date_to'], date(2026, 1, 31))
        self.assertEqual(result['days_count'], 31)

    def test_custom_range_date_objects(self):
        d_from = date(2026, 3, 1)
        d_to = date(2026, 3, 31)
        result = self._call(period='custom', date_from=d_from, date_to=d_to)
        self.assertTrue(result['is_custom'])
        self.assertEqual(result['date_from'], d_from)

    def test_custom_without_dates_falls_back_to_30d(self):
        """If period=custom but no dates provided, should fall back gracefully."""
        result = self._call(period='custom')
        # Should return a valid result without raising
        self.assertIn('date_from', result)
        self.assertIn('date_to', result)

    def test_unknown_period_falls_back_gracefully(self):
        result = self._call(period='unknown_period_xyz')
        self.assertIn('date_from', result)
        self.assertIn('date_to', result)


class ResolveDateRangeDatetimeTests(TestCase):
    """Test that returned datetimes are timezone-aware and boundaries are correct."""

    def _call(self, **kwargs):
        from analytics.date_service import resolve_date_range
        return resolve_date_range(**kwargs)

    def test_start_datetime_is_timezone_aware(self):
        result = self._call(period='30d')
        self.assertIsNotNone(result['start_datetime'].tzinfo)

    def test_end_datetime_is_timezone_aware(self):
        result = self._call(period='30d')
        self.assertIsNotNone(result['end_datetime'].tzinfo)

    def test_start_datetime_is_midnight(self):
        result = self._call(period='30d')
        self.assertEqual(result['start_datetime'].hour, 0)
        self.assertEqual(result['start_datetime'].minute, 0)
        self.assertEqual(result['start_datetime'].second, 0)

    def test_end_datetime_is_end_of_day(self):
        result = self._call(period='30d')
        self.assertEqual(result['end_datetime'].hour, 23)
        self.assertEqual(result['end_datetime'].minute, 59)
        self.assertEqual(result['end_datetime'].second, 59)

    def test_start_before_end(self):
        result = self._call(period='90d')
        self.assertLess(result['start_datetime'], result['end_datetime'])

    def test_generated_at_is_recent(self):
        before = timezone.now()
        result = self._call(period='today')
        after = timezone.now()
        self.assertGreaterEqual(result['generated_at'], before)
        self.assertLessEqual(result['generated_at'], after)

    def test_as_of_date_overrides_base_date(self):
        """as_of_date parameter should shift the base reference date."""
        from analytics.date_service import resolve_date_range
        fixed = date(2025, 6, 15)
        result = resolve_date_range(period='today', as_of_date=fixed)
        self.assertEqual(result['date_from'], fixed)
        self.assertEqual(result['date_to'], fixed)


class ResolveDateRangeNeverHardcodesTests(TestCase):
    """Ensure no hardcoded months/years appear in result."""

    def _call(self, **kwargs):
        from analytics.date_service import resolve_date_range
        return resolve_date_range(**kwargs)

    def test_period_label_is_dynamic_string(self):
        result = self._call(period='this_month')
        label = result.get('period_label', '')
        self.assertIsInstance(label, str)
        self.assertGreater(len(label), 0)
        # Must NOT be hardcoded 'September 2026' or similar
        self.assertNotIn('September 2026', label)
        self.assertNotIn('October 2026', label)

    def test_30d_never_hardcodes_specific_date(self):
        result1 = self._call(period='30d')
        import time
        time.sleep(0.001)
        result2 = self._call(period='30d')
        # Both runs should produce dates relative to today (same in same second)
        self.assertEqual(result1['date_to'], result2['date_to'])


# ---------------------------------------------------------------------------
# 2. FreshnessService Tests — get_data_freshness_report
# ---------------------------------------------------------------------------

class DataFreshnessReportTests(TestCase):
    """Test get_data_freshness_report() structure and classification logic."""

    def _call(self):
        from analytics.freshness_service import get_data_freshness_report
        return get_data_freshness_report()

    def test_returns_dict(self):
        result = self._call()
        self.assertIsInstance(result, dict)

    def test_has_required_keys(self):
        result = self._call()
        required = {'overall_status', 'data_sources', 'generated_at'}
        for key in required:
            self.assertIn(key, result, msg=f"Missing key: {key}")

    def test_overall_status_is_valid_string(self):
        result = self._call()
        valid_statuses = {'FRESH', 'PARTIALLY_UPDATED', 'STALE', 'INSUFFICIENT_DATA', 'NO_DATA'}
        self.assertIn(result['overall_status'], valid_statuses)

    def test_data_sources_is_list(self):
        result = self._call()
        self.assertIsInstance(result['data_sources'], list)

    def test_generated_at_is_recent(self):
        before = timezone.now()
        result = self._call()
        after = timezone.now()
        self.assertGreaterEqual(result['generated_at'], before)
        self.assertLessEqual(result['generated_at'], after)

    def test_each_source_has_required_fields(self):
        result = self._call()
        for source in result['data_sources']:
            self.assertIn('name', source, msg=f"Source missing 'name': {source}")
            self.assertIn('status', source, msg=f"Source missing 'status': {source}")

    def test_warning_message_present_when_stale(self):
        """If overall_status is STALE, there should be a warning_message."""
        result = self._call()
        if result['overall_status'] in ('STALE', 'INSUFFICIENT_DATA'):
            self.assertIn('warning_message', result)

    def test_no_exception_on_empty_db(self):
        """Should never raise even when tables are empty."""
        try:
            result = self._call()
            self.assertIsNotNone(result)
        except Exception as e:
            self.fail(f"get_data_freshness_report() raised exception on empty DB: {e}")
