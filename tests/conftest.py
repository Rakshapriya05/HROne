"""Shared fixtures.

Three kinds of tests live in this folder:
  * pure tests          - no database at all (test_rules.py, test_validation.py)
  * `database` tests    - run against mongomock ALWAYS, and against a real MongoDB too
                          when one is reachable (same test code, two backends)
  * `real_db` tests     - need a real MongoDB (concurrency, aggregation pipelines,
                          explain plans); skipped with a clear reason otherwise

Set TEST_MONGO_URI (default: MONGO_URI or mongodb://localhost:27017) to point at MongoDB.
Each run uses a throw-away database named attendance_test_<random> and drops it afterwards.
"""
import os
import uuid
from datetime import datetime

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app import main

TEST_URI = os.getenv("TEST_MONGO_URI") or os.getenv("MONGO_URI") or "mongodb://localhost:27017"


def ist_ms(year, month, day, hour=0, minute=0, second=0, millis=0) -> int:
    """Epoch milliseconds for an IST wall-clock time."""
    moment = datetime(year, month, day, hour, minute, second, tzinfo=main.IST)
    return int(moment.timestamp()) * 1000 + millis


_MONGO_REACHABLE = {}


def _real_client():
    """A client for the real MongoDB, or None. The reachability check runs once per session
    (a missing server otherwise costs 1.5 s per test)."""
    if _MONGO_REACHABLE.get("ok") is False:
        return None
    try:
        real = MongoClient(TEST_URI, tz_aware=True, serverSelectionTimeoutMS=1500)
        real.admin.command("ping")
        _MONGO_REACHABLE["ok"] = True
        return real
    except PyMongoError:
        _MONGO_REACHABLE["ok"] = False
        return None


@pytest.fixture
def real_db(monkeypatch):
    real = _real_client()
    if real is None:
        pytest.skip(f"real MongoDB not reachable at TEST_MONGO_URI ({TEST_URI})")
    name = f"attendance_test_{uuid.uuid4().hex[:8]}"
    database = real[name]
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "client", real)
    main.ensure_indexes(database)
    yield database
    real.drop_database(name)
    real.close()


@pytest.fixture(params=["mongomock", "mongodb"])
def database(request, monkeypatch):
    if request.param == "mongodb":
        real = _real_client()
        if real is None:
            pytest.skip(f"real MongoDB not reachable at TEST_MONGO_URI ({TEST_URI})")
        name = f"attendance_test_{uuid.uuid4().hex[:8]}"
        database = real[name]
    else:
        real = None
        database = mongomock.MongoClient(tz_aware=True)["attendance_test"]
    monkeypatch.setattr(main, "db", database)
    main.ensure_indexes(database)
    yield database
    if real is not None:
        real.drop_database(database.name)
        real.close()


@pytest.fixture
def client(database):
    return TestClient(main.app)


@pytest.fixture
def no_db(monkeypatch):
    """Any accidental database access fails loudly: used by pure validation tests."""
    monkeypatch.setattr(main, "db", None)
    return TestClient(main.app)


EMPLOYEES = [
    # emp_code, name, department, shift_start, shift_end, joined_on
    ("EMP0001", "Asha Rao", "Engineering", "09:30", "18:30", "2026-01-05"),
    ("EMP0002", "Vikram Shah", "Engineering", "09:30", "18:30", "2026-07-13"),
    ("EMP0003", "Neha Iyer", "Sales", "09:30", "18:30", "2026-02-01"),
    ("EMP0004", "Rohit Das", "Sales", "09:30", "18:30", "2026-02-01"),
    ("EMP0005", "Meera Nair", "Support", "22:00", "06:00", "2026-03-10"),
    ("EMP0006", "Karan Bedi", "Support", "09:30", "18:30", "2026-03-10"),
    ("EMP0007", "Future Hire", "Ops", "09:30", "18:30", "2026-08-01"),
    ("EMP0008", "Tina Paul", "Sales", "09:30", "18:30", "2026-02-01"),
]


def employee_doc(code, name, dept, start, end, joined):
    return {
        "emp_code": code,
        "name": name,
        "email": f"{code.lower()}@example.com",
        "department": dept,
        "shift_start": start,
        "shift_end": end,
        "joined_on": joined,
        "created_at": datetime(2026, 1, 1, tzinfo=main.UTC),
    }


def seed_employees(database, rows=EMPLOYEES):
    database.employees.insert_many([employee_doc(*row) for row in rows])


@pytest.fixture(params=["interpreter", "mongodb"])
def analytics_client(request, monkeypatch):
    """Analytics pipelines need operators mongomock lacks. This fixture runs them on
    (a) tests/mini_mongo.py, a reference interpreter that works anywhere, and
    (b) a real MongoDB when reachable. Only (b) proves real-server behaviour."""
    from tests.mini_mongo import InterpretedDb

    if request.param == "interpreter":
        mock_db = mongomock.MongoClient(tz_aware=True)["attendance_test"]
        monkeypatch.setattr(main, "db", InterpretedDb(mock_db))
        yield TestClient(main.app), mock_db
        return
    real = _real_client()
    if real is None:
        pytest.skip(f"real MongoDB not reachable at TEST_MONGO_URI ({TEST_URI})")
    name = f"attendance_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(main, "db", real[name])
    monkeypatch.setattr(main, "client", real)
    main.ensure_indexes(real[name])
    yield TestClient(main.app), real[name]
    real.drop_database(name)
    real.close()
