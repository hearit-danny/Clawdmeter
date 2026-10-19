import datetime

import pytest

from daemon.pace import expected_weekly_pct, weekly_surplus_pct

# Window Wed 2026-10-07 08:00 -> Wed 2026-10-14 08:00.
RESET = datetime.datetime(2026, 10, 14, 8, 0)


def _mins_to_reset(now):
    return (RESET - now) / datetime.timedelta(minutes=1)


@pytest.mark.parametrize("now, expected", [
    (datetime.datetime(2026, 10, 7, 8, 0), 0.0),     # window opens at the reset
    (datetime.datetime(2026, 10, 7, 12, 0), 10.0),   # half of Wednesday's 08:00-16:00
    (datetime.datetime(2026, 10, 7, 20, 0), 20.0),   # held after close of business
    (datetime.datetime(2026, 10, 10, 8, 0), 60.0),   # Wed/Thu/Fri earned
    (datetime.datetime(2026, 10, 11, 23, 0), 60.0),  # weekend earns nothing
    (datetime.datetime(2026, 10, 12, 7, 30), 60.0),  # Monday opens
    (datetime.datetime(2026, 10, 12, 11, 45), 70.0), # half of Monday's 07:30-16:00
    (datetime.datetime(2026, 10, 12, 16, 0), 80.0),  # Monday closes
    (datetime.datetime(2026, 10, 13, 7, 30), 80.0),  # held overnight
    (datetime.datetime(2026, 10, 14, 7, 45), 100.0), # sliver before reset is next week's
])
def test_expected_follows_working_hours(now, expected):
    assert expected_weekly_pct(RESET, now) == pytest.approx(expected)


@pytest.mark.parametrize("used, surplus", [(17, 43.0), (78, -18.0)])
def test_saturday_surplus_and_deficit(used, surplus):
    now = datetime.datetime(2026, 10, 10, 15, 0)
    assert weekly_surplus_pct(used, _mins_to_reset(now), now) == surplus


@pytest.mark.parametrize("now, used, surplus", [
    (datetime.datetime(2026, 10, 12, 7, 0), 64, -4.0),   # before work: 60 - 64
    (datetime.datetime(2026, 10, 12, 17, 17), 68, 12.0), # after work: 80 - 68
    (datetime.datetime(2026, 10, 13, 7, 30), 68, 12.0),  # unchanged at Tuesday's open
])
def test_monday_examples(now, used, surplus):
    assert weekly_surplus_pct(used, _mins_to_reset(now), now) == surplus


@pytest.mark.parametrize("reset", [
    datetime.datetime(2026, 10, 14, 16, 0),   # reset at close of business
    datetime.datetime(2026, 10, 14, 18, 0),   # reset after hours
    datetime.datetime(2026, 10, 17, 9, 0),    # reset on a Saturday
])
def test_every_window_holds_five_days(reset):
    assert expected_weekly_pct(reset, reset) == pytest.approx(100.0)
    assert expected_weekly_pct(reset, reset - 7 * datetime.timedelta(days=1)) == pytest.approx(0.0)


@pytest.mark.parametrize("wr", [None, 0, -1])
def test_unknown_reset_gives_no_value(wr):
    assert weekly_surplus_pct(17, wr) is None
