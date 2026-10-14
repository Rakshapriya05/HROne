"""Pure unit tests for the business rules R1-R5, R7 and R8. No database needed."""
from datetime import date, datetime, timedelta

import pytest

from app import main
from tests.conftest import ist_ms

IST, UTC = main.IST, main.UTC


def at(day, hh, mm, ss=0):
    """IST wall-clock time as an aware datetime on the given date."""
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=IST)


MON = date(2026, 7, 6)


# ---- R2: late ------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hh,mm,ss,expected",
    [
        (9, 30, 0, 0),
        (9, 40, 0, 0),  # exactly 10:00 after start is still inside grace
        (9, 40, 1, 10),  # the contract's own example
        (10, 15, 59, 45),  # the contract's own example
        (10, 5, 0, 35),
        (8, 0, 0, 0),  # early
    ],
)
def test_late_minutes(hh, mm, ss, expected):
    assert main.compute_late_minutes(at(MON, hh, mm, ss), MON, "09:30", "18:30") == expected


def test_late_is_measured_in_ist_not_utc():
    # 04:35 UTC is 10:05 IST. A naive implementation using the UTC clock would get this wrong.
    punch = datetime(2026, 7, 6, 4, 35, tzinfo=UTC)
    assert main.compute_late_minutes(punch, MON, "09:30", "18:30") == 35


def test_late_for_overnight_shift_punching_in_after_midnight():
    # Shift 22:00-06:00 that began on Monday; punch-in at 22:30 Monday is 30 min late.
    assert main.compute_late_minutes(at(MON, 22, 30), MON, "22:00", "06:00") == 30
    # A punch-in at 00:30 Tuesday belongs to Monday's shift: 150 minutes after 22:00.
    tuesday_early = at(MON + timedelta(days=1), 0, 30)
    assert main.compute_late_minutes(tuesday_early, MON, "22:00", "06:00") == 150


# ---- R3: overtime --------------------------------------------------------- #
@pytest.mark.parametrize(
    "hh,mm,ss,expected",
    [(18, 29, 0, 0), (18, 59, 59, 0), (19, 0, 0, 30), (19, 10, 0, 40), (19, 10, 59, 40), (21, 0, 0, 150)],
)
def test_overtime_minutes(hh, mm, ss, expected):
    assert main.compute_overtime(at(MON, hh, mm, ss), MON, "09:30", "18:30") == expected


def test_overtime_overnight_shift_ends_next_day():
    out = at(MON + timedelta(days=1), 6, 40)
    assert main.compute_overtime(out, MON, "22:00", "06:00") == 40
    assert main.compute_overtime(at(MON + timedelta(days=1), 6, 29), MON, "22:00", "06:00") == 0


# ---- R4 / R5: work hours and half day ------------------------------------- #
def test_work_hours_rounds_half_up_not_bankers():
    start = at(MON, 9, 0, 0)
    # 18 s = 0.005 h exactly. Half-up gives 0.01; Python's round() gives 0.0.
    assert main.compute_work_hours(start, start + timedelta(seconds=18)) == 0.01
    # 4.495 h exactly -> 4.50 (half-up).
    assert main.compute_work_hours(start, start + timedelta(seconds=16182)) == 4.5
    assert main.compute_work_hours(start, start + timedelta(seconds=16181)) == 4.49


@pytest.mark.parametrize(
    "seconds,expected",
    # Exact ties (x.xx5 hours) where float round() goes the wrong way; found by brute force.
    [(54, 0.02), (162, 0.05), (450, 0.13), (630, 0.18), (30186, 8.39), (30222, 8.4), (30258, 8.41)],
)
def test_work_hours_half_up_where_float_rounding_is_wrong(seconds, expected):
    start = at(MON, 9, 0, 0)
    assert main.compute_work_hours(start, start + timedelta(seconds=seconds)) == expected
    assert round(seconds / 3600, 2) != expected  # proves this case discriminates


def test_work_hours_matches_contract_sample():
    assert main.compute_work_hours(at(MON, 9, 28), at(MON, 18, 35)) == 9.12


