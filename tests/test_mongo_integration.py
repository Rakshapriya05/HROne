"""Tests that need a REAL MongoDB server (skipped, with a reason, when none is reachable):
concurrency guarantees, unique indexes, and explain plans. mongomock cannot prove any of these.

Run:  $env:TEST_MONGO_URI = "mongodb://localhost:27017" ; python -m pytest tests/test_mongo_integration.py -v
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.conftest import employee_doc, ist_ms, seed_employees

THREADS = 24


def fire(requests):
    """Send all requests at (nearly) the same instant, each from its own thread and client."""
    barrier = threading.Barrier(len(requests))

    def send(request):
        method, url, body = request
        client = TestClient(main.app)
        barrier.wait()
        return client.request(method, url, json=body)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(send, requests))


def codes(responses):
    return sorted(r.status_code for r in responses)


@pytest.fixture
def staffed(real_db):
    seed_employees(real_db)
    return real_db


# ============================ concurrency ==================================== #
def test_health_is_200_with_real_mongo(real_db):
    assert TestClient(main.app).get("/health").json() == {"status": "ok"}


def test_simultaneous_punch_ins_yield_exactly_one_201(staffed):
    body = {"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 30)}
    responses = fire([("POST", "/attendance/punch-in", body)] * THREADS)
    assert codes(responses) == [201] + [409] * (THREADS - 1)
    assert staffed.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_simultaneous_punch_ins_for_different_employees_do_not_conflict(staffed):
    requests = [
        ("POST", "/attendance/punch-in", {"emp_code": f"EMP000{n}", "punched_at": ist_ms(2026, 7, 6, 9, 30)})
        for n in (1, 2, 3, 4, 6, 8)
    ]
    assert codes(fire(requests)) == [201] * 6


def test_simultaneous_punch_ins_on_different_dates_all_succeed(staffed):
    requests = [
        ("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, day, 9, 30)})
        for day in range(1, 21)
    ]
    assert codes(fire(requests)) == [201] * 20


def test_simultaneous_punch_outs_yield_exactly_one_200(staffed):
    TestClient(main.app).post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 0)})
    body = {"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 18, 0)}
    responses = fire([("POST", "/attendance/punch-out", body)] * THREADS)
    assert codes(responses) == [200] + [409] * (THREADS - 1)
    assert staffed.attendance_logs.find_one({"emp_code": "EMP0001"})["work_hours"] == 9.0


def test_simultaneous_employee_creates_yield_exactly_one_201(real_db):
    body = {"emp_code": "EMP0042", "name": "Race", "email": "r@example.com", "department": "X", "joined_on": "2026-01-01"}
    responses = fire([("POST", "/employees", body)] * THREADS)
    assert codes(responses) == [201] + [409] * (THREADS - 1)
    assert real_db.employees.count_documents({"emp_code": "EMP0042"}) == 1


def test_concurrent_regularizations_never_lose_a_history_entry(staffed):
    client = TestClient(main.app)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 10, 5)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 19, 10)})
    requests = [
        (
            "PATCH",
            "/attendance/EMP0001/2026-07-07",
            {"reason": f"correction {n}", "regularized_by": f"hr{n}", "punch_out": ist_ms(2026, 7, 7, 19, 11 + n)},
        )
        for n in range(THREADS)
    ]
    responses = fire(requests)
    assert set(codes(responses)) <= {200, 409}
    winners = codes(responses).count(200)
    assert winners >= 1
    history = staffed.attendance_logs.find_one({"emp_code": "EMP0001"})["history"]
    assert len(history) == winners  # one entry per successful PATCH, none lost, none duplicated
    assert len({h["reason"] for h in history}) == winners


# ============================ indexes ======================================== #
def index_keys(collection):
    return {tuple(v["key"]): v for v in collection.index_information().values()}


def test_indexes_exist_and_creation_is_idempotent(real_db):
    main.ensure_indexes(real_db)  # second run (the fixture already ran it once)
    employees, logs = index_keys(real_db.employees), index_keys(real_db.attendance_logs)
    assert employees[(("emp_code", 1),)].get("unique") is True
    assert logs[(("emp_code", 1), ("date", 1))].get("unique") is True
    assert (("date", -1), ("emp_code", 1)) in logs
    assert (("status", 1), ("date", -1), ("emp_code", 1)) in logs
    assert (("emp_code", 1), ("punch_in", -1)) in logs
    assert (("department", 1), ("joined_on", 1)) in employees and (("joined_on", 1),) in employees


def test_indexes_can_be_built_over_an_already_seeded_database(real_db):
    real_db.employees.drop_indexes()
    real_db.attendance_logs.drop_indexes()
    seed_employees(real_db)
    real_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "LEAVE"})
    main.ensure_indexes(real_db)
    assert (("emp_code", 1), ("date", 1)) in index_keys(real_db.attendance_logs)


def test_database_itself_rejects_a_duplicate_day_record(staffed):
    from pymongo.errors import DuplicateKeyError

    staffed.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "LEAVE"})
    with pytest.raises(DuplicateKeyError):
        staffed.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "ABSENT"})


def test_stored_instants_are_bson_dates_in_utc(staffed):
    TestClient(main.app).post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28)})
    raw = staffed.attendance_logs.find_one({"emp_code": "EMP0001"})["punch_in"]
    assert isinstance(raw, datetime) and raw.utcoffset() == timedelta(0)
    assert raw == datetime(2026, 7, 6, 3, 58, tzinfo=main.UTC)  # 09:28 IST == 03:58 UTC, as in DATA_MODEL.md


# ============================ explain plans ================================== #
@pytest.fixture
def big(real_db):
    """~6,000 attendance records so the planner has a reason to prefer indexes."""
    departments = ["Engineering", "Sales", "Support", "Ops", "HR"]
    employees = [
        employee_doc(f"EMP{n:04d}", f"Person {n}", departments[n % 5], "09:30", "18:30", "2026-01-05") for n in range(1, 101)
    ]
    real_db.employees.insert_many(employees)
    start = datetime(2026, 5, 1)
    logs = []
    for n in range(1, 101):
        for offset in range(60):
            day = (start + timedelta(days=offset)).date().isoformat()
            late = (n * offset) % 40
            logs.append(
                {
                    "emp_code": f"EMP{n:04d}", "date": day, "status": ["PRESENT", "WFH", "LEAVE"][(n + offset) % 3],
                    "punch_in": datetime(2026, 5, 1, 4, 0, tzinfo=main.UTC), "punch_out": None, "work_hours": 8.0,
                    "late_minutes": late if late > 10 else 0, "overtime_minutes": 0, "half_day": False, "history": [],
                }
            )  # fmt: skip
    real_db.attendance_logs.insert_many(logs)
    return TestClient(main.app)


EXPLAIN_CASES = [
    ("attendance_list", "", "attendance_logs"),
    ("attendance_list", "emp_code=EMP0005", "attendance_logs"),
    ("attendance_list", "status=WFH", "attendance_logs"),
    ("attendance_list", "date_from=2026-05-10&date_to=2026-05-20", "attendance_logs"),
    ("attendance_list", "emp_code=EMP0005&date_from=2026-05-10&status=PRESENT&page=2&page_size=10", "attendance_logs"),
    ("employee_monthly", "emp_code=EMP0005&month=2026-05", "attendance_logs"),
    ("department_summary", "month=2026-05", "employees"),
    ("department_summary", "month=2026-05&department=Sales", "employees"),
    ("late_leaderboard", "month=2026-05", "attendance_logs"),
    ("late_leaderboard", "month=2026-05&department=Sales&limit=5", "attendance_logs"),
    ("department_trend", "department=Sales&from=2026-05-01&to=2026-06-15", "employees"),
]


@pytest.mark.parametrize("endpoint,query,collection", EXPLAIN_CASES)
def test_explain_uses_an_index_and_never_scans_the_collection(big, endpoint, query, collection):
    response = big.get(f"/admin/explain/{endpoint}?{query}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == endpoint and body["collection"] == collection
    plan = json.dumps(body["explain"])
    assert "IXSCAN" in plan, plan[:2000]
    assert "COLLSCAN" not in plan, plan[:2000]
    assert "executionStats" in plan


def test_explain_attendance_list_sort_is_served_by_the_index(big):
    plan = json.dumps(big.get("/admin/explain/attendance_list").json()["explain"])
    assert '"stage": "SORT"' not in plan  # no in-memory sort at 100k scale
    assert '"nReturned": 20' in plan  # same skip/limit as the real endpoint (page_size default 20)


def test_endpoints_still_return_correct_data_on_the_big_dataset(big):
    page = big.get("/attendance?page_size=100").json()
    assert page["total"] == 6000 and len(page["items"]) == 100
    keys = [(i["date"], i["emp_code"]) for i in page["items"]]
    assert keys == sorted(keys, key=lambda k: (-int(k[0].replace("-", "")), k[1]))
    assert big.get("/analytics/leaderboard/late?month=2026-05&limit=5").status_code == 200
    assert big.get("/analytics/departments/summary?month=2026-05").status_code == 200
    trend = big.get("/analytics/departments/Sales/trend", params={"from": "2026-05-01", "to": "2026-07-31"})
    assert trend.status_code == 200 and len(trend.json()["items"]) == 92
