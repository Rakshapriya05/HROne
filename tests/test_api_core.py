"""Endpoint behaviour for employees, punch-in/out, listing and regularization.

These use the `database` fixture: they run on mongomock here and ALSO on a real MongoDB when
one is reachable, with identical assertions."""
from datetime import datetime

import pytest

from app import main
from tests.conftest import employee_doc, ist_ms, seed_employees

DAY = lambda h, m, s=0: ist_ms(2026, 7, 6, h, m, s)  # Monday 2026-07-06, IST
NEXT = lambda d, h, m, s=0: ist_ms(2026, 7, d, h, m, s)

ASHA = {
    "emp_code": "EMP0001",
    "name": "Asha Rao",
    "email": "asha@example.com",
    "department": "Engineering",
    "joined_on": "2026-01-05",
}
RECORD_KEYS = {
    "emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
    "late_minutes", "overtime_minutes", "half_day", "history",
}  # fmt: skip


def punch_in(client, code="EMP0001", at=None, **extra):
    body = {"emp_code": code, **extra}
    if at is not None:
        body["punched_at"] = at
    return client.post("/attendance/punch-in", json=body)


def punch_out(client, code="EMP0001", at=None):
    body = {"emp_code": code}
    if at is not None:
        body["punched_at"] = at
    return client.post("/attendance/punch-out", json=body)


@pytest.fixture
def staffed(database):
    seed_employees(database)
    return database


# =========================== employees ====================================== #
def test_create_employee_returns_contract_shape(client):
    response = client.post("/employees", json=ASHA)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30"  # defaults
    assert isinstance(body["created_at"], int) and body["created_at"] > main.MIN_EPOCH_MS
    assert "_id" not in body and "id" not in body


def test_create_employee_ignores_client_supplied_created_at(client):
    body = client.post("/employees", json={**ASHA, "created_at": 1}).json()
    assert body["created_at"] > main.MIN_EPOCH_MS


def test_duplicate_emp_code_is_409(client):
    assert client.post("/employees", json=ASHA).status_code == 201
    second = client.post("/employees", json={**ASHA, "name": "Someone Else"})
    assert second.status_code == 409
    assert isinstance(second.json()["detail"], str)


def test_unique_index_exists_on_emp_code(database):
    info = database.employees.index_information()
    assert any(v.get("unique") and v["key"] == [("emp_code", 1)] for v in info.values())


def test_list_employees_sorted_filtered_and_paginated(client, staffed):
    page1 = client.get("/employees?page_size=3").json()
    assert [e["emp_code"] for e in page1["items"]] == ["EMP0001", "EMP0002", "EMP0003"]
    assert page1["total"] == 8 and page1["page"] == 1 and page1["page_size"] == 3
    page2 = client.get("/employees?page_size=3&page=2").json()
    assert [e["emp_code"] for e in page2["items"]] == ["EMP0004", "EMP0005", "EMP0006"]
    page3 = client.get("/employees?page_size=3&page=3").json()
    assert [e["emp_code"] for e in page3["items"]] == ["EMP0007", "EMP0008"]
    assert client.get("/employees?page_size=3&page=4").json()["items"] == []


def test_list_employees_total_respects_department_filter(client, staffed):
    body = client.get("/employees?department=Sales").json()
    assert body["total"] == 3
    assert [e["emp_code"] for e in body["items"]] == ["EMP0003", "EMP0004", "EMP0008"]
    assert client.get("/employees?department=sales").json()["total"] == 0  # case-sensitive


# =========================== punch-in ======================================= #
def test_punch_in_success_and_response_shape(client, staffed):
    response = punch_in(client, at=DAY(9, 28))
    assert response.status_code == 201
    body = response.json()
    assert set(body) == RECORD_KEYS
    assert body["date"] == "2026-07-06" and body["status"] == "PRESENT"
    assert body["punch_in"] == DAY(9, 28)
    assert body["punch_out"] is None and body["work_hours"] is None
    assert body["late_minutes"] == 0 and body["overtime_minutes"] == 0
    assert body["half_day"] is False and body["history"] == []
    stored = staffed.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["punch_in"], datetime) and stored["punch_in"].utcoffset().total_seconds() == 0


@pytest.mark.parametrize("hh,mm,ss,late", [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (10, 5, 0, 35)])
def test_punch_in_late_rule(client, staffed, hh, mm, ss, late):
    assert punch_in(client, at=DAY(hh, mm, ss)).json()["late_minutes"] == late


