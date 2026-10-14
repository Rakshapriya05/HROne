"""A small interpreter for the MongoDB aggregation operators used by app/main.py.

PURPOSE: the development sandbox had no MongoDB server, and mongomock lacks $lookup-with-pipeline,
$setWindowFields, $dateFromString and more. This module executes the REAL pipeline objects that
main.py builds so their logic can be checked against an independent oracle.

LIMITS: it encodes my understanding of MongoDB semantics. It proves the pipelines are logically
consistent; it does NOT prove they behave identically on a real server. The same tests also run
against real MongoDB (see conftest.py) and that is the authoritative check.
"""
import math
from datetime import datetime, timedelta
from decimal import Decimal

MISSING = object()


# ----------------------------------------------------------------- values -- #
def get_path(doc, path):
    current = doc
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def is_number(value):
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def truthy(value):
    return value is not MISSING and value is not None and value is not False and not (is_number(value) and value == 0)


def type_rank(value):
    if value is MISSING:
        return 0
    if value is None:
        return 1
    if is_number(value):
        return 2
    if isinstance(value, str):
        return 3
    if isinstance(value, bool):  # (bool is checked before the generic fallback)
        return 5
    if isinstance(value, datetime):
        return 6
    return 4


def sort_key(value):
    rank = type_rank(value)
    return (rank, value if rank in (2, 3, 5, 6) else 0)


def compare(a, b):
    ka, kb = sort_key(a), sort_key(b)
    return (ka > kb) - (ka < kb)


# ------------------------------------------------------------ expressions -- #
def ev(expr, doc, variables):
    if isinstance(expr, str):
        if expr.startswith("$$"):
            name, _, rest = expr[2:].partition(".")
            value = variables[name]
            return get_path(value, rest) if rest else value
        if expr.startswith("$"):
            return get_path(doc, expr[1:])
        return expr
    if isinstance(expr, list):
        return [ev(item, doc, variables) for item in expr]
    if isinstance(expr, dict):
        if len(expr) == 1 and next(iter(expr)).startswith("$"):
            op, arg = next(iter(expr.items()))
            return OPERATORS[op](arg, doc, variables)
        return {key: ev(value, doc, variables) for key, value in expr.items()}
    return expr


def _args(arg, doc, variables):
    return [ev(item, doc, variables) for item in arg]


def op_cond(arg, doc, variables):
    condition, then, otherwise = (arg["if"], arg["then"], arg["else"]) if isinstance(arg, dict) else arg
    return ev(then if truthy(ev(condition, doc, variables)) else otherwise, doc, variables)


def op_ifnull(arg, doc, variables):
    value, replacement = _args(arg, doc, variables)
    return replacement if value is None or value is MISSING else value


def op_filter(arg, doc, variables):
    name = arg.get("as", "this")
    return [
        item
        for item in ev(arg["input"], doc, variables)
        if truthy(ev(arg["cond"], doc, {**variables, name: item}))
    ]


def op_map(arg, doc, variables):
    name = arg.get("as", "this")
    return [ev(arg["in"], doc, {**variables, name: item}) for item in ev(arg["input"], doc, variables)]


def op_array_elem_at(arg, doc, variables):
    array, index = _args(arg, doc, variables)
    return array[index] if -len(array) <= index < len(array) else MISSING


OPERATORS = {
    "$cond": op_cond,
    "$ifNull": op_ifnull,
    "$filter": op_filter,
    "$map": op_map,
    "$arrayElemAt": op_array_elem_at,
    "$and": lambda a, d, v: all(truthy(x) for x in _args(a, d, v)),
    "$in": lambda a, d, v: (lambda x, arr: x in arr)(*_args(a, d, v)),
    "$eq": lambda a, d, v: (lambda x, y: compare(x, y) == 0)(*_args(a, d, v)),
    "$gt": lambda a, d, v: (lambda x, y: compare(x, y) > 0)(*_args(a, d, v)),
    "$lte": lambda a, d, v: (lambda x, y: compare(x, y) <= 0)(*_args(a, d, v)),
    "$isNumber": lambda a, d, v: is_number(ev(a, d, v)),
    "$toDecimal": lambda a, d, v: Decimal(str(ev(a, d, v))),
    "$floor": lambda a, d, v: math.floor(ev(a, d, v)),
    "$divide": lambda a, d, v: (lambda x, y: float(x) / float(y))(*_args(a, d, v)),
    "$add": lambda a, d, v: sum(_args(a, d, v)),
    "$multiply": lambda a, d, v: math.prod(_args(a, d, v)),
    "$size": lambda a, d, v: len(ev(a, d, v)),
    "$range": lambda a, d, v: list(range(*_args(a, d, v))),
    "$isoDayOfWeek": lambda a, d, v: ev(a, d, v).isoweekday(),
    "$dateFromString": lambda a, d, v: datetime.strptime(ev(a["dateString"], d, v), a["format"]),
    "$dateToString": lambda a, d, v: ev(a["date"], d, v).strftime(a["format"]),
    "$dateAdd": lambda a, d, v: ev(a["startDate"], d, v) + timedelta(days=ev(a["amount"], d, v)),
}


# ---------------------------------------------------------------- queries -- #
def matches(doc, query, variables):
    for key, condition in query.items():
        if key == "$expr":
            if not truthy(ev(condition, doc, variables)):
                return False
        elif isinstance(condition, dict) and any(k.startswith("$") for k in condition):
            value = get_path(doc, key)
            for op, operand in condition.items():
                if type_rank(value) != type_rank(operand):  # query comparisons are type-bracketed
                    return False
                order = compare(value, operand)
                if not {"$gt": order > 0, "$gte": order >= 0, "$lt": order < 0, "$lte": order <= 0}[op]:
                    return False
        else:
            value = get_path(doc, key)
            if not (value == condition or (condition is None and value is MISSING)):
                return False
    return True


