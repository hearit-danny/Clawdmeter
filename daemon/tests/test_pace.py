import datetime

import pytest

from daemon.pace import expected_weekly_pct, weekly_surplus_pct

# Window Wed 2026-10-07 08:00 -> Wed 2026-10-14 08:00.
RESET = datetime.datetime(2026, 10, 14, 8, 0)


def _mins_to_reset(now):
    return (RESET - now) / datetime.timedelta(minutes=1)


@pytest.mark.parametrize("now, expected", [
    (datetime.datetime(2026, 10, 7, 8, 0), 0.0),     # window opens
    (datetime.datetime(2026, 10, 7, 20, 0), 10.0),   # half of Wednesday's slot
    (datetime.datetime(2026, 10, 10, 8, 0), 60.0),   # Wed/Thu/Fri earned
    (datetime.datetime(2026, 10, 11, 23, 0), 60.0),  # weekend earns nothing
    (datetime.datetime(2026, 10, 12, 20, 0), 70.0),  # half of Monday's slot
    (datetime.datetime(2026, 10, 14, 8, 0), 100.0),  # window closes
])
def test_expected_follows_workday_slots(now, expected):
    assert expected_weekly_pct(RESET, now) == pytest.approx(expected)


@pytest.mark.parametrize("used, surplus", [(17, 43.0), (78, -18.0)])
def test_saturday_surplus_and_deficit(used, surplus):
    now = datetime.datetime(2026, 10, 10, 15, 0)
    assert weekly_surplus_pct(used, _mins_to_reset(now), now) == surplus


@pytest.mark.parametrize("wr", [None, 0, -1])
def test_unknown_reset_gives_no_value(wr):
    assert weekly_surplus_pct(17, wr) is None
