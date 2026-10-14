"""
Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment
      variables win)

Layout of this file (everything lives here, as the assignment requires):
  1. configuration and MongoDB connection
  2. validation types
  3. time / rule helpers (R1-R5, R8)
  4. response serialisers
  5. query and pipeline builders (shared by the endpoints AND the explain endpoint)
  6. endpoints
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    model_validator,
)
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import AutoReconnect, DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

logger = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive
# ones, so a stored instant can never be mistaken for local time.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
ALL_STATUSES = ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_LIMIT = 4.5  # R5
MAX_PUNCH_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92

MIN_EPOCH_MS = 100_000_000_000  # below this the value looks like seconds
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01


def ensure_indexes(database=None) -> None:
    """Create every index the endpoints rely on. Safe to run on every start."""
    database = database if database is not None else db
    specs = [
        ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "uniq_emp_code"}),
        # department filter + R9 headcount (joined_on <= end of period)
        ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined"}),
        ("employees", [("joined_on", ASCENDING)], {"name": "joined"}),
        # one record per employee per day; also the punch-in race guard
        ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uniq_emp_date"}),
        # GET /attendance default sort, plus date-range scans for analytics
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
        # GET /attendance?status=...
        (
            "attendance_logs",
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
            {"name": "status_date_emp"},
        ),
        # punch-out: latest record of an employee at or before a time
        ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    ]
    for collection, keys, options in specs:
        try:
            database[collection].create_index(keys, **options)
        except AutoReconnect:
            # MongoDB is unreachable: stop now instead of waiting out a timeout per index.
            logger.error("MongoDB unreachable at startup; indexes not created (GET /health will say 503)")
            return
        except PyMongoError:
            logger.exception("Could not create index %s on %s", options["name"], collection)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(request: Request, exc: PyMongoError):
    # Log the real error server-side, never leak driver details to the client.
    logger.error("MongoDB error on %s: %r", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Validation types
# --------------------------------------------------------------------------- #
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
EMP_CODE_RE = r"^EMP[0-9]{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _check_date(value: Optional[str]) -> Optional[str]:
    if value is not None:
        try:
            Date.fromisoformat(value)
        except ValueError:
            raise ValueError("must be a real calendar date in YYYY-MM-DD form")
    return value


def _check_month(value: Optional[str]) -> Optional[str]:
    if value is not None:
        try:
            Date(int(value[:4]), int(value[5:7]), 1)
        except ValueError:
            raise ValueError("must be a real month in YYYY-MM form")
    return value


# Strict int: rejects "123", 1.5, 1.0 and True, which plain `int` would coerce.
EpochMs = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
DateStr = Annotated[str, Field(pattern=DATE_RE), AfterValidator(_check_date)]

DatePath = Annotated[str, Path(pattern=DATE_RE), AfterValidator(_check_date)]
DateQuery = Annotated[str, Query(pattern=DATE_RE), AfterValidator(_check_date)]
MonthQuery = Annotated[str, Query(pattern=MONTH_RE), AfterValidator(_check_month)]
OptDateQuery = Annotated[Optional[str], Query(pattern=DATE_RE), AfterValidator(_check_date)]
OptMonthQuery = Annotated[Optional[str], Query(pattern=MONTH_RE), AfterValidator(_check_month)]
PageQuery = Annotated[int, Query(ge=1)]
PageSizeQuery = Annotated[int, Query(ge=1, le=100)]
LimitQuery = Annotated[int, Query(ge=1, le=50)]

StatusT = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatusT = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatusT = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    # Derived fields (work_hours, ...) are not declared, so a client-sent value is ignored.
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        # The contract does not allow null; "omitted" and "null" must not be confused.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null; omit it to leave it unchanged")
        return self


def invalid(loc: tuple, message: str, value: Any = None) -> RequestValidationError:
    """A 422 in FastAPI's standard body shape, for checks that depend on stored data."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": message, "input": value}])


