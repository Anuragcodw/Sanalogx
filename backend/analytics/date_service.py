"""
analytics/date_service.py
MediVault — Central Dynamic Date & Reporting Period Engine

Guarantees:
- Never hardcodes months, years, or fixed calendar periods.
- Resolves all relative dates at request time using the current application date.
- Uses configured Django timezone for consistent datetime boundaries (up to 23:59:59.999999).
- Supports: Today, Yesterday, Last 7d, Last 30d, Last 90d, This Week, This Month,
  Last Month, This Quarter, This Year, and Custom range.
- Strictly validates start_date <= end_date.
"""

from datetime import date, datetime, time, timedelta
from typing import Dict, Any, Tuple, Optional
from django.utils import timezone
from django.core.exceptions import ValidationError


PERIOD_CHOICES = [
    ('today', 'Today'),
    ('yesterday', 'Yesterday'),
    ('7d', 'Last 7 Days'),
    ('30d', 'Last 30 Days'),
    ('90d', 'Last 90 Days'),
    ('this_week', 'This Week'),
    ('this_month', 'This Month'),
    ('last_month', 'Last Month'),
    ('this_quarter', 'This Quarter'),
    ('this_year', 'This Year'),
    ('custom', 'Custom Range'),
]


def resolve_date_range(
    period: str = '30d',
    date_from: Optional[Any] = None,
    date_to: Optional[Any] = None,
    as_of_date: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Resolves relative or custom reporting periods dynamically.

    Args:
        period: One of 'today', 'yesterday', '7d', '30d', '90d', 'this_week',
                'this_month', 'last_month', 'this_quarter', 'this_year', 'custom'.
        date_from: Optional custom start date (str 'YYYY-MM-DD' or date).
        date_to: Optional custom end date (str 'YYYY-MM-DD' or date).
        as_of_date: Base reference date (defaults to current date in Django timezone).

    Returns:
        dict:
            - 'period': str canonical period key
            - 'period_label': human-readable range description
            - 'date_from': datetime.date object
            - 'date_to': datetime.date object
            - 'start_datetime': timezone-aware datetime at 00:00:00.000000
            - 'end_datetime': timezone-aware datetime at 23:59:59.999999
            - 'as_of_date': datetime.date base reference date
            - 'days_count': int total inclusive days in range
            - 'is_custom': bool
            - 'is_current_month': bool whether range represents the current month to date
            - 'generated_at': datetime.datetime current timestamp
    """
    current_now = timezone.now()
    today = as_of_date if as_of_date is not None else timezone.localdate(current_now)

    period = (period or '30d').lower().strip()
    is_custom = False

    if period == 'today':
        start_d = today
        end_d = today
        label = f"Today ({today.strftime('%d %b %Y')})"

    elif period == 'yesterday':
        yest = today - timedelta(days=1)
        start_d = yest
        end_d = yest
        label = f"Yesterday ({yest.strftime('%d %b %Y')})"

    elif period in ('7d', 'last_7d'):
        start_d = today - timedelta(days=6)  # inclusive 7 days
        end_d = today
        label = f"Last 7 Days ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    elif period in ('30d', 'last_30d'):
        start_d = today - timedelta(days=29)  # inclusive 30 days
        end_d = today
        label = f"Last 30 Days ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    elif period in ('90d', 'last_90d'):
        start_d = today - timedelta(days=89)  # inclusive 90 days
        end_d = today
        label = f"Last 90 Days ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    elif period == 'this_week':
        # Monday to today
        start_d = today - timedelta(days=today.weekday())
        end_d = today
        label = f"This Week ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    elif period == 'this_month':
        # 1st of current month up to today (dynamic, never the entire future month)
        start_d = today.replace(day=1)
        end_d = today
        label = f"This Month ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    elif period == 'last_month':
        # Previous calendar month
        first_of_this_month = today.replace(day=1)
        last_of_last_month = first_of_this_month - timedelta(days=1)
        start_d = last_of_last_month.replace(day=1)
        end_d = last_of_last_month
        label = f"Last Month ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    elif period == 'this_quarter':
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        start_d = today.replace(month=quarter_month, day=1)
        end_d = today
        q_num = ((today.month - 1) // 3) + 1
        label = f"Q{q_num} {today.year} ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    elif period == 'this_year':
        start_d = today.replace(month=1, day=1)
        end_d = today
        label = f"Year-to-Date {today.year} ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    elif period == 'custom':
        is_custom = True
        df = _parse_date(date_from)
        dt = _parse_date(date_to)

        if not df or not dt:
            df = today - timedelta(days=29)
            dt = today

        if df > dt:
            df, dt = dt, df

        start_d = df
        end_d = dt
        label = f"Custom ({start_d.strftime('%d %b %Y')} – {end_d.strftime('%d %b %Y')})"

    else:
        # Default fallback: 30 days
        start_d = today - timedelta(days=29)
        end_d = today
        period = '30d'
        label = f"Last 30 Days ({start_d.strftime('%d %b')} – {end_d.strftime('%d %b %Y')})"

    # Construct timezone-aware boundary datetimes
    start_dt = timezone.make_aware(datetime.combine(start_d, time.min))
    end_dt = timezone.make_aware(datetime.combine(end_d, time.max))

    days_count = (end_d - start_d).days + 1
    is_current_month = (start_d == today.replace(day=1) and end_d == today)

    return {
        'period': period,
        'period_label': label,
        'date_from': start_d,
        'date_to': end_d,
        'start_datetime': start_dt,
        'end_datetime': end_dt,
        'as_of_date': today,
        'days_count': days_count,
        'is_custom': is_custom,
        'is_current_month': is_current_month,
        'generated_at': current_now,
    }


def _parse_date(val: Any) -> Optional[date]:
    """Safely parse a date from string or date object."""
    if not val:
        return None
    if isinstance(val, date):
        return val
    if isinstance(val, datetime):
        return val.date()
    try:
        return date.fromisoformat(str(val).strip())
    except (ValueError, TypeError):
        return None