# ----------------------------------------------------------------- stages -- #
def _sum(values):
    total = 0
    for value in values:
        if is_number(value):
            if isinstance(total, Decimal) or isinstance(value, Decimal):
                total = Decimal(str(total)) + Decimal(str(value))
            else:
                total += value
    return total


def stage_group(spec, docs, db, variables):
    groups = {}
    for doc in docs:
        key = ev(spec["_id"], doc, variables)
        groups.setdefault(key if key is not MISSING else None, []).append(doc)
    out = []
    for key, members in groups.items():
        row = {"_id": key}
        for name, accumulator in spec.items():
            if name == "_id":
                continue
            (op, expr), = accumulator.items()
            values = [ev(expr, m, variables) for m in members]
            row[name] = _sum(values) if op == "$sum" else [x for x in values if x is not MISSING]
        out.append(row)
    return out


def stage_lookup(spec, docs, db, variables):
    out = []
    for doc in docs:
        inner_vars = dict(variables)
        for name, expr in spec.get("let", {}).items():
            inner_vars[name] = ev(expr, doc, variables)
        foreign = db[spec["from"]]
        if "localField" in spec:
            local = get_path(doc, spec["localField"])
            local = None if local is MISSING else local
            foreign = [f for f in foreign if get_path(f, spec["foreignField"]) == local]
        joined = run(spec.get("pipeline", []), list(foreign), db, inner_vars)
        out.append({**doc, spec["as"]: joined})
    return out


def stage_unwind(spec, docs, db, variables):
    path, preserve = (spec, False) if isinstance(spec, str) else (spec["path"], spec.get("preserveNullAndEmptyArrays", False))
    name = path[1:]
    out = []
    for doc in docs:
        array = doc.get(name, MISSING)
        if isinstance(array, list) and array:
            out.extend({**doc, name: item} for item in array)
        elif preserve:
            out.append({k: v for k, v in doc.items() if k != name})
    return out


def stage_set(spec, docs, db, variables):
    out = []
    for doc in docs:
        new = dict(doc)
        for name, expr in spec.items():
            value = ev(expr, doc, variables)  # every expression sees the ORIGINAL document
            if value is MISSING:
                new.pop(name, None)
            else:
                new[name] = value
        out.append(new)
    return out


def stage_project(spec, docs, db, variables):
    out = []
    for doc in docs:
        row = {}
        if spec.get("_id", 1) not in (0, False) and "_id" in doc:
            row["_id"] = doc["_id"]
        for name, value in spec.items():
            if name == "_id":
                continue
            if value is True or (isinstance(value, int) and value == 1):
                if name in doc:
                    row[name] = doc[name]
            else:
                computed = ev(value, doc, variables)
                if computed is not MISSING:
                    row[name] = computed
        out.append(row)
    return out


def stage_sort(spec, docs, db, variables):
    docs = list(docs)
    for key, direction in reversed(list(spec.items())):
        docs.sort(key=lambda d: sort_key(get_path(d, key)), reverse=direction == -1)
    return docs


def stage_set_window_fields(spec, docs, db, variables):
    ordered = stage_sort(spec["sortBy"], docs, db, variables)
    sort_fields = list(spec["sortBy"])
    same = lambda a, b: all(compare(get_path(a, f), get_path(b, f)) == 0 for f in sort_fields)
    out = []
    for i, doc in enumerate(ordered):
        row = dict(doc)
        for name, definition in spec["output"].items():
            if "$rank" in definition:
                row[name] = next(j for j, other in enumerate(ordered) if same(other, doc)) + 1
            else:
                (op, expr), = ((k, v) for k, v in definition.items() if k != "window")
                assert op == "$sum", "interpreter only implements $sum windows"
                low, high = definition["window"]["documents"]
                window = ordered[max(0, i + low) : min(len(ordered) - 1, i + high) + 1]
                row[name] = _sum(ev(expr, w, variables) for w in window)
        out.append(row)
    return out


STAGES = {
    "$match": lambda s, docs, db, v: [d for d in docs if matches(d, s, v)],
    "$group": stage_group,
    "$lookup": stage_lookup,
    "$unwind": stage_unwind,
    "$set": stage_set,
    "$addFields": stage_set,
    "$project": stage_project,
    "$sort": stage_sort,
    "$setWindowFields": stage_set_window_fields,
}


def run(pipeline, docs, db, variables=None):
    docs = list(docs)
    for stage in pipeline:
        (name, spec), = stage.items()
        docs = STAGES[name](spec, docs, db, variables or {})
    return docs


# -------------------------------------------------- database test double -- #
class InterpretedCollection:
    """Delegates everything to a mongomock collection except aggregate(), which is interpreted."""

    def __init__(self, backing, store):
        self._backing, self._store = backing, store

    def aggregate(self, pipeline):
        snapshot = {name: [dict(d) for d in coll.find({})] for name, coll in self._store.items()}
        return iter(run(pipeline, snapshot[self._backing.name], snapshot))

    def __getattr__(self, name):
        return getattr(self._backing, name)


class InterpretedDb:
    def __init__(self, mock_db):
        store = {"employees": mock_db.employees, "attendance_logs": mock_db.attendance_logs}
        self.employees = InterpretedCollection(mock_db.employees, store)
        self.attendance_logs = InterpretedCollection(mock_db.attendance_logs, store)
        self.name = mock_db.name
