"""Analytics endpoints.

Every analytics test runs twice via the `analytics_client` fixture:
  [interpreter] tests/mini_mongo.py, a reference interpreter of the aggregation operators used
                (always runs; proves the pipelines' logic, NOT real-server behaviour)
  [mongodb]     a real MongoDB (skipped, with a reason, when none is reachable)

Every expectation is checked twice: against hand-computed literals AND against the independent
oracle in analytics_data.py."""
import pytest

from tests.analytics_data import (
    LOGS,
    oracle_leaderboard,
    oracle_monthly,
    oracle_summary,
    oracle_trend,
)
from tests.conftest import EMPLOYEES, employee_doc, seed_employees

EMP_DOCS = [employee_doc(*row) for row in EMPLOYEES]


def load(database):
    seed_employees(database)
    database.attendance_logs.insert_many([dict(x) for x in LOGS])


@pytest.fixture
def loaded(analytics_client):
    client, database = analytics_client
    load(database)
    return client


real_client = loaded  # the same fixture: it runs on the interpreter and on real MongoDB


# ============================ employee monthly ============================== #
def monthly(client, code, month="2026-07"):
    response = client.get(f"/analytics/employees/{code}/monthly?month={month}")
    assert response.status_code == 200, response.text
    return response.json()


def test_monthly_busy_employee_literals(loaded):
    assert monthly(loaded, "EMP0001") == {
        "emp_code": "EMP0001", "month": "2026-07",
        "working_days": 23,
        "present_days": 3.5,  # 1 + 1 + 0.5 (WFH half day) + 1 (ON_DUTY); Saturday excluded; LEAVE excluded
        "leave_days": 1,
        "late_count": 3,  # 35, 12 and the Saturday's 20
        "total_late_minutes": 67,  # weekend record still counts
        "total_overtime_minutes": 75,  # 40 + the Saturday's 35
        "attendance_pct": 15.22,  # 3.5 / 23 * 100 = 15.2173..
    }  # fmt: skip


def test_monthly_mid_month_joiner_counts_working_days_from_joined_on(loaded):
    body = monthly(loaded, "EMP0002")  # joined Mon 2026-07-13
    assert body["working_days"] == 15
    assert body["present_days"] == 2.0 and body["attendance_pct"] == 13.33
    assert body["late_count"] == 1 and body["total_late_minutes"] == 11  # 11 is late (> 10 grace was applied at punch-in)


def test_monthly_employee_with_no_logs_is_zero_not_null(loaded):
    body = monthly(loaded, "EMP0006")
    assert body["working_days"] == 23 and body["present_days"] == 0.0 and body["attendance_pct"] == 0.0
    assert body["late_count"] == 0 and body["leave_days"] == 0
    assert body["total_late_minutes"] == 0 and body["total_overtime_minutes"] == 0


def test_monthly_employee_not_yet_joined_has_null_percentage(loaded):
    body = monthly(loaded, "EMP0007")  # joins 2026-08-01
    assert body["working_days"] == 0 and body["attendance_pct"] is None and body["present_days"] == 0.0


def test_monthly_weekend_half_day_counts_for_nothing_but_overtime_and_late(loaded):
    body = monthly(loaded, "EMP0005")
    assert body["present_days"] == 1.0  # Monday only; the Sunday half day is ignored
    assert body["late_count"] == 1 and body["total_overtime_minutes"] == 40


def test_monthly_open_record_counts_as_present_without_hours(loaded):
    body = monthly(loaded, "EMP0003")
    assert body["present_days"] == 3.0 and body["leave_days"] == 1 and body["late_count"] == 2


def test_monthly_other_months_are_isolated(loaded):
    june = monthly(loaded, "EMP0001", "2026-06")
    assert june["present_days"] == 1.0 and june["late_count"] == 1 and june["total_late_minutes"] == 50
    assert june["working_days"] == 22
    assert monthly(loaded, "EMP0001", "2026-08")["total_late_minutes"] == 60
    assert monthly(loaded, "EMP0001", "2026-02")["working_days"] == 20


