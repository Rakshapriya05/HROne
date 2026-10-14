"""Awkward analytics dataset (July 2026; 2026-07-01 is a Wednesday) plus an independent
pure-Python oracle. The oracle deliberately shares NO code with app/main.py: it uses plain
loops and Decimal so it can catch mistakes in the MongoDB pipelines."""
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

UTC = timezone.utc
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


def log(code, day, status="PRESENT", wh=None, late=0, ot=0, half=False, legacy=False):
    y, m, d = map(int, day.split("-"))
    has_punch = status in PRESENT_STATUSES
    doc = {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": datetime(y, m, d, 4, 0, tzinfo=UTC) if has_punch else None,
        "punch_out": datetime(y, m, d, 13, 0, tzinfo=UTC) if has_punch and wh is not None else None,
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": ot,
        "half_day": half,
        "history": [],
    }
    if legacy:  # older records omit these two fields entirely
        del doc["half_day"], doc["history"]
    return doc


LOGS = [
    # EMP0001 Engineering: weekday, late+overtime, WFH half day, on duty, leave, a Saturday
    log("EMP0001", "2026-07-06", wh=9.0),
    log("EMP0001", "2026-07-07", wh=9.08, late=35, ot=40),
    log("EMP0001", "2026-07-08", "WFH", wh=3.5, half=True),
    log("EMP0001", "2026-07-09", "ON_DUTY", wh=8.0, late=12),
    log("EMP0001", "2026-07-10", "LEAVE"),
    log("EMP0001", "2026-07-11", wh=5.0, late=20, ot=35),
    log("EMP0001", "2026-06-30", wh=8.0, late=50),  # previous month: must be excluded
    log("EMP0001", "2026-08-01", wh=8.0, late=60),  # next month: must be excluded
    # EMP0002 Engineering, joined 2026-07-13; one legacy record
    log("EMP0002", "2026-07-14", wh=8.98, legacy=True),
    log("EMP0002", "2026-07-15", wh=8.0, late=11),
    # Sales: EMP0003 / EMP0004 tie on 70 late minutes
    log("EMP0003", "2026-07-06", wh=9.08, late=35, ot=40),
    log("EMP0003", "2026-07-07", wh=8.0, late=35),
    log("EMP0003", "2026-07-08", "LEAVE"),
    log("EMP0003", "2026-07-09", wh=None),  # open record: present, no work_hours
    log("EMP0004", "2026-07-06", wh=8.0, late=70),
    log("EMP0004", "2026-07-07", "WFH", wh=3.5, half=True),
    log("EMP0004", "2026-07-08", "ABSENT"),
    log("EMP0008", "2026-07-06", wh=7.5, late=20),
    log("EMP0008", "2026-07-04", "ON_DUTY", wh=6.0),  # Saturday
    # Support (EMP0005 overnight shift; EMP0006 has no logs at all)
    log("EMP0005", "2026-07-06", wh=8.75, late=15, ot=40),
    log("EMP0005", "2026-07-12", wh=4.0, half=True),  # Sunday half day
    # orphan: employee does not exist
    log("EMP0099", "2026-07-06", wh=8.0, late=500),
]


def _d(text):
    return date.fromisoformat(text)


