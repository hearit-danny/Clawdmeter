"""Weekly pace against a five-workday budget.

Each Monday-to-Friday in the 7-day window earns 20% of the weekly budget,
linearly across its working hours (WORKDAY_START to WORKDAY_END, local time).
Outside working hours the expected value holds still until the next workday
opens. A workday belongs to the window that contains its close, so when the
reset falls inside working hours the window's first day starts at the reset
and the sliver before the next reset counts toward the next window. Every
window therefore holds exactly five 20% days.

surplus = expected - used: positive means under budget, negative means over.
With a Wednesday 08:00 reset, Monday 16:00 expects 80% (Wed/Thu/Fri/Mon), so
68% used reads +12 and stays there until Tuesday 07:30.
"""
import datetime

WORKDAY_START = datetime.time(7, 30)
WORKDAY_END = datetime.time(16, 0)

_DAY = datetime.timedelta(days=1)


def expected_weekly_pct(reset_at: datetime.datetime, now: datetime.datetime) -> float:
    start = reset_at - 7 * _DAY
    earned = 0.0
    day = start.date()
    while day <= reset_at.date():
        opens = datetime.datetime.combine(day, WORKDAY_START)
        closes = datetime.datetime.combine(day, WORKDAY_END)
        if day.weekday() < 5 and start < closes <= reset_at:
            opens = max(opens, start)
            earned += min(max((now - opens) / (closes - opens), 0.0), 1.0)
        day += _DAY
    return earned * 20.0


def weekly_surplus_pct(weekly_pct, weekly_reset_mins, now=None):
    """Surplus in weekly-% points, or None when the reset time is unknown."""
    if not weekly_reset_mins or weekly_reset_mins <= 0:
        return None
    now = now or datetime.datetime.now()
    reset_at = now + datetime.timedelta(minutes=weekly_reset_mins)
    return round(expected_weekly_pct(reset_at, now) - weekly_pct, 1)
