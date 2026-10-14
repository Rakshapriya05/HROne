"""Request-validation tests (422/503). The `no_db` fixture makes any database access raise,
so these tests also prove that validation happens BEFORE the database is touched."""
import pytest
from pymongo.errors import ServerSelectionTimeoutError

from app import main
from tests.conftest import ist_ms

VALID_EMPLOYEE = {
    "emp_code": "EMP0001",
    "name": "Asha Rao",
    "email": "asha@example.com",
    "department": "Engineering",
    "joined_on": "2026-01-05",
}


def assert_422(response):
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)


# ---- epoch milliseconds --------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        1783312500,  # seconds, not milliseconds
        99_999_999_999,  # just under the minimum
        4_102_444_800_001,  # just over the maximum
        1783312500000.0,  # float
        1783312500000.5,
        "1783312500000",  # string
        True,  # bool is an int subclass in Python; must still be rejected
        -5,
        [1],  # wrong JSON type
    ],
)
@pytest.mark.parametrize("path", ["/attendance/punch-in", "/attendance/punch-out"])
def test_punched_at_must_be_epoch_milliseconds(no_db, path, bad):
    assert_422(no_db.post(path, json={"emp_code": "EMP0001", "punched_at": bad}))


def test_punch_in_rejects_non_presence_status(no_db):
    for status in ("ABSENT", "LEAVE", "BOGUS", "present", 1):
        assert_422(no_db.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status}))


def test_punch_in_requires_emp_code(no_db):
    assert_422(no_db.post("/attendance/punch-in", json={}))
    assert_422(no_db.post("/attendance/punch-in", content="not json", headers={"content-type": "application/json"}))


# ---- employees ------------------------------------------------------------ #
@pytest.mark.parametrize(
    "patch",
    [
        {"emp_code": "emp0001"},
        {"emp_code": "EMP123"},  # too few digits
        {"emp_code": "EMP1234567"},  # too many digits
        {"emp_code": "EMP12٣٤"},  # non-ASCII digits
        {"emp_code": 1},
        {"name": ""},
        {"name": "x" * 101},
        {"email": "not-an-email"},
        {"email": "a@b"},
        {"department": ""},
        {"department": "d" * 51},
        {"shift_start": "9:30"},
        {"shift_start": "24:00"},
        {"shift_end": "18:60"},
        {"shift_start": "09:30", "shift_end": "09:30"},  # must differ
        {"joined_on": "2026-02-30"},  # not a real date
        {"joined_on": "20260105"},
        {"joined_on": "05-01-2026"},
        {"joined_on": 20260105},
    ],
)
def test_create_employee_validation(no_db, patch):
    assert_422(no_db.post("/employees", json={**VALID_EMPLOYEE, **patch}))


@pytest.mark.parametrize("missing", ["emp_code", "name", "email", "department", "joined_on"])
def test_create_employee_required_fields(no_db, missing):
    body = {k: v for k, v in VALID_EMPLOYEE.items() if k != missing}
    assert_422(no_db.post("/employees", json=body))


# ---- pagination and filters ---------------------------------------------- #
@pytest.mark.parametrize("path", ["/employees", "/attendance"])
@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=abc", "page_size=0", "page_size=101", "page_size=x"])
def test_pagination_bounds(no_db, path, query):
    assert_422(no_db.get(f"{path}?{query}"))


@pytest.mark.parametrize(
    "query",
    [
        "date_from=2026-13-01",
        "date_to=2026-02-30",
        "date_from=2026-7-1",
        "status=BOGUS",
        "date_from=2026-07-10&date_to=2026-07-09",  # from > to
    ],
)
def test_attendance_list_filter_validation(no_db, query):
    assert_422(no_db.get(f"/attendance?{query}"))


# ---- PATCH ---------------------------------------------------------------- #
GOOD_PATCH = {"reason": "biometric glitch", "regularized_by": "hr.admin", "status": "WFH"}


@pytest.mark.parametrize(
    "patch",
    [
        {"reason": "abcd"},  # shorter than 5
        {"reason": "r" * 201},
        {"regularized_by": ""},
        {"regularized_by": "x" * 51},
        {"status": "BOGUS"},
        {"status": None},  # null is not "omitted"
        {"punch_in": None},
        {"punch_out": None},
        {"punch_in": 1783312500},
        {"punch_out": 1783312500000.0},
        {"punch_in": "x"},
    ],
)
def test_patch_body_validation(no_db, patch):
    assert_422(no_db.patch("/attendance/EMP0001/2026-07-07", json={**GOOD_PATCH, **patch}))