def test_monthly_matches_oracle_for_every_employee_and_month(loaded):
    for emp in EMP_DOCS:
        for month in ("2026-06", "2026-07", "2026-08"):
            assert monthly(loaded, emp["emp_code"], month) == oracle_monthly(EMP_DOCS, LOGS, emp["emp_code"], month)


def test_monthly_unknown_employee_is_404(loaded):
    assert loaded.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404


def test_monthly_reads_stored_values_instead_of_recomputing(analytics_client):
    client, database = analytics_client
    seed_employees(database)
    odd = dict(LOGS[0], emp_code="EMP0001", late_minutes=7, overtime_minutes=3)  # inconsistent with punch times on purpose
    database.attendance_logs.insert_one(odd)
    body = monthly(client, "EMP0001")
    assert body["total_late_minutes"] == 7 and body["total_overtime_minutes"] == 3 and body["late_count"] == 1


# ============================ department summary ============================ #
def summary(client, month="2026-07", department=None):
    suffix = f"&department={department}" if department else ""
    response = client.get(f"/analytics/departments/summary?month={month}{suffix}")
    assert response.status_code == 200, response.text
    return response.json()


def test_summary_literals(real_client):
    body = summary(real_client)
    assert [i["department"] for i in body["items"]] == ["Engineering", "Sales", "Support"]  # sorted; Ops omitted
    eng, sales, support = body["items"]
    assert eng == {
        "department": "Engineering", "headcount": 2, "present_days": 5.5,
        "avg_work_hours": 7.37,  # (9+9.08+3.5+8+5 + 8.98+8) / 7 records = 7.3657 (Saturday record included)
        "late_count": 4, "total_late_minutes": 78, "leave_count": 1, "on_duty_count": 1,
    }  # fmt: skip
    assert sales["headcount"] == 3 and sales["leave_count"] == 1 and sales["on_duty_count"] == 1
    assert sales["late_count"] == 4 and sales["total_late_minutes"] == 70 + 70 + 20
    assert sales["present_days"] == 3 + 1.5 + 1  # EMP0003 3 (open record included), EMP0004 1.5, EMP0008 1 (Saturday excluded)
    assert support["headcount"] == 2  # EMP0006 has no logs and is still counted
    assert support["present_days"] == 1.0  # Sunday half day excluded
    assert support["avg_work_hours"] == 6.38  # (8.75 + 4.0) / 2 = 6.375 -> half-up 6.38


def test_summary_headcount_excludes_future_joiners_and_counts_logless_people(real_client):
    june = summary(real_client, "2026-06")
    eng = next(i for i in june["items"] if i["department"] == "Engineering")
    assert eng["headcount"] == 1  # EMP0002 joins in July
    august = summary(real_client, "2026-08")
    ops = next(i for i in august["items"] if i["department"] == "Ops")
    assert ops["headcount"] == 1 and ops["present_days"] == 0.0 and ops["avg_work_hours"] is None


def test_summary_department_filter_and_empty_month(real_client):
    only = summary(real_client, department="Sales")
    assert [i["department"] for i in only["items"]] == ["Sales"]
    assert summary(real_client, department="Ops")["items"] == []  # headcount 0 -> omitted
    assert summary(real_client, department="sales")["items"] == []  # case-sensitive
    january = summary(real_client, "2026-01")
    assert all(i["present_days"] == 0.0 and i["avg_work_hours"] is None for i in january["items"])


@pytest.mark.parametrize("month", ["2026-01", "2026-06", "2026-07", "2026-08"])
@pytest.mark.parametrize("department", [None, "Engineering", "Sales", "Support", "Ops"])
def test_summary_matches_oracle(real_client, month, department):
    assert summary(real_client, month, department) == oracle_summary(EMP_DOCS, LOGS, month, department)


# ============================ late leaderboard ============================== #
def board(client, query):
    response = client.get(f"/analytics/leaderboard/late?{query}")
    assert response.status_code == 200, response.text
    return response.json()