# --------------------------------------------------------------------------- #
# 3. Time and business-rule helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(value: datetime) -> int:
    if value.tzinfo is None:  # defensive; the client is tz_aware so this should not happen
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def ms_or_none(value: Optional[datetime]) -> Optional[int]:
    return None if value is None else dt_to_ms(value)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    """Current time truncated to milliseconds, which is what MongoDB can store."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def hhmm_to_time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_time(shift_end) <= hhmm_to_time(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: the IST date of the punch-in; for an overnight shift, a punch-in earlier than
    shift_end (in IST) belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm_to_time(shift_end):
        day -= timedelta(days=1)
    return day


def shift_window(day: Date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end of the shift that began on `day`, as IST datetimes."""
    start = datetime.combine(day, hhmm_to_time(shift_start), tzinfo=IST)
    end = datetime.combine(day, hhmm_to_time(shift_end), tzinfo=IST)
    if end <= start:  # overnight: the shift ends on the next calendar day
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, day: Date, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes
    since shift start (not since the end of the grace period)."""
    start, _ = shift_window(day, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: Date, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, counted only when at least 30."""
    _, end = shift_window(day, shift_start, shift_end)
    seconds = int((punch_out - end).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def half_up(value: Any, places: int) -> float:
    """R4/R8 rounding. Python's round() and MongoDB's $round are half-to-even, so use
    Decimal for an exact half-up."""
    number = value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))
    return float(number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def derive_fields(
    status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: Date, shift_start: str, shift_end: str
) -> dict:
    """All server-computed fields for a record (DATA_MODEL.md, 'stored derived values')."""
    derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return derived
    derived["late_minutes"] = compute_late_minutes(punch_in, day, shift_start, shift_end)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        derived["work_hours"] = hours
        derived["overtime_minutes"] = compute_overtime(punch_out, day, shift_start, shift_end)
        derived["half_day"] = hours < HALF_DAY_LIMIT
    return derived


def check_punch_span(punch_in: datetime, punch_out: datetime, loc: tuple) -> None:
    if punch_out <= punch_in:
        raise invalid(loc, "punch_out must be after punch_in")
    if punch_out - punch_in > MAX_PUNCH_SPAN:
        raise invalid(loc, "punch_out must be within 24 hours of punch_in")


def month_bounds(month: str) -> tuple[Date, Date]:
    year, mon = int(month[:4]), int(month[5:7])
    return Date(year, mon, 1), Date(year, mon, calendar.monthrange(year, mon)[1])


def count_weekdays(first: Date, last: Date) -> int:
    """Mon-Fri days in [first, last] (R7, no holiday calendar)."""
    if last < first:
        return 0
    total_days = (last - first).days + 1
    full_weeks, extra = divmod(total_days, 7)
    count = full_weeks * 5
    for offset in range(extra):
        if (first + timedelta(days=full_weeks * 7 + offset)).weekday() < 5:
            count += 1
    return count


def page_skip(page: int, page_size: int) -> int:
    return min((page - 1) * page_size, 2**62)  # keep absurd pages inside BSON's int64


# --------------------------------------------------------------------------- #
# 4. Serialisers (BSON -> API shape)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": ms_or_none(doc.get("created_at")),
    }