def test_punch_in_truncates_to_whole_seconds(client, staffed):
    body = punch_in(client, at=ist_ms(2026, 7, 6, 9, 40, 0, millis=900)).json()
    assert body["punch_in"] == DAY(9, 40, 0)
    assert body["late_minutes"] == 0  # 09:40:00.900 counts as 09:40:00


def test_punch_in_unknown_employee_is_404(client, staffed):
    assert punch_in(client, code="EMP9999", at=DAY(9, 0)).status_code == 404


def test_punch_in_twice_same_date_is_409(client, staffed):
    assert punch_in(client, at=DAY(9, 0)).status_code == 201
    again = punch_in(client, at=DAY(11, 0))
    assert again.status_code == 409
    assert staffed.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_punch_in_on_a_date_with_an_absent_record_is_409(client, staffed):
    staffed.attendance_logs.insert_one(
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "ABSENT", "punch_in": None, "punch_out": None}
    )
    assert punch_in(client, at=DAY(9, 0)).status_code == 409


@pytest.mark.parametrize("status", ["WFH", "ON_DUTY"])
def test_punch_in_accepts_presence_statuses(client, staffed, status):
    assert punch_in(client, at=DAY(9, 0), status=status).json()["status"] == status


def test_punch_in_date_is_ist_date(client, staffed):
    # 01:30 IST on the 7th is still the 6th in UTC; the record belongs to the 7th.
    assert punch_in(client, at=NEXT(7, 1, 30)).json()["date"] == "2026-07-07"


def test_punch_in_overnight_shift_after_midnight_belongs_to_previous_day(client, staffed):
    body = punch_in(client, code="EMP0005", at=NEXT(7, 1, 0)).json()  # 01:00 IST on the 7th
    assert body["date"] == "2026-07-06"
    assert body["late_minutes"] == 180  # 22:00 -> 01:00
    assert punch_in(client, code="EMP0005", at=NEXT(6, 21, 55)).status_code == 409  # same shift-day


def test_punch_in_defaults_to_now(client, staffed):
    before = main.dt_to_ms(main.now_seconds())
    body = punch_in(client).json()
    assert before <= body["punch_in"] <= before + 5000 and body["punch_in"] % 1000 == 0


def test_punch_in_ignores_client_supplied_derived_fields(client, staffed):
    body = punch_in(client, at=DAY(9, 0), late_minutes=99, work_hours=5, half_day=True, history=[{"x": 1}]).json()
    assert body["late_minutes"] == 0 and body["work_hours"] is None
    assert body["half_day"] is False and body["history"] == []


# =========================== punch-out ====================================== #
def test_punch_out_success(client, staffed):
    punch_in(client, at=DAY(9, 28))
    response = punch_out(client, at=DAY(18, 35))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == RECORD_KEYS
    assert body["punch_out"] == DAY(18, 35)
    assert body["work_hours"] == 9.12 and body["overtime_minutes"] == 0 and body["half_day"] is False


def test_punch_out_overtime_and_half_day_rules(client, staffed):
    punch_in(client, at=DAY(10, 5))
    body = punch_out(client, at=DAY(19, 10)).json()
    assert body["overtime_minutes"] == 40 and body["late_minutes"] == 35 and body["work_hours"] == 9.08
    # half day
    punch_in(client, code="EMP0003", at=DAY(9, 30))
    half = punch_out(client, code="EMP0003", at=DAY(13, 0)).json()
    assert half["work_hours"] == 3.5 and half["half_day"] is True


def test_punch_out_boundary_rounding(client, staffed):
    punch_in(client, at=DAY(9, 30, 0))
    body = punch_out(client, at=DAY(13, 59, 42)).json()  # exactly 4.495 h
    assert body["work_hours"] == 4.5 and body["half_day"] is False


def test_punch_out_across_midnight_for_overnight_shift(client, staffed):
    punch_in(client, code="EMP0005", at=NEXT(6, 21, 55))
    body = punch_out(client, code="EMP0005", at=NEXT(7, 6, 40)).json()
    assert body["date"] == "2026-07-06"
    assert body["work_hours"] == 8.75 and body["overtime_minutes"] == 40


def test_punch_out_unknown_employee_and_no_punch_in_are_404(client, staffed):
    assert punch_out(client, code="EMP9999", at=DAY(18, 0)).status_code == 404
    assert punch_out(client, at=DAY(18, 0)).status_code == 404  # never punched in
    punch_in(client, at=DAY(9, 0))
    assert punch_out(client, at=DAY(8, 0)).status_code == 404  # before every punch-in


def test_second_punch_out_is_409(client, staffed):
    punch_in(client, at=DAY(9, 0))
    assert punch_out(client, at=DAY(18, 0)).status_code == 200
    assert punch_out(client, at=DAY(19, 0)).status_code == 409