def test_leaderboard_ties_share_rank_and_next_rank_is_skipped(real_client):
    items = board(real_client, "month=2026-07")["items"]
    assert [(i["emp_code"], i["rank"], i["total_late_minutes"], i["late_count"]) for i in items] == [
        ("EMP0003", 1, 70, 2), ("EMP0004", 1, 70, 1), ("EMP0001", 3, 67, 3),
        ("EMP0008", 4, 20, 1), ("EMP0005", 5, 15, 1), ("EMP0002", 6, 11, 1),
    ]  # fmt: skip
    assert items[0]["name"] == "Neha Iyer" and items[0]["department"] == "Sales"


def test_leaderboard_limit_applies_after_ranking(real_client):
    assert [i["emp_code"] for i in board(real_client, "month=2026-07&limit=1")["items"]] == ["EMP0003", "EMP0004"]
    assert len(board(real_client, "month=2026-07&limit=2")["items"]) == 2  # ranks 1,1 -> rank 3 is > 2
    assert [i["rank"] for i in board(real_client, "month=2026-07&limit=3")["items"]] == [1, 1, 3]
    assert [i["rank"] for i in board(real_client, "month=2026-07&limit=4")["items"]] == [1, 1, 3, 4]


def test_leaderboard_ignores_orphan_logs_other_months_and_non_late_employees(real_client):
    codes = [i["emp_code"] for i in board(real_client, "month=2026-07&limit=50")["items"]]
    assert "EMP0099" not in codes  # log for a non-existent employee
    assert "EMP0006" not in codes and "EMP0007" not in codes  # never late
    assert board(real_client, "month=2026-01")["items"] == []
    assert board(real_client, "month=2026-06")["items"][0]["total_late_minutes"] == 50


def test_leaderboard_department_ranks_inside_the_department(real_client):
    sales = board(real_client, "month=2026-07&department=Sales")["items"]
    assert [(i["emp_code"], i["rank"]) for i in sales] == [("EMP0003", 1), ("EMP0004", 1), ("EMP0008", 3)]
    eng = board(real_client, "month=2026-07&department=Engineering&limit=1")["items"]
    assert [(i["emp_code"], i["rank"]) for i in eng] == [("EMP0001", 1)]  # NOT rank 3, as in the global board
    assert board(real_client, "month=2026-07&department=Ops")["items"] == []


@pytest.mark.parametrize("month", ["2026-06", "2026-07", "2026-08"])
@pytest.mark.parametrize("department", [None, "Sales", "Engineering", "Support"])
@pytest.mark.parametrize("limit", [1, 2, 3, 10, 50])
def test_leaderboard_matches_oracle(real_client, month, department, limit):
    query = f"month={month}&limit={limit}" + (f"&department={department}" if department else "")
    assert board(real_client, query) == oracle_leaderboard(EMP_DOCS, LOGS, month, limit, department)


# ============================ department trend ============================== #
def trend(client, department, start, end):
    response = client.get(f"/analytics/departments/{department}/trend", params={"from": start, "to": end})
    assert response.status_code == 200, response.text
    return response.json()


def test_trend_literals_with_gaps_weekends_and_moving_average(real_client):
    body = trend(real_client, "Sales", "2026-07-01", "2026-07-12")
    assert body["department"] == "Sales" and len(body["items"]) == 12  # exactly one row per calendar day
    rows = {r["date"]: r for r in body["items"]}
    assert [r["date"] for r in body["items"]] == sorted(rows)

    sat = rows["2026-07-04"]  # weekend with an ON_DUTY record: counted as present, but no rate
    assert sat["is_working_day"] is False and sat["present_count"] == 1.0 and sat["attendance_rate"] is None
    assert rows["2026-07-05"]["present_count"] == 0.0 and rows["2026-07-05"]["attendance_rate"] is None

    assert rows["2026-07-01"]["attendance_rate"] == 0.0  # a working day with no records is 0.0, not null
    assert rows["2026-07-06"]["present_count"] == 3.0 and rows["2026-07-06"]["attendance_rate"] == 1.0
    assert rows["2026-07-06"]["late_count"] == 3
    assert rows["2026-07-07"]["present_count"] == 1.5 and rows["2026-07-07"]["attendance_rate"] == 0.5
    assert rows["2026-07-08"]["attendance_rate"] == 0.0 and rows["2026-07-08"]["present_count"] == 0.0  # LEAVE + ABSENT
    assert rows["2026-07-09"]["attendance_rate"] == 0.3333  # open record counts as present

    averages = {d: r["moving_avg_7d"] for d, r in rows.items()}
    assert averages["2026-07-01"] == 0.0  # window of one row
    assert averages["2026-07-04"] == 0.0  # weekend null is skipped
    assert averages["2026-07-06"] == 0.25  # (0 + 0 + 0 + 1.0) / 4
    assert averages["2026-07-07"] == 0.3  # 1.5 / 5
    assert averages["2026-07-08"] == 0.3
    assert averages["2026-07-09"] == 0.3667  # 1.8333 / 5 -> half-up
    assert averages["2026-07-12"] == 0.3667