@pytest.mark.parametrize("missing", ["reason", "regularized_by"])
def test_patch_requires_reason_and_actor(no_db, missing):
    body = {k: v for k, v in GOOD_PATCH.items() if k != missing}
    assert_422(no_db.patch("/attendance/EMP0001/2026-07-07", json=body))


def test_patch_path_date_must_be_a_real_date(no_db):
    assert_422(no_db.patch("/attendance/EMP0001/2026-02-30", json=GOOD_PATCH))
    assert_422(no_db.patch("/attendance/EMP0001/yesterday", json=GOOD_PATCH))


# ---- analytics ------------------------------------------------------------ #
@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-1", "202607", "2026-07-01", "abc", "0000-05"])
@pytest.mark.parametrize(
    "path", ["/analytics/employees/EMP0001/monthly", "/analytics/departments/summary", "/analytics/leaderboard/late"]
)
def test_month_format(no_db, path, month):
    assert_422(no_db.get(f"{path}?month={month}"))


@pytest.mark.parametrize("path", ["/analytics/employees/EMP0001/monthly", "/analytics/departments/summary"])
def test_month_is_required(no_db, path):
    assert_422(no_db.get(path))


@pytest.mark.parametrize("limit", ["0", "51", "-1", "abc"])
def test_leaderboard_limit_bounds(no_db, limit):
    assert_422(no_db.get(f"/analytics/leaderboard/late?month=2026-07&limit={limit}"))


@pytest.mark.parametrize(
    "query",
    [
        "from=2026-07-10&to=2026-07-09",  # to < from
        "from=2026-01-01&to=2026-04-03",  # 93 days
        "from=2026-07-01",  # to missing
        "to=2026-07-01",  # from missing
        "from=2026-02-30&to=2026-03-05",
        "from=2026-7-1&to=2026-07-05",
    ],
)
def test_trend_validation(no_db, query):
    assert_422(no_db.get(f"/analytics/departments/Sales/trend?{query}"))


# ---- explain -------------------------------------------------------------- #
def test_explain_unknown_endpoint(no_db):
    assert_422(no_db.get("/admin/explain/not_a_real_endpoint"))


@pytest.mark.parametrize(
    "endpoint,query",
    [
        ("employee_monthly", ""),
        ("employee_monthly", "emp_code=EMP0001"),
        ("employee_monthly", "month=2026-07"),
        ("department_summary", ""),
        ("late_leaderboard", ""),
        ("department_trend", "department=Sales"),
        ("department_trend", "department=Sales&from=2026-07-01"),
        ("department_trend", "from=2026-07-01&to=2026-07-05"),
        ("department_trend", "department=Sales&from=2026-07-09&to=2026-07-01"),
        ("attendance_list", "date_from=2026-07-10&date_to=2026-07-01"),
        ("attendance_list", "page_size=500"),
    ],
)
def test_explain_missing_or_bad_params(no_db, endpoint, query):
    assert_422(no_db.get(f"/admin/explain/{endpoint}?{query}"))


# ---- health --------------------------------------------------------------- #
def test_health_is_503_when_mongo_is_down(monkeypatch):
    from fastapi.testclient import TestClient

    class DownClient:
        class admin:
            @staticmethod
            def command(*_a, **_k):
                raise ServerSelectionTimeoutError("connection refused: secret-host:27017")

    monkeypatch.setattr(main, "client", DownClient)
    response = TestClient(main.app).get("/health")
    assert response.status_code == 503
    assert "secret-host" not in response.text  # driver details must not leak


def test_unexpected_database_error_is_503_without_details(monkeypatch):
    from fastapi.testclient import TestClient

    class Broken:
        def __getattr__(self, name):
            raise ServerSelectionTimeoutError("mongodb://user:password@host/db failed")

    monkeypatch.setattr(main, "db", Broken())
    response = TestClient(main.app, raise_server_exceptions=False).get("/employees")
    assert response.status_code == 503
    assert "password" not in response.text
