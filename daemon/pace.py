"""Weekly pace against a five-workday budget.

The 7-day window is split into seven 24h slots that start at the window's
start (reset time minus 7 days), so slot boundaries fall at the reset's time
of day. The five slots that start Monday to Friday each earn 20% of the
weekly budget, linearly across the slot; weekend slots earn nothing.

surplus = expected - used: positive means under budget, negative means over.
With a Wednesday 08:00 reset, by Saturday the Wed/Thu/Fri slots have earned
60%, so 17% used reads +43 and 78% used reads -18 until Monday 08:00.
"""
import datetime

_DAY = datetime.timedelta(days=1)


def expected_weekly_pct(reset_at: datetime.datetime, now: datetime.datetime) -> float:
    start = reset_at - 7 * _DAY
    earned = 0.0
    for i in range(7):
        slot = start + i * _DAY
        if slot.weekday() < 5:
            earned += min(max((now - slot) / _DAY, 0.0), 1.0)
    return earned * 20.0


def weekly_surplus_pct(weekly_pct, weekly_reset_mins, now=None):
    """Surplus in weekly-% points, or None when the reset time is unknown."""
    if not weekly_reset_mins or weekly_reset_mins <= 0:
        return None
    now = now or datetime.datetime.now()
    reset_at = now + datetime.timedelta(minutes=weekly_reset_mins)
    return round(expected_weekly_pct(reset_at, now) - weekly_pct, 1)