def test_trend_headcount_follows_joined_on_day_by_day(real_client):
    rows = trend(real_client, "Engineering", "2026-07-10", "2026-07-16")["items"]
    assert [r["headcount"] for r in rows] == [1, 1, 1, 2, 2, 2, 2]  # EMP0002 joins Mon 2026-07-13
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-07-10"]["present_count"] == 0.0 and by_date["2026-07-10"]["attendance_rate"] == 0.0  # LEAVE
    assert by_date["2026-07-11"]["present_count"] == 1.0 and by_date["2026-07-11"]["attendance_rate"] is None
    assert by_date["2026-07-14"]["attendance_rate"] == 0.5  # 1 of 2 present
    assert by_date["2026-07-15"]["late_count"] == 1


def test_trend_moving_average_ignores_rows_before_from(real_client):
    # Same day, different range starts: the shorter range must NOT look at earlier rows.
    short = {r["date"]: r for r in trend(real_client, "Sales", "2026-07-08", "2026-07-09")["items"]}
    assert short["2026-07-08"]["moving_avg_7d"] == 0.0  # only itself (0.0), not 0.3 from the wider window
    assert short["2026-07-09"]["moving_avg_7d"] == 0.1667  # (0.0 + 0.3333) / 2 = 0.16665 -> half-up


def test_trend_with_no_records_at_all_still_returns_every_day(real_client):
    rows = trend(real_client, "Sales", "2026-03-02", "2026-03-11")["items"]  # Mon..Wed, no logs in March
    assert len(rows) == 10 and all(r["present_count"] == 0.0 and r["late_count"] == 0 for r in rows)
    assert rows[0]["attendance_rate"] == 0.0 and rows[0]["is_working_day"] is True
    assert rows[5]["is_working_day"] is False and rows[5]["attendance_rate"] is None  # Sat 03-07


def test_trend_before_anyone_joined_has_null_rates_and_null_average(real_client):
    rows = trend(real_client, "Sales", "2025-01-01", "2025-01-10")["items"]  # Sales joined 2026-02-01
    assert len(rows) == 10 and all(r["headcount"] == 0 for r in rows)
    assert all(r["attendance_rate"] is None and r["moving_avg_7d"] is None for r in rows)


def test_trend_range_limits_and_errors(real_client):
    assert len(trend(real_client, "Sales", "2026-07-01", "2026-09-30")["items"]) == 92  # exactly 92 days is allowed
    assert len(trend(real_client, "Sales", "2026-07-01", "2026-07-01")["items"]) == 1
    assert real_client.get("/analytics/departments/Nowhere/trend?from=2026-07-01&to=2026-07-05").status_code == 404
    assert real_client.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-10-01").status_code == 422  # 93 days


@pytest.mark.parametrize(
    "department,start,end",
    [
        ("Sales", "2026-07-01", "2026-07-31"),
        ("Sales", "2026-06-20", "2026-08-10"),
        ("Engineering", "2026-07-01", "2026-07-31"),
        ("Engineering", "2026-07-12", "2026-07-14"),
        ("Support", "2026-07-01", "2026-07-20"),
        ("Ops", "2026-07-25", "2026-08-05"),
        ("Sales", "2026-07-04", "2026-07-04"),
    ],
)
def test_trend_matches_oracle(real_client, department, start, end):
    assert trend(real_client, department, start, end) == oracle_trend(EMP_DOCS, LOGS, department, start, end)