def test_punch_out_not_after_punch_in_is_422(client, staffed):
    punch_in(client, at=DAY(9, 0))
    assert punch_out(client, at=DAY(9, 0)).status_code == 422  # equal
    assert staffed.attendance_logs.find_one({"emp_code": "EMP0001"})["punch_out"] is None


def test_punch_out_more_than_24h_after_is_422_and_exactly_24h_is_ok(client, staffed):
    punch_in(client, at=DAY(9, 0))
    assert punch_out(client, at=NEXT(7, 9, 0, 1)).status_code == 422
    assert punch_out(client, at=NEXT(7, 9, 0, 0)).status_code == 200


def test_punch_out_closes_the_most_recent_record(client, staffed):
    punch_in(client, at=DAY(9, 0))
    punch_out(client, at=DAY(18, 0))
    punch_in(client, at=NEXT(7, 9, 0))
    body = punch_out(client, at=NEXT(7, 18, 0)).json()
    assert body["date"] == "2026-07-07"
    assert staffed.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})["punch_out"] is not None


def test_punch_out_skips_records_without_punch_in(client, staffed):
    staffed.attendance_logs.insert_one(
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "LEAVE", "punch_in": None, "punch_out": None}
    )
    assert punch_out(client, at=DAY(18, 0)).status_code == 404


# =========================== GET /attendance ================================ #
@pytest.fixture
def logs(staffed):
    def doc(code, day, status="PRESENT", **extra):
        base = {
            "emp_code": code, "date": day, "status": status, "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
        }  # fmt: skip
        return {**base, **extra}

    staffed.attendance_logs.insert_many(
        [
            doc("EMP0002", "2026-07-06"),
            doc("EMP0001", "2026-07-06"),
            doc("EMP0001", "2026-07-07", "WFH"),
            doc("EMP0003", "2026-07-07", "LEAVE"),
            doc("EMP0001", "2026-07-08", "ABSENT"),
            # legacy record: no history, no half_day, no late/overtime at all
            {"emp_code": "EMP0004", "date": "2026-07-08", "status": "PRESENT",
             "punch_in": datetime(2026, 7, 8, 4, 0, tzinfo=main.UTC)},
        ]
    )  # fmt: skip
    return staffed


def listing(client, query=""):
    response = client.get(f"/attendance?{query}")
    assert response.status_code == 200
    return response.json()


def test_list_attendance_sorted_date_desc_then_emp_code_asc(client, logs):
    body = listing(client)
    assert [(i["date"], i["emp_code"]) for i in body["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0004"),
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0003"),
        ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002"),
    ]  # fmt: skip
    assert body["total"] == 6


def test_list_attendance_filters_and_total(client, logs):
    assert listing(client, "emp_code=EMP0001")["total"] == 3
    assert listing(client, "status=LEAVE")["total"] == 1
    body = listing(client, "date_from=2026-07-07&date_to=2026-07-07")
    assert body["total"] == 2 and {i["emp_code"] for i in body["items"]} == {"EMP0001", "EMP0003"}
    assert listing(client, "date_from=2026-07-08")["total"] == 2  # inclusive
    assert listing(client, "date_to=2026-07-06")["total"] == 2  # inclusive
    assert listing(client, "emp_code=EMP0001&status=WFH")["total"] == 1
    assert listing(client, "emp_code=NOBODY")["items"] == []


def test_list_attendance_pagination_total_ignores_page(client, logs):
    page = listing(client, "page_size=4&page=2")
    assert page["total"] == 6 and len(page["items"]) == 2 and page["page"] == 2 and page["page_size"] == 4
    assert listing(client, "page_size=4&page=3")["items"] == []


def test_list_attendance_fills_legacy_defaults_and_hides_ids(client, logs):
    legacy = next(i for i in listing(client)["items"] if i["emp_code"] == "EMP0004")
    assert set(legacy) == RECORD_KEYS
    assert legacy["history"] == [] and legacy["half_day"] is False
    assert legacy["late_minutes"] == 0 and legacy["overtime_minutes"] == 0
    assert legacy["punch_in"] == main.dt_to_ms(datetime(2026, 7, 8, 4, 0, tzinfo=main.UTC))
    assert legacy["punch_out"] is None and legacy["work_hours"] is None