def half_up(value, places):
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_range(month):
    first = _d(month + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def in_month(entry, month):
    first, last = month_range(month)
    return first <= _d(entry["date"]) <= last


def day_value(entry):
    return Decimal("0.5") if entry.get("half_day") else Decimal(1)


def weekdays(first, last):
    count, day = 0, first
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


def oracle_monthly(employees, logs, code, month):
    emp = next(e for e in employees if e["emp_code"] == code)
    first, last = month_range(month)
    mine = [x for x in logs if x["emp_code"] == code and in_month(x, month)]
    working = weekdays(max(first, _d(emp["joined_on"])), last)
    present = sum(
        (day_value(x) for x in mine if x["status"] in PRESENT_STATUSES and _d(x["date"]).weekday() < 5), Decimal(0)
    )
    return {
        "emp_code": code,
        "month": month,
        "working_days": working,
        "present_days": half_up(present, 2),
        "leave_days": sum(x["status"] == "LEAVE" for x in mine),
        "late_count": sum(x.get("late_minutes", 0) > 0 for x in mine),
        "total_late_minutes": sum(x.get("late_minutes", 0) for x in mine),
        "total_overtime_minutes": sum(x.get("overtime_minutes", 0) for x in mine),
        "attendance_pct": half_up(present / working * 100, 2) if working else None,
    }


def oracle_summary(employees, logs, month, department=None):
    _, last = month_range(month)
    by_dept = {}
    for emp in employees:
        if _d(emp["joined_on"]) <= last and (department is None or emp["department"] == department):
            by_dept.setdefault(emp["department"], []).append(emp["emp_code"])
    items = []
    for dept in sorted(by_dept):
        codes = set(by_dept[dept])
        mine = [x for x in logs if x["emp_code"] in codes and in_month(x, month)]
        present = sum(
            (day_value(x) for x in mine if x["status"] in PRESENT_STATUSES and _d(x["date"]).weekday() < 5),
            Decimal(0),
        )
        hours = [Decimal(str(x["work_hours"])) for x in mine if x["status"] in PRESENT_STATUSES and x["work_hours"] is not None]
        items.append(
            {
                "department": dept,
                "headcount": len(codes),
                "present_days": half_up(present, 2),
                "avg_work_hours": half_up(sum(hours) / len(hours), 2) if hours else None,
                "late_count": sum(x.get("late_minutes", 0) > 0 for x in mine),
                "total_late_minutes": sum(x.get("late_minutes", 0) for x in mine),
                "leave_count": sum(x["status"] == "LEAVE" for x in mine),
                "on_duty_count": sum(x["status"] == "ON_DUTY" for x in mine),
            }
        )
    return {"month": month, "items": items}


def oracle_leaderboard(employees, logs, month, limit=10, department=None):
    known = {e["emp_code"]: e for e in employees}
    totals = {}
    for x in logs:
        if x["emp_code"] in known and in_month(x, month) and x.get("late_minutes", 0) > 0:
            if department is None or known[x["emp_code"]]["department"] == department:
                t = totals.setdefault(x["emp_code"], [0, 0])
                t[0] += x["late_minutes"]
                t[1] += 1
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    rows = []
    for code, (total, count) in ordered:
        rank = 1 + sum(1 for _, (other, _c) in ordered if other > total)  # competition ranking
        rows.append(
            {
                "rank": rank,
                "emp_code": code,
                "name": known[code]["name"],
                "department": known[code]["department"],
                "total_late_minutes": total,
                "late_count": count,
            }
        )
    return {"month": month, "items": [r for r in rows if r["rank"] <= limit]}


def oracle_trend(employees, logs, department, start, end):
    codes = {e["emp_code"] for e in employees if e["department"] == department}
    rows, day = [], _d(start)
    while day <= _d(end):
        text = day.isoformat()
        headcount = sum(1 for e in employees if e["department"] == department and _d(e["joined_on"]) <= day)
        todays = [x for x in logs if x["emp_code"] in codes and x["date"] == text]
        present = sum((day_value(x) for x in todays if x["status"] in PRESENT_STATUSES), Decimal(0))
        working = day.weekday() < 5
        rate = None
        if working and headcount > 0:
            rate = Decimal(str(half_up(present / headcount, 4)))
        rows.append(
            {
                "date": text,
                "is_working_day": working,
                "headcount": headcount,
                "present_count": float(present),
                "late_count": sum(x.get("late_minutes", 0) > 0 for x in todays),
                "attendance_rate": None if rate is None else float(rate),
                "_rate": rate,
            }
        )
        day += timedelta(days=1)
    for i, row in enumerate(rows):
        window = [r["_rate"] for r in rows[max(0, i - 6) : i + 1] if r["_rate"] is not None]
        row["moving_avg_7d"] = half_up(sum(window) / len(window), 4) if window else None
    for row in rows:
        del row["_rate"]
    return {"department": department, "items": rows}