def _history_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def attendance_out(doc: dict) -> dict:
    """Missing legacy fields default as DATA_MODEL.md says; no _id/id ever leaves the API."""
    history = []
    for entry in doc.get("history") or []:
        history.append(
            {
                "at": dt_to_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": {
                    field: {"from": _history_value(change.get("from")), "to": _history_value(change.get("to"))}
                    for field, change in entry.get("changes", {}).items()
                },
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


# --------------------------------------------------------------------------- #
# 5. Query and pipeline builders
#    The endpoints and /admin/explain both call these, so what is explained is
#    exactly what is executed.
# --------------------------------------------------------------------------- #
def check_date_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from and date_to and date_from > date_to:
        raise invalid(("query", "date_from"), "date_from must not be after date_to", date_from)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


# Expression fragments shared by the analytics pipelines.
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
_IS_WEEKDAY = {
    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]
}
_IS_HALF_DAY = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
_LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def month_totals_accumulators() -> dict:
    """$group accumulators for one employee over one month (R5-R7). Reads the STORED
    derived values; nothing is recomputed from punch times."""
    return {
        # R7: only Mon-Fri records count as present; a half day counts 0.5.
        "present_days": {
            "$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, {"$cond": [_IS_HALF_DAY, 0.5, 1]}, 0]}
        },
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [_LATE_MINUTES, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": _LATE_MINUTES},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # Decimal128 keeps the sum of 2-decimal hours exact, so the final mean can be
        # rounded half-up without floating-point drift.
        "hours_sum": {"$sum": {"$cond": [_HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
        "hours_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None, **month_totals_accumulators()}},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees so that people with zero logs are still counted (R9), then
    left-joins each employee's month of logs and totals them before grouping by department."""
    first, last = month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$group": {"_id": None, **month_totals_accumulators()}},
                ],
                "as": "totals",
            }
        },
        # An employee with no logs has totals == []; keep them (one row per employee).
        {"$unwind": {"path": "$totals", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$totals.present_days"},
                "hours_sum": {"$sum": "$totals.hours_sum"},
                "hours_n": {"$sum": "$totals.hours_n"},
                "late_count": {"$sum": "$totals.late_count"},
                "total_late_minutes": {"$sum": "$totals.total_late_minutes"},
                "leave_count": {"$sum": "$totals.leave_count"},
                "on_duty_count": {"$sum": "$totals.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {
            "$match": {
                "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # Inner join: logs whose employee does not exist disappear here.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        # Filter BEFORE ranking, so ranks are computed inside the department only.
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        # $rank is standard competition ranking: ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit applies to the RANK, not to the row count, so ties at the cutoff are kept.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_day_count(start: Date, end: Date) -> int:
    return (end - start).days + 1


def check_trend_range(start: str, end: str) -> int:
    first, last = Date.fromisoformat(start), Date.fromisoformat(end)
    if last < first:
        raise invalid(("query", "to"), "to must not be before from", end)
    days = trend_day_count(first, last)
    if days > MAX_TREND_DAYS:
        raise invalid(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days", end)
    return days


def department_trend_pipeline(department: str, start: str, days: int) -> list:
    """One row per calendar day, built inside MongoDB.

    The calendar is generated with $range (so a range with no logs at all still yields
    every day), headcount is derived from joined_on, and the 7-day moving average is a
    $setWindowFields. Rates are carried as integers in units of 1/10000 and rounded
    half-up with integer arithmetic, because $round is half-to-even.
    """
    first = Date.fromisoformat(start)
    last = first + timedelta(days=days - 1)
    first_dt = datetime(first.year, first.month, first.day, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joined": {"$push": "$joined_on"}, "codes": {"$push": "$emp_code"}}},
        # Per-day totals for this department's employees inside the range.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$codes"},
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                    {
                        "$group": {
                            "_id": "$date",
                            # present "half units": a full day is 2, a half day is 1
                            "present_units": {
                                "$sum": {"$cond": [_IS_PRESENT, {"$cond": [_IS_HALF_DAY, 1, 2]}, 0]}
                            },
                            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE_MINUTES, 0]}, 1, 0]}},
                        }
                    },
                ],
                "as": "days",
            }
        },
        {
            "$project": {
                "_id": 0,
                "joined": 1,
                "days": 1,
                "rows": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "i",
                        "in": {
                            "date": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {"$dateAdd": {"startDate": first_dt, "unit": "day", "amount": "$$i"}},
                                }
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$rows"},
        {
            "$set": {
                "date": "$rows.date",
                "headcount": {
                    "$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$rows.date"]}}}
                },
                "day": {
                    "$arrayElemAt": [
                        {"$filter": {"input": "$days", "as": "d", "cond": {"$eq": ["$$d._id", "$rows.date"]}}},
                        0,
                    ]
                },
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$rows.date", "format": "%Y-%m-%d"}}},
                        5,
                    ]
                },
            }
        },
        {
            "$set": {
                "present_units": {"$ifNull": ["$day.present_units", 0]},
                "late_count": {"$ifNull": ["$day.late_count", 0]},
            }
        },
        {
            # rate * 10000, rounded half-up: floor(10000*units/(2*headcount) + 1/2)
            "$set": {
                "rate_e4": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": [10000, "$present_units"]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    # $sum ignores the nulls, so only non-null rates are summed ...
                    "window_sum": {"$sum": "$rate_e4", "window": {"documents": [-6, 0]}},
                    # ... and only non-null rates are counted.
                    "window_n": {
                        "$sum": {"$cond": [{"$isNumber": "$rate_e4"}, 1, 0]},
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$present_units", 2]},
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [{"$isNumber": "$rate_e4"}, {"$divide": ["$rate_e4", 10000]}, None]
                },
                # mean of the window, half-up to 4 decimals: floor((2*sum + n) / (2*n)) / 10000
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$window_n", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": [2, "$window_sum"]}, "$window_n"]},
                                            {"$multiply": [2, "$window_n"]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        # The unique index on emp_code decides; there is no check-then-insert race.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQuery = 1, page_size: PageSizeQuery = 20):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip(page_skip(page, page_size)).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ---- attendance ------------------------------------------------------------ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derive_fields(body.status, punched_at, None, day, employee["shift_start"], employee["shift_end"]),
        "history": [],
    }
    try:
        # Unique index (emp_code, date): of N simultaneous requests exactly one insert wins.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    employee = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()

    # Most recent record that started at or before the punch-out (works across midnight).
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", DESCENDING)]
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")
    check_punch_span(record["punch_in"], punched_at, ("body", "punched_at"))

    derived = derive_fields(
        record["status"],
        record["punch_in"],
        punched_at,
        Date.fromisoformat(record["date"]),
        employee["shift_start"],
        employee["shift_end"],
    )
    # The filter only matches while the record is still open, so of two simultaneous
    # punch-outs exactly one update applies and the other gets None.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Already punched out")
    return attendance_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: OptDateQuery = None,
    date_to: OptDateQuery = None,
    status: Optional[StatusT] = None,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
):
    check_date_range(date_from, date_to)
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort(ATTENDANCE_SORT).skip(page_skip(page, page_size)).limit(page_size)
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DatePath, body: RegularizeRequest):
    employee = get_employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="No attendance record for that date")

    shift_start, shift_end = employee["shift_start"], employee["shift_end"]
    new_status = body.status or record["status"]

    if new_status in PRESENCE:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if new_in is None:
            raise invalid(("body", "punch_in"), "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, shift_start, shift_end).isoformat() != date:
            raise invalid(("body", "punch_in"), "punch_in must stay on the record's attendance date", body.punch_in)
        if new_out is not None:
            check_punch_span(new_in, new_out, ("body", "punch_out"))
    else:
        # ABSENT / LEAVE clear both punch times, so supplying one is contradictory.
        if body.punch_in is not None or body.punch_out is not None:
            raise invalid(("body",), f"{new_status} records cannot have punch times")
        new_in = new_out = None

    old_in, old_out = record.get("punch_in"), record.get("punch_out")
    if (new_status, new_in, new_out) == (record["status"], old_in, old_out):
        raise invalid(("body",), "the request changes nothing")

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derive_fields(new_status, new_in, new_out, Date.fromisoformat(date), shift_start, shift_end),
    }
    old_values = {
        "status": record["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day")),
    }
    # Only fields that actually changed are written to the audit entry.
    changes = {
        field: {"from": old_values[field], "to": new_values[field]}
        for field in old_values
        if old_values[field] != new_values[field]
    }
    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record is exactly as we read
    # it (same status, punches and history length). Otherwise someone else got there
    # first and we answer 409 instead of overwriting their work or their history entry.
    history_len = len(record.get("history") or [])
    if history_len:
        history_guard: dict = {"history": {"$size": history_len}}
    else:
        history_guard = {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": old_in,
            "punch_out": old_out,
            **history_guard,
        },
        {"$set": new_values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Record was modified concurrently; retry")
    return attendance_out(updated)


# ---- analytics ------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    employee = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    totals = next(iter(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month))), None) or {}

    # R7: working days run from joined_on (if later than the 1st) to the end of the month.
    counting_from = max(first, Date.fromisoformat(employee["joined_on"]))
    working_days = count_weekdays(counting_from, last)
    present_days = half_up(totals.get("present_days", 0), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(totals.get("leave_count", 0)),
        "late_count": int(totals.get("late_count", 0)),
        "total_late_minutes": int(totals.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes", 0)),
        "attendance_pct": half_up(Decimal(str(present_days)) / working_days * 100, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: Optional[str] = None):
    groups = db.employees.aggregate(department_summary_pipeline(month, department))
    items = []
    for group in groups:
        hours_n = int(group["hours_n"])
        hours_sum = group["hours_sum"]
        hours_sum = hours_sum.to_decimal() if isinstance(hours_sum, Decimal128) else Decimal(str(hours_sum))
        items.append(
            {
                "department": group["_id"],
                "headcount": int(group["headcount"]),
                "present_days": half_up(group["present_days"], 2),
                "avg_work_hours": half_up(hours_sum / hours_n, 2) if hours_n else None,
                "late_count": int(group["late_count"]),
                "total_late_minutes": int(group["total_late_minutes"]),
                "leave_count": int(group["leave_count"]),
                "on_duty_count": int(group["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQuery, limit: LimitQuery = 10, department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department))
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_RE), AfterValidator(_check_date)],
    to: DateQuery,
):
    days = check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail="Unknown department")
    rows = db.employees.aggregate(department_trend_pipeline(department, from_, days))
    items = [
        {
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": row["attendance_rate"],
            "moving_avg_7d": row["moving_avg_7d"],
        }
        for row in rows
    ]
    return {"department": department, "items": items}


# ---- admin ----------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def _require(**params: Any) -> None:
    for name, value in params.items():
        if value is None:
            raise RequestValidationError(
                [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None}]
            )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: OptMonthQuery = None,
    department: Optional[str] = None,
    limit: LimitQuery = 10,
    date_from: OptDateQuery = None,
    date_to: OptDateQuery = None,
    status: Optional[StatusT] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DATE_RE), AfterValidator(_check_date)] = None,
    to: OptDateQuery = None,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
):
    """Raw MongoDB explain (executionStats) of the endpoint's main query, built by the same
    functions the endpoint itself uses."""
    if endpoint == "attendance_list":
        check_date_range(date_from, date_to)
        collection = "attendance_logs"
        command: dict = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": page_skip(page, page_size),
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            _require(department=department, **{"from": from_, "to": to})
            days = check_trend_range(from_, to)
            collection, pipeline = "employees", department_trend_pipeline(department, from_, days)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    # Explain output contains BSON types (dates, Int64, ...); convert to plain JSON.
    plain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": plain}