# =========================== PATCH (regularize) ============================= #
def patch(client, code="EMP0001", day="2026-07-07", **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return client.patch(f"/attendance/{code}/{day}", json=body)


@pytest.fixture
def worked(client, staffed):
    """EMP0001 on Tue 2026-07-07: in 10:05 (35 min late), out 19:10 (40 min overtime)."""
    punch_in(client, at=NEXT(7, 10, 5))
    punch_out(client, at=NEXT(7, 19, 10))
    return staffed


def test_patch_recomputes_and_appends_history(client, worked):
    response = patch(client, punch_in=NEXT(7, 9, 28))
    assert response.status_code == 200
    body = response.json()
    assert body["punch_in"] == NEXT(7, 9, 28)
    assert body["late_minutes"] == 0 and body["work_hours"] == 9.7 and body["overtime_minutes"] == 40
    assert len(body["history"]) == 1
    entry = body["history"][0]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch"
    assert main.MIN_EPOCH_MS < entry["at"]
    # only fields that really changed, with API-shaped (epoch ms) values
    assert entry["changes"] == {
        "punch_in": {"from": NEXT(7, 10, 5), "to": NEXT(7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 9.08, "to": 9.7},
    }
    stored = worked.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
    assert isinstance(stored["history"][0]["at"], datetime)  # BSON dates in the database
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["to"], datetime)
    assert stored["punch_in"] == main.ms_to_dt(NEXT(7, 9, 28))


def test_patch_history_is_append_only(client, worked):
    patch(client, punch_in=NEXT(7, 9, 28), reason="first fix")
    second = patch(client, punch_out=NEXT(7, 18, 40), reason="second fix", regularized_by="hr.two").json()
    assert [h["reason"] for h in second["history"]] == ["first fix", "second fix"]
    assert second["history"][0]["changes"]["punch_in"]["to"] == NEXT(7, 9, 28)  # first entry untouched
    assert second["history"][1]["by"] == "hr.two"
    assert second["history"][1]["changes"]["punch_out"] == {"from": NEXT(7, 19, 10), "to": NEXT(7, 18, 40)}
    assert second["history"][1]["changes"]["overtime_minutes"] == {"from": 40, "to": 0}  # 10 min is under the 30-min minimum


def test_patch_status_only_change(client, worked):
    body = patch(client, status="WFH").json()
    assert body["status"] == "WFH"
    assert list(body["history"][0]["changes"]) == ["status"]


def test_patch_to_leave_clears_punches_and_derived_fields(client, worked):
    body = patch(client, status="LEAVE").json()
    assert body["status"] == "LEAVE" and body["punch_in"] is None and body["punch_out"] is None
    assert body["work_hours"] is None and body["late_minutes"] == 0 and body["overtime_minutes"] == 0
    assert set(body["history"][0]["changes"]) == {
        "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"
    }
    assert body["history"][0]["changes"]["punch_in"] == {"from": NEXT(7, 10, 5), "to": None}


def test_patch_leave_to_presence_with_punch_in(client, staffed):
    staffed.attendance_logs.insert_one(
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "LEAVE", "punch_in": None, "punch_out": None}
    )  # legacy-ish: no derived fields, no history
    body = patch(client, status="PRESENT", punch_in=NEXT(7, 10, 0)).json()
    assert body["status"] == "PRESENT" and body["late_minutes"] == 30 and body["work_hours"] is None
    assert len(body["history"]) == 1


def test_patch_on_legacy_record_without_history_field(client, staffed):
    staffed.attendance_logs.insert_one(
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT",
         "punch_in": main.ms_to_dt(NEXT(7, 10, 5)), "punch_out": main.ms_to_dt(NEXT(7, 19, 10)),
         "work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40}
    )  # fmt: skip
    body = patch(client, status="WFH").json()
    assert body["half_day"] is False and len(body["history"]) == 1


def test_patch_422_cases(client, worked):
    assert patch(client, status="LEAVE", punch_in=NEXT(7, 9, 0)).status_code == 422  # clears punches
    assert patch(client, status="ABSENT", punch_out=NEXT(7, 18, 0)).status_code == 422
    assert patch(client).status_code == 422  # nothing changed
    assert patch(client, status="PRESENT").status_code == 422  # same status, nothing else
    assert patch(client, punch_in=NEXT(7, 10, 5), punch_out=NEXT(7, 19, 10)).status_code == 422  # same values
    assert patch(client, punch_out=NEXT(7, 10, 5)).status_code == 422  # not after punch_in
    assert patch(client, punch_out=NEXT(8, 10, 6)).status_code == 422  # > 24h
    assert patch(client, punch_in=NEXT(8, 9, 0)).status_code == 422  # leaves the attendance date
    assert patch(client, punch_in=NEXT(7, 20, 0)).status_code == 422  # after the existing punch_out
    # nothing above may have written an audit entry
    assert worked.attendance_logs.find_one({"emp_code": "EMP0001"})["history"] == []