def test_half_day_uses_the_rounded_value():
    start = at(MON, 9, 30)
    exactly_4_495 = main.derive_fields("PRESENT", start, start + timedelta(seconds=16182), MON, "09:30", "18:30")
    assert exactly_4_495["work_hours"] == 4.5 and exactly_4_495["half_day"] is False
    just_below = main.derive_fields("PRESENT", start, start + timedelta(seconds=16181), MON, "09:30", "18:30")
    assert just_below["work_hours"] == 4.49 and just_below["half_day"] is True


def test_derive_fields_defaults():
    start = at(MON, 9, 30)
    open_record = main.derive_fields("WFH", start, None, MON, "09:30", "18:30")
    assert open_record == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    leave = main.derive_fields("LEAVE", None, None, MON, "09:30", "18:30")
    assert leave == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# ---- R1: attendance date and truncation ----------------------------------- #
def test_attendance_date_is_the_ist_date():
    # 20:00 UTC on the 6th is 01:30 IST on the 7th.
    assert main.attendance_date(datetime(2026, 7, 6, 20, 0, tzinfo=UTC), "09:30", "18:30") == date(2026, 7, 7)


def test_attendance_date_overnight_rule():
    # 01:00 IST is earlier than shift_end 06:00, so it belongs to the previous day's shift.
    assert main.attendance_date(at(date(2026, 7, 7), 1, 0), "22:00", "06:00") == date(2026, 7, 6)
    assert main.attendance_date(at(date(2026, 7, 7), 5, 59, 59), "22:00", "06:00") == date(2026, 7, 6)
    # at exactly shift_end it is no longer "earlier than", so it is the 7th itself
    assert main.attendance_date(at(date(2026, 7, 7), 6, 0, 0), "22:00", "06:00") == date(2026, 7, 7)
    assert main.attendance_date(at(date(2026, 7, 6), 21, 55), "22:00", "06:00") == date(2026, 7, 6)


def test_day_shift_is_not_treated_as_overnight():
    assert main.attendance_date(at(date(2026, 7, 7), 1, 0), "09:30", "18:30") == date(2026, 7, 7)


def test_shift_end_equal_to_start_is_never_created_but_counts_as_overnight():
    assert main.is_overnight("09:30", "09:30") is True


def test_epoch_ms_is_truncated_to_whole_seconds():
    truncated = main.ms_to_dt(ist_ms(2026, 7, 6, 9, 40, 0, millis=900))
    assert truncated.microsecond == 0
    assert main.dt_to_ms(truncated) == ist_ms(2026, 7, 6, 9, 40, 0)


def test_ms_round_trip():
    ms = ist_ms(2026, 7, 6, 10, 5)
    assert main.dt_to_ms(main.ms_to_dt(ms)) == ms
    assert main.dt_to_ms(datetime(1970, 1, 1, 0, 0, 1)) == 1000  # naive treated as UTC


# ---- R7 / R8 helpers ------------------------------------------------------ #
def test_count_weekdays():
    assert main.count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert main.count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15  # mid-month joiner
    assert main.count_weekdays(date(2026, 7, 4), date(2026, 7, 5)) == 0  # a weekend
    assert main.count_weekdays(date(2026, 8, 1), date(2026, 7, 31)) == 0  # joined after the month
    assert main.count_weekdays(date(2026, 2, 1), date(2026, 2, 28)) == 20


def test_month_bounds_handles_leap_year():
    assert main.month_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert main.month_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))


@pytest.mark.parametrize(
    "value,places,expected",
    [(0.125, 2, 0.13), (2.675, 2, 2.68), (0.285, 2, 0.29), (1.005, 2, 1.01), (0.00005, 4, 0.0001), (15.217391, 2, 15.22)],
)
def test_half_up_rounding(value, places, expected):
    assert main.half_up(value, places) == expected


def test_page_skip_is_zero_based_and_bounded():
    assert main.page_skip(1, 20) == 0
    assert main.page_skip(3, 20) == 40
    assert main.page_skip(10**30, 100) <= 2**62