def test_patch_presence_status_needs_a_punch_in(client, staffed):
    staffed.attendance_logs.insert_one(
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "ABSENT", "punch_in": None, "punch_out": None}
    )
    assert patch(client, status="PRESENT").status_code == 422
    assert patch(client, status="WFH", punch_out=NEXT(7, 18, 0)).status_code == 422


def test_patch_absent_record_with_punch_in_but_no_status_is_422(client, staffed):
    staffed.attendance_logs.insert_one(
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "ABSENT", "punch_in": None, "punch_out": None}
    )
    assert patch(client, punch_in=NEXT(7, 9, 0)).status_code == 422


def test_patch_404s(client, worked):
    assert patch(client, code="EMP9999", status="WFH").status_code == 404  # unknown employee
    assert patch(client, day="2026-07-20", status="WFH").status_code == 404  # no record that day
    assert patch(client, code="EMP0002", status="WFH").status_code == 404  # employee without record


def test_patch_ignores_derived_fields_in_body(client, worked):
    body = patch(client, status="WFH", work_hours=1, late_minutes=500, half_day=True, history=[]).json()
    assert body["work_hours"] == 9.08 and body["late_minutes"] == 35 and body["half_day"] is False
    assert len(body["history"]) == 1


def test_patch_overnight_punch_in_must_stay_on_attendance_date(client, staffed):
    punch_in(client, code="EMP0005", at=NEXT(6, 21, 55))  # record date 2026-07-06
    ok = patch(client, code="EMP0005", day="2026-07-06", punch_in=NEXT(7, 0, 30))  # 00:30 next day is still the 6th's shift
    assert ok.status_code == 200 and ok.json()["late_minutes"] == 150
    bad = patch(client, code="EMP0005", day="2026-07-06", punch_in=NEXT(7, 7, 0))  # after shift end -> the 7th
    assert bad.status_code == 422


def test_patch_truncates_to_whole_seconds(client, worked):
    body = patch(client, punch_in=ist_ms(2026, 7, 7, 9, 28, 0, millis=999)).json()
    assert body["punch_in"] == NEXT(7, 9, 28)


def test_stale_patch_is_rejected_instead_of_overwriting(client, worked, monkeypatch):
    """Simulates a concurrent edit: another writer appends history between our read and write.
    (The real-threads version is in test_mongo_integration.py.)"""
    real_find_one = worked.attendance_logs.find_one
    fired = {"done": False}

    class Racing:
        def __getattr__(self, name):
            return getattr(worked.attendance_logs, name)

        def find_one(self, *args, **kwargs):
            doc = real_find_one(*args, **kwargs)
            if not fired["done"]:
                fired["done"] = True
                worked.attendance_logs.update_one(
                    {"_id": doc["_id"]},
                    {"$push": {"history": {"at": datetime.now(main.UTC), "by": "other", "reason": "other edit", "changes": {}}}},
                )
            return doc

    class RacingDb:
        employees = worked.employees
        attendance_logs = Racing()

    monkeypatch.setattr(main, "db", RacingDb())
    assert patch(client, status="WFH").status_code == 409
    stored = real_find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
    assert stored["status"] == "PRESENT" and [h["by"] for h in stored["history"]] == ["other"]


def test_patch_rejected_if_a_punch_out_lands_between_read_and_write(client, staffed, monkeypatch):
    """A punch-out is not a correction and writes no history, so the history-length guard alone
    would miss it. The status/punch guards must catch it, or the PATCH would clobber the punch-out."""
    punch_in(client, at=NEXT(7, 10, 5))  # open record
    real_find_one = staffed.attendance_logs.find_one
    fired = {"done": False}

    class Racing:
        def __getattr__(self, name):
            return getattr(staffed.attendance_logs, name)

        def find_one(self, *args, **kwargs):
            doc = real_find_one(*args, **kwargs)
            if not fired["done"]:
                fired["done"] = True
                staffed.attendance_logs.update_one(
                    {"_id": doc["_id"]}, {"$set": {"punch_out": main.ms_to_dt(NEXT(7, 18, 0)), "work_hours": 7.92}}
                )
            return doc

    class RacingDb:
        employees = staffed.employees
        attendance_logs = Racing()

    monkeypatch.setattr(main, "db", RacingDb())
    assert patch(client, punch_in=NEXT(7, 9, 0)).status_code == 409
    stored = real_find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
    assert stored["punch_out"] == main.ms_to_dt(NEXT(7, 18, 0)) and stored["history"] == []
