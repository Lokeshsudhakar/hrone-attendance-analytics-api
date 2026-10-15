"""
Employee Attendance & Analytics API (HROne assignment, contract v2.0).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real
      environment variables always win over the file).

Everything the assignment asks for lives in this one file:
  * CRUD + punch in/out with the R1-R5 business rules
  * manual corrections (PATCH) with an append-only history / audit trail
  * analytics implemented as MongoDB aggregation pipelines (R6-R9)
  * GET /admin/explain/{endpoint} running a real MongoDB explain
  * idempotent index creation at startup
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

load_dotenv()  # does not override variables that are already set

logger = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "hrone_attendance_db")

client: MongoClient = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    tz_aware=False,  # PyMongo returns naive UTC datetimes; we normalise ourselves
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# R1: instants are truncated to whole seconds before any calculation.
SECOND_MS = 1000
MIN_EPOCH_MS = 100_000_000_000  # values below look like seconds -> 422
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_SECONDS = 30 * 60  # R3
MAX_PUNCH_SECONDS = 24 * 3600  # punch-out window
HALF_DAY_HOURS = Decimal("4.50")  # R5

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
EMPCODE_RE = r"^EMP\d{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

# Analytics read the stored derived values (never recompute from punches).
GROUP_MONTHLY = {
    "_id": None,
    "present_days": {
        "$sum": {
            "$cond": [
                {"$and": [
                    {"$lte": [{"$isoDayOfWeek": "$_d"}, 5]},  # R7: Mon-Fri only
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},  # R6
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},  # R5
                0,
            ]
        }
    },
    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
}

# Department summary per-document contributions (after $unwind of the lookup).
DEPT_DOC_FIELDS = {
    "_id": "$_id",
    "headcount": {"$first": "$headcount"},  # R9: everybody who joined by month end
    "present_days": {
        "$sum": {
            "$cond": [
                {"$and": [
                    {"$in": ["$_st", list(PRESENCE_STATUSES)]},
                    {"$lte": ["$_dow", 5]},  # R7 weekday filter on present days only
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0,
            ]
        }
    },
    # work_hours is a 2-decimal value: sum exact integer "cents" so the mean can
    # be rounded half-up exactly afterwards (R8).
    "work_cents": {
        "$sum": {
            "$cond": [
                {"$and": [
                    {"$in": ["$_st", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                {"$round": [{"$multiply": [{"$ifNull": ["$logs.work_hours", 0]}, 100]}, 0]},
                0,
            ]
        }
    },
    "work_n": {
        "$sum": {
            "$cond": [
                {"$and": [
                    {"$in": ["$_st", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                1,
                0,
            ]
        }
    },
    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
    "leave_count": {"$sum": {"$cond": [{"$eq": ["$_st", "LEAVE"]}, 1, 0]}},
    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_st", "ON_DUTY"]}, 1, 0]}},
}


# --------------------------------------------------------------------------- #
# Configuration / startup
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Create every index we rely on. Idempotent, never destructive."""
    indexes = [
        # (collection, keys, unique)
        ("employees", [("emp_code", ASCENDING)], True),  # unique emp_code -> 409 on races
        ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], False),
        ("employees", [("joined_on", ASCENDING)], False),  # headcount without dept filter
        # unique natural key of a record; makes concurrent punch-ins safe
        ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], True),
        # GET /attendance filter + sort (date desc, emp_code asc) and month ranges
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], False),
    ]
    for name, keys, unique in indexes:
        try:
            db[name].create_index(keys, unique=unique, name="_".join(k for k, _ in keys))
        except ServerSelectionTimeoutError as exc:  # DB unreachable: stop trying, stay up
            logger.warning("MongoDB unreachable while creating indexes: %s", exc)
            break
        except PyMongoError as exc:  # keep serving even if one index fails
            logger.warning("could not create index %s.%s: %s", name, keys, exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Small helpers: validation, time, rounding
# --------------------------------------------------------------------------- #
def _err(status: int, message: str, loc: tuple[Any, ...] = ("body",)) -> HTTPException:
    """422s carry FastAPI's default validation-error body (detail = array)."""
    return HTTPException(status, [{"loc": list(loc), "msg": message, "type": "value_error"}])


def _check_epoch_ms(value: Any) -> int:
    """Epoch milliseconds must be integers in range; seconds/floats/strings -> 422."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer number of milliseconds since 1970-01-01T00:00:00Z")
    if not (MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
        raise ValueError("epoch milliseconds out of range (100000000000..4102444800000)")
    return value


def _check_date_str(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(DATE_RE, value):
        raise ValueError("expected a calendar date YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a real calendar date") from None
    return value


def _check_month(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(MONTH_RE, value):
        raise ValueError("expected a calendar month YYYY-MM")
    return value


EpochMs = Annotated[int, BeforeValidator(_check_epoch_ms)]
DateStr = Annotated[str, BeforeValidator(_check_date_str)]
MonthStr = Annotated[str, BeforeValidator(_check_month)]


def truncate_ms(ms: int) -> int:
    """R1: instants are truncated to whole seconds before any calculation."""
    return ms - (ms % SECOND_MS)


def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)  # PyMongo returns naive UTC datetimes
    else:
        dt = dt.astimezone(UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def ist_now_ms() -> int:
    return truncate_ms(int(datetime.now(UTC).timestamp() * 1000))


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    start = date(year, mon, 1)
    end = date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift runs past midnight."""
    return shift_end <= shift_start


def shift_start_instant(record_date: str, shift_start: str) -> datetime:
    day = date.fromisoformat(record_date)
    hour, minute = map(int, shift_start.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def shift_end_instant(record_date: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(record_date)
    if is_overnight(shift_start, shift_end):
        day += timedelta(days=1)  # overnight shift ends the next calendar day
    hour, minute = map(int, shift_end.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def attendance_date(punch_ms: int, shift_start: str, shift_end: str) -> str:
    """R1: the record's date is the IST calendar date of punch_in.

    Overnight shifts: an IST time earlier than shift_end belongs to the shift
    that started the previous day.
    """
    local = ms_to_dt(punch_ms).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_ms: int, record_date: str, shift_start: str) -> int:
    """R2: strictly more than 10:00 after shift_start -> floor(minutes since start)."""
    start = shift_start_instant(record_date, shift_start)
    elapsed = (ms_to_dt(punch_ms) - start).total_seconds()
    if elapsed <= GRACE_SECONDS:
        return 0
    return int(elapsed // 60)


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half-up."""
    seconds = (punch_out_ms - punch_in_ms) // SECOND_MS
    value = Decimal(seconds) / Decimal(3600)
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out_ms: int, record_date: str, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes) after shift_end, only when >= 30 minutes."""
    end = shift_end_instant(record_date, shift_start, shift_end)
    elapsed = (ms_to_dt(punch_out_ms) - end).total_seconds()
    if elapsed < OVERTIME_MIN_SECONDS:
        return 0
    return int(elapsed // 60)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: after punch-out, half day when the *rounded* work hours < 4.50."""
    if work_hours is None:
        return False
    return Decimal(str(work_hours)) < HALF_DAY_HOURS


def derive_fields(
    status: str,
    punch_in_ms: Optional[int],
    punch_out_ms: Optional[int],
    record_date: str,
    shift_start: str,
    shift_end: str,
) -> dict[str, Any]:
    """Compute the four derived fields (R2-R5) for a final state of a record."""
    if status not in PRESENCE_STATUSES or punch_in_ms is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in_ms, record_date, shift_start)
    if punch_out_ms is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work = compute_work_hours(punch_in_ms, punch_out_ms)
    return {
        "work_hours": work,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out_ms, record_date, shift_start, shift_end),
        "half_day": compute_half_day(work),
    }


def round_half_up(value: Any, places: int) -> Optional[float]:
    """R8: reported numbers are rounded half-up (not banker's rounding)."""
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Request / response models (the contract's schemas)
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=EMPCODE_RE)]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=EMAIL_RE)]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=TIME_RE)] = "09:30"
    shift_end: Annotated[str, Field(pattern=TIME_RE)] = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: EpochMs  # server-set, epoch milliseconds


class PageMeta(BaseModel):
    total: int
    page: int
    page_size: int


class EmployeePage(PageMeta):
    items: list[EmployeeOut]


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    """Only these fields can be changed; derived fields are read-only and ignored."""

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


class HistoryEntry(BaseModel):
    at: EpochMs
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    punch_in: Optional[EpochMs]
    punch_out: Optional[EpochMs]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]


class AttendancePage(PageMeta):
    items: list[AttendanceRecordOut]


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: list[TrendItem]


# --------------------------------------------------------------------------- #
# Serialisation: MongoDB documents -> API responses (epoch ms, no _id)
# --------------------------------------------------------------------------- #
def _change_to_api(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def employee_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]) if isinstance(doc.get("created_at"), datetime) else doc.get("created_at"),
    }


def record_out(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert an attendance document to the AttendanceRecord schema.

    `_id` and any legacy gap (missing history / half_day) are handled here:
    the internal id never appears in a response.
    """
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for field, delta in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": _change_to_api(delta.get("from")),
                "to": _change_to_api(delta.get("to")),
            }
        history.append({
            "at": dt_to_ms(entry["at"]) if isinstance(entry.get("at"), datetime) else entry.get("at"),
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": changes,
        })
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": dt_to_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def find_employee(emp_code: str) -> Optional[dict[str, Any]]:
    return db.employees.find_one({"emp_code": emp_code})


def find_employee_by_department(department: str) -> Optional[dict[str, Any]]:
    return db.employees.find_one({"department": department}, {"_id": 1})


def require_employee(emp_code: str) -> dict[str, Any]:
    emp = find_employee(emp_code)
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    """200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, f"MongoDB is not reachable: {exc.__class__.__name__}") from exc
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, response_model=EmployeeOut)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    doc = body.model_dump()
    now = ms_to_dt(ist_now_ms())
    doc["created_at"] = now  # BSON date; returned as epoch ms
    try:
        # the unique index on emp_code is the source of truth for duplicates
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return employee_out(doc)


@app.get("/employees", response_model=EmployeePage)
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department  # exact, case-sensitive
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch in / punch out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecordOut)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise _err(422, "punched_at must be an integer epoch-milliseconds value", ("body", "punched_at"))
    emp = require_employee(body.emp_code)

    punched_at = body.punched_at if body.punched_at is not None else ist_now_ms()
    punched_at = truncate_ms(punched_at)  # R1
    day = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])

    # Fast, friendly duplicate check; the unique index below is what makes the
    # concurrent case safe (two simultaneous punch-ins -> one 201, rest 409).
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": day}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ms_to_dt(punched_at),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, day, emp["shift_start"]),  # R2
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:  # lost the race -> exactly one 201
        raise HTTPException(409, "already punched in for this date") from exc
    doc.pop("_id", None)
    return record_out(doc)


@app.post("/attendance/punch-out", status_code=200, response_model=AttendanceRecordOut)
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise _err(422, "punched_at must be an integer epoch-milliseconds value", ("body", "punched_at"))
    emp = require_employee(body.emp_code)

    punched_at = body.punched_at if body.punched_at is not None else ist_now_ms()
    punched_at = truncate_ms(punched_at)
    ts = ms_to_dt(punched_at)

    # Most recent record whose punch_in is at or before punched_at; works across
    # midnight because we order by the record's date, not by the punch-out day.
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("date", DESCENDING)],
    )
    if log is None:
        raise HTTPException(404, "no punch-in record found for this employee")

    punch_in_dt = log["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)
    delta = (ts - punch_in_dt).total_seconds()
    if delta <= 0:
        raise _err(422, "punched_at must be after punch_in", ("body", "punched_at"))
    if delta > MAX_PUNCH_SECONDS:
        raise _err(422, "punched_at must not be more than 24 hours after punch_in", ("body", "punched_at"))

    record_date = log["date"]
    derived = derive_fields(
        log["status"], dt_to_ms(punch_in_dt), punched_at, record_date, emp["shift_start"], emp["shift_end"]
    )
    # Condition on punch_out: null -> concurrent punch-outs: exactly one 200.
    result = db.attendance_logs.update_one(
        {"_id": log["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
    )
    if result.modified_count == 0:
        raise HTTPException(409, "already punched out for this date")

    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Attendance: list & manual correction
# --------------------------------------------------------------------------- #
@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise _err(422, "date_from must be on or before date_to", ("query", "date_from"))
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    # Index-backed sort + pagination: no collection scan, no in-memory sort.
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200, response_model=AttendanceRecordOut)
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeRequest) -> dict[str, Any]:
    emp = require_employee(emp_code)
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if log is None:
        raise HTTPException(404, "attendance record not found for this date")

    # Explicit nulls are validation errors (only omitted fields mean "unchanged").
    for field in ("status", "punch_in", "punch_out"):
        if field in body.model_fields_set and getattr(body, field) is None:
            raise _err(422, f"{field} must not be null", ("body", field))

    old_status = log["status"]
    old_in = dt_to_ms(log["punch_in"]) if isinstance(log.get("punch_in"), datetime) else None
    old_out = dt_to_ms(log["punch_out"]) if isinstance(log.get("punch_out"), datetime) else None
    old_derived = {
        "work_hours": log.get("work_hours"),
        "late_minutes": int(log.get("late_minutes") or 0),
        "overtime_minutes": int(log.get("overtime_minutes") or 0),
        "half_day": bool(log.get("half_day", False)),
    }

    # --- compute the final state of the record -----------------------------
    new_status = body.status if body.status is not None else old_status
    if body.punch_in is not None or body.punch_out is not None:
        if new_status in ("ABSENT", "LEAVE"):
            # ABSENT/LEAVE carry no punches; supplying one alongside is 422
            raise _err(422, "ABSENT/LEAVE records must not carry punch times", ("body", "punch_in"))
        new_in: Optional[int] = body.punch_in if body.punch_in is not None else old_in
        new_out: Optional[int] = body.punch_out if body.punch_out is not None else old_out
        if new_in is None and new_out is not None:
            raise _err(422, "punch_out requires a punch_in", ("body", "punch_out"))
    else:
        new_in = old_in
        new_out = old_out
    if new_status in PRESENCE_STATUSES and new_in is None:
        raise _err(422, "presence statuses require a punch_in", ("body", "punch_in"))

    if new_in is not None:
        new_in = truncate_ms(new_in)
        # R1: the punch-in must stay on the record's attendance date.
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise _err(422, "punch_in must fall on the record's attendance date (R1)", ("body", "punch_in"))
    if new_out is not None:
        new_out = truncate_ms(new_out)
        if new_in is None or new_out <= new_in:
            raise _err(422, "punch_out must be after punch_in", ("body", "punch_out"))
        if (new_out - new_in) > MAX_PUNCH_SECONDS * SECOND_MS:
            raise _err(422, "punch_out must be within 24 hours of punch_in", ("body", "punch_out"))

    if new_status not in PRESENCE_STATUSES:
        new_in = None
        new_out = None  # ABSENT / LEAVE always clear both punch times

    new_derived = derive_fields(new_status, new_in, new_out, date, emp["shift_start"], emp["shift_end"])

    # --- what actually changed (history records real changes only) ---------
    changes: dict[str, dict[str, Any]] = {}

    def _record(field: str, old: Any, new: Any) -> None:
        if old != new:
            changes[field] = {"from": old, "to": new}

    _record("status", old_status, new_status)
    _record("punch_in", old_in, new_in)
    _record("punch_out", old_out, new_out)
    for field in ("work_hours", "late_minutes", "overtime_minutes", "half_day"):
        _record(field, old_derived[field], new_derived[field])

    if not changes:
        raise _err(422, "the request does not change anything", ("body",))

    entry = {
        "at": ms_to_dt(ist_now_ms()),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,  # BSON datetimes inside; epoch ms at the API edge
    }
    if "punch_in" in changes:
        for key in ("from", "to"):
            if changes["punch_in"][key] is not None:
                changes["punch_in"][key] = ms_to_dt(changes["punch_in"][key])
    if "punch_out" in changes:
        for key in ("from", "to"):
            if changes["punch_out"][key] is not None:
                changes["punch_out"][key] = ms_to_dt(changes["punch_out"][key])

    # Legacy documents may not carry `history` at all: normalise first so the
    # optimistic-concurrency condition below can use its length.
    if "history" not in log or log["history"] is None:
        db.attendance_logs.update_one(
            {"_id": log["_id"]}, {"$set": {"history": []}}, upsert=False
        )
    seen = len(log.get("history") or [])

    # Optimistic append: match on the history length we read so a concurrent
    # correction cannot silently lose this one (loser gets 409, never a merge).
    result = db.attendance_logs.update_one(
        {"_id": log["_id"], "history": {"$size": seen}},
        {
            "$set": {
                "status": new_status,
                "punch_in": ms_to_dt(new_in) if new_in is not None else None,
                "punch_out": ms_to_dt(new_out) if new_out is not None else None,
                **new_derived,
            },
            "$push": {"history": entry},
        },
    )
    if result.matched_count == 0:
        raise HTTPException(409, "the record was modified by another request, please retry")

    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics pipeline builders (shared with /admin/explain)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$set": {"_d": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": GROUP_MONTHLY},
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}  # R9 headcount cutoff
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$codes"},
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                ],
                "as": "logs",
            }
        },
        # preserve departments whose employees have zero logs in the month
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "_st": {"$ifNull": ["$logs.status", "NONE"]},
                "_dow": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}
                    }
                },
            }
        },
        {"$group": DEPT_DOC_FIELDS},
        {"$sort": {"_id": 1}},  # department ascending
    ]


def build_late_leaderboard_pipeline(month: str, department: Optional[str], limit: int) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},  # only employees with a total > 0
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # logs of unknown employees are ignored
        *([{"$match": {"emp.department": department}}] if department is not None else []),
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        # standard competition ranking: ties share a rank, the next is skipped
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING},
                "output": {
                    "_totals": {"$push": "$total_late_minutes",
                                "window": {"documents": ["unbounded", 0]}}
                },
            }
        },
        {
            "$set": {
                "rank": {
                    "$add": [
                        1,
                        {
                            "$size": {
                                "$filter": {
                                    "input": "$_totals",
                                    "as": "t",
                                    "cond": {"$gt": ["$$t", "$total_late_minutes"]},
                                }
                            }
                        },
                    ]
                }
            }
        },
        {"$unset": "_totals"},
        # limit applies AFTER ranking: everything with rank <= limit stays in
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"rank": ASCENDING, "emp_code": ASCENDING}},
    ]


def build_department_trend_pipeline(department: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
    codes = [d["emp_code"] for d in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    return [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}, "emp_code": {"$in": codes}}},
        {"$set": {"_d": {"$dateFromString": {"dateString": "$date"}}}},
        {
            "$group": {
                "_id": "$_d",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},  # R6, incl. weekends
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$set": {"_d": "$_id"}},  # $densify needs a real field, not the group key
        # gap filling in the database: one row per calendar day, weekends included
        {
            "$densify": {
                "field": "_d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    # $densify's upper bound is exclusive: end at the next midnight
                    "bounds": [ms_to_dt(_date_ms(date_from)), ms_to_dt(_date_ms(date_to) + 86_400_000)],
                },
            }
        },
        {"$sort": {"_d": ASCENDING}},
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_d"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # headcount per day: department employees whose joined_on <= that day (R9)
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [{"$match": {"department": department}}, {"$project": {"joined_on": 1}}],
                "as": "_emps",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                }
            }
        },
        {"$project": {"_id": 0, "_d": 0, "_emps": 0}},
    ]


def _date_ms(day: str) -> int:
    """Midnight of a calendar date as UTC ms (matches $dateFromString output)."""
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def weekday(day: str) -> bool:
    return date.fromisoformat(day).weekday() < 5  # R7: Mon-Fri


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(emp_code: str, month: MonthStr) -> dict[str, Any]:
    emp = require_employee(emp_code)
    start, end = month_bounds(month)

    rows = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    row = rows[0] if rows else {}

    # Working days are a calendar computation (no document scan): Mon-Fri from
    # max(month start, joined_on) to the last day of the month.
    first = date.fromisoformat(start)
    joined = date.fromisoformat(emp["joined_on"])
    last = date.fromisoformat(end)
    cursor_day, working_days = max(first, joined), 0
    while cursor_day <= last:
        if cursor_day.weekday() < 5:
            working_days += 1
        cursor_day += timedelta(days=1)

    present_days = float(row.get("present_days") or 0)
    if working_days > 0:
        pct = Decimal(str(present_days)) / Decimal(working_days) * 100
        attendance_pct = float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days") or 0),
        "late_count": int(row.get("late_count") or 0),
        "total_late_minutes": int(row.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes") or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(month: MonthStr, department: Optional[str] = None) -> dict[str, Any]:
    start, end = month_bounds(month)
    rows = db.employees.aggregate(build_department_summary_pipeline(month, department))

    items: list[dict[str, Any]] = []
    for row in rows:
        headcount = int(row.get("headcount") or 0)
        if headcount == 0:  # R9: a department with nobody by month end is omitted
            continue
        work_n = int(row.get("work_n") or 0)
        if work_n > 0:
            # exact mean of the stored 2-decimal values -> round half-up (R8)
            mean = (Decimal(int(row.get("work_cents") or 0)) / Decimal(100 * work_n)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            avg: Optional[float] = float(mean)
        else:
            avg = None
        items.append({
            "department": row["_id"],
            "headcount": headcount,
            "present_days": float(row.get("present_days") or 0),
            "avg_work_hours": avg,
            "late_count": int(row.get("late_count") or 0),
            "total_late_minutes": int(row.get("total_late_minutes") or 0),
            "leave_count": int(row.get("leave_count") or 0),
            "on_duty_count": int(row.get("on_duty_count") or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(
    month: MonthStr,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
) -> dict[str, Any]:
    rows = list(db.attendance_logs.aggregate(build_late_leaderboard_pipeline(month, department, limit)))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(
    department: str,
    from_: Annotated[DateStr, Query(alias="from")],
    to: DateStr,
) -> dict[str, Any]:
    if to < from_:
        raise _err(422, "to must be on or after from", ("query", "to"))
    if (date.fromisoformat(to) - date.fromisoformat(from_)).days + 1 > 92:
        raise _err(422, "the trend range must not exceed 92 days", ("query", "from"))
    if find_employee_by_department(department) is None:
        raise HTTPException(404, "department not found")

    # One aggregation: match -> group -> $densify (gap filling) -> headcount.
    rows = list(db.attendance_logs.aggregate(build_department_trend_pipeline(department, from_, to)))

    # Final arithmetic over at most 92 aggregate rows (R8 exact half-up):
    # attendance_rate and the 7-day moving average of the reported rates.
    items: list[dict[str, Any]] = []
    window: list[Optional[Decimal]] = []
    for row in rows:
        day = row["date"]
        working = weekday(day)
        headcount = int(row.get("headcount") or 0)
        if working and headcount > 0:
            rate = (Decimal(str(row.get("present_count") or 0)) / Decimal(headcount)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            rate_value: Optional[Decimal] = rate
        else:
            rate_value = None  # non-working day or empty department that day

        window.append(rate_value)
        if len(window) > 7:
            window.pop(0)
        seen = [w for w in window if w is not None]  # rows before `from` never enter
        if seen:
            moving = (sum(seen) / Decimal(len(seen))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            moving_value: Optional[float] = float(moving)
        else:
            moving_value = None

        items.append({
            "date": day,
            "is_working_day": working,
            "headcount": headcount,
            "present_count": float(row.get("present_count") or 0),
            "late_count": int(row.get("late_count") or 0),
            "attendance_rate": float(rate_value) if rate_value is not None else None,
            "moving_avg_7d": moving_value,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
EXPLAINABLE = ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Run MongoDB explain (executionStats) for an endpoint's main query."""
    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard") and month is None:
        raise _err(422, "month is required for this endpoint", ("query", "month"))
    if endpoint == "employee_monthly" and emp_code is None:
        raise _err(422, "emp_code is required for this endpoint", ("query", "emp_code"))
    if endpoint == "department_trend":
        if department is None or from_ is None or to is None:
            raise _err(422, "department, from and to are required for this endpoint", ("query", "department"))
        if to < from_:
            raise _err(422, "to must be on or after from", ("query", "to"))
        if (date.fromisoformat(to) - date.fromisoformat(from_)).days + 1 > 92:
            raise _err(422, "the trend range must not exceed 92 days", ("query", "from"))
    if endpoint == "attendance_list" and date_from is not None and date_to is not None and date_from > date_to:
        raise _err(422, "date_from must be on or before date_to", ("query", "date_from"))

    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None:
                query["date"]["$gte"] = date_from
            if date_to is not None:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},  # raw command: sort is a document
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        command = {"aggregate": "attendance_logs", "pipeline": build_employee_monthly_pipeline(emp_code, month), "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        command = {"aggregate": "employees", "pipeline": build_department_summary_pipeline(month, department), "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        command = {
            "aggregate": "attendance_logs",
            "pipeline": build_late_leaderboard_pipeline(month, department, limit),
            "cursor": {},
        }
        collection = "attendance_logs"
    else:  # department_trend
        command = {
            "aggregate": "attendance_logs",
            "pipeline": build_department_trend_pipeline(department, from_, to),
            "cursor": {},
        }
        collection = "attendance_logs"

    try:
        raw = db.command({"explain": command, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(500, f"explain failed: {exc.__class__.__name__}") from exc
    return {"endpoint": endpoint, "collection": collection, "explain": raw}


# --------------------------------------------------------------------------- #
# Dashboard (EXTRA, not part of the contract): one HTML page that reads from
# the public API. It holds no business logic and never touches MongoDB - it
# just fetches the JSON endpoints above and draws them.
# --------------------------------------------------------------------------- #
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attendance &amp; Analytics - API Console</title>
<style>
  :root { --bg:#f7f7f8; --card:#fff; --ink:#1a1d24; --muted:#6f7480;
          --line:#e4e4e7; --accent:#3b4252; --good:#3f7d4e; --warn:#a4402f; --bad:#7a1f1f; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
         background:var(--bg); color:var(--ink); }
  header { background:var(--card); border-bottom:1px solid var(--line);
           padding:14px 24px; display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  h1 { font-size:17px; margin:0; font-weight:600; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%;
         background:var(--muted); margin-right:6px; }
  .dot.up { background:var(--good); }
  main { padding:24px; }
  .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em;
       color:var(--muted); margin:26px 0 12px; }
  table { width:100%; border-collapse:collapse; font-size:13.5px; margin-top:10px; }
  th,td { text-align:left; padding:6px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr:last-child td { border-bottom:none; }
  select,button,input { font:inherit; padding:6px 10px; border:1px solid var(--line);
                        border-radius:7px; background:var(--card); color:var(--ink); }
  button { background:var(--accent); color:#fff; border-color:var(--accent); cursor:pointer; }
  button:hover { opacity:.92; }
  a { color:var(--accent); }
  .ct { font-weight:600; font-size:14px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .ct code { background:#f1f1f3; padding:2px 7px; border-radius:6px; font-size:13px; }
  .m { color:#fff; font-size:11px; font-weight:700; padding:2px 7px; border-radius:5px; letter-spacing:.03em; }
  .get    { background:#2f6f4f; } .post   { background:#2c5aa0; }
  .patch  { background:#8a6d1f; } .del { background:var(--bad); }
  .hint { color:var(--muted); font-size:12.5px; margin:6px 0 10px; }
  .f { display:grid; gap:8px; grid-template-columns:1fr 1fr; margin-bottom:10px; }
  .f label { display:grid; gap:3px; font-size:12px; color:var(--muted); }
  .f input,.f select { width:100%; }
  .wide { grid-column:1 / -1; }
  .res { margin-top:4px; }
  .line { display:flex; gap:10px; align-items:center; margin:10px 0 6px; flex-wrap:wrap; }
  .badge { display:inline-block; min-width:42px; text-align:center; padding:2px 8px;
           border-radius:6px; font-weight:700; font-size:13px; color:#fff; }
  .badge.ok { background:var(--good); } .badge.warn { background:var(--warn); }
  .badge.err { background:var(--bad); }
  pre { background:#f4f4f5; border:1px solid var(--line); border-radius:8px;
        padding:10px; font-size:12.5px; max-height:280px; overflow:auto; margin:0; }
  .empty { color:var(--muted); font-style:italic; }
  .err { color:var(--warn); }
  .verdict { font-size:13px; margin-top:6px; }
  .verdict b.ok { color:var(--good); } .verdict b.bad { color:var(--bad); }
  .overview { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); }
</style>
</head>
<body>
<header>
  <h1>Attendance &amp; Analytics - API Console</h1>
  <span><span class="dot" id="dot"></span><span id="health">checking&hellip;</span></span>
  <a href="/docs">Swagger /docs &#8599;</a>
  <span style="flex:1"></span>
  <label>Month <select id="month" class="mo"></select></label>
  <button id="reload">Refresh tables</button>
</header>

<main>
  <h2 style="margin-top:0">Analytics overview (read-only)</h2>
  <div class="overview">
    <section>
      <h2>Department summary</h2>
      <div id="summary"><span class="empty">loading&hellip;</span></div>
    </section>
    <section>
      <h2>Late arrival leaderboard</h2>
      <div id="leaderboard"><span class="empty">loading&hellip;</span></div>
    </section>
    <section>
      <h2>Monthly record by employee</h2>
      <select id="emp"></select>
      <div id="monthly"><span class="empty">pick an employee</span></div>
    </section>
    <section>
      <h2>Most recent punches</h2>
      <div id="recent"><span class="empty">loading&hellip;</span></div>
    </section>
  </div>

  <h2>Operations console - every endpoint; each Run shows the HTTP status and the exact JSON output</h2>
  <div class="grid">

    <section>
      <div class="ct"><span class="m get">GET</span><code>/health</code></div>
      <p class="hint">Service liveness. Must return 200 and {"status":"ok"}.</p>
      <button onclick="doHealth()">Run</button>
      <div class="res" id="h_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m post">POST</span><code>/employees</code></div>
      <p class="hint">Create employee. New code gives 201; an existing code gives 409 (unique index).</p>
      <div class="f">
        <label>emp_code <input id="e_code" value="EMP9001"></label>
        <label>name <input id="e_name" value="Test User"></label>
        <label>email <input id="e_mail" value="test@example.com"></label>
        <label>department <input id="e_dept" value="Engineering"></label>
        <label>shift_start <input id="e_ss" value="09:30"></label>
        <label>shift_end <input id="e_se" value="18:30"></label>
        <label>joined_on <input id="e_join" value="2026-07-01"></label>
      </div>
      <button onclick="doCreate()">Run</button>
      <div class="res" id="e_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m get">GET</span><code>/employees</code></div>
      <p class="hint">List employees (optional department filter, cursor-style paging).</p>
      <div class="f">
        <label>department (optional) <input id="el_dept" placeholder="Engineering"></label>
        <label>page_size <input id="el_ps" type="number" value="20" min="1" max="100"></label>
      </div>
      <button onclick="doEmpList()">Run</button>
      <div class="res" id="el_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m post">POST</span><code>/attendance/punch-in</code></div>
      <p class="hint">Punch in. First punch today gives 201; the same employee again gives 409 (rule R4).</p>
      <div class="f">
        <label>emp_code <input id="pi_emp" placeholder="EMP0001"></label>
        <label>status <select id="pi_st"><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option></select></label>
        <label class="wide">punched_at (epoch ms, blank = now) <input id="pi_at" placeholder="1783396680000"></label>
      </div>
      <button onclick="doPunchIn()">Run</button>
      <div class="res" id="pi_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m post">POST</span><code>/attendance/punch-out</code></div>
      <p class="hint">Punch out. Must be after punch_in (rule R3); a second punch-out gives 409 (rule R5).</p>
      <div class="f">
        <label>emp_code <input id="po_emp" placeholder="EMP0001"></label>
        <label>punched_at (epoch ms, blank = now) <input id="po_at" placeholder="1783429200000"></label>
      </div>
      <button onclick="doPunchOut()">Run</button>
      <div class="res" id="po_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m get">GET</span><code>/attendance</code></div>
      <p class="hint">List attendance (filters + paging), newest date first.</p>
      <div class="f">
        <label>date_from <input id="al_from" type="date" value="2026-07-01"></label>
        <label>date_to <input id="al_to" type="date" value="2026-07-31"></label>
        <label>status <select id="al_st"><option value="">any</option><option>PRESENT</option><option>ABSENT</option><option>LEAVE</option><option>WFH</option><option>ON_DUTY</option></select></label>
        <label>page_size <input id="al_ps" type="number" value="100" min="1" max="100"></label>
      </div>
      <button onclick="doAttList()">Run</button>
      <div class="res" id="al_res"></div>
    </section>

    <section class="wide">
      <div class="ct"><span class="m patch">PATCH</span><code>/attendance/{emp_code}/{date}</code></div>
      <p class="hint">Manual correction (regularize). Every change appends an audit entry to history; a stale concurrent write gives 409.</p>
      <div class="f">
        <label>emp_code <input id="rg_emp" placeholder="EMP0006"></label>
        <label>date <input id="rg_date" type="date" value="2026-07-07"></label>
        <label>status <select id="rg_st"><option value="">(keep current)</option><option>PRESENT</option><option>ABSENT</option><option>LEAVE</option><option>WFH</option><option>ON_DUTY</option></select></label>
        <label>punch_in (epoch ms) <input id="rg_in" placeholder="1783397100000"></label>
        <label>punch_out (epoch ms) <input id="rg_out" placeholder="1783425600000"></label>
        <label>reason <input id="rg_why" value="forgot to punch out"></label>
        <label>regularized_by <input id="rg_by" value="hr.admin"></label>
      </div>
      <button onclick="doPatch()">Run</button>
      <div class="res" id="rg_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m get">GET</span><code>/analytics/employees/{emp_code}/monthly</code></div>
      <p class="hint">Monthly rollup for one employee (rules R8-R9: half-up rounding, joined-on cutoff).</p>
      <div class="f">
        <label>emp_code <input id="am_emp" placeholder="EMP0001"></label>
        <label>month <select id="am_month" class="mo"></select></label>
      </div>
      <button onclick="doMonthly()">Run</button>
      <div class="res" id="am_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m get">GET</span><code>/analytics/departments/summary</code></div>
      <p class="hint">Per-department rollup (headcount first, then logs - zero-log employees still count).</p>
      <div class="f">
        <label>month <select id="ds_month" class="mo"></select></label>
        <label>department (optional) <input id="ds_dept" placeholder="Sales"></label>
      </div>
      <button onclick="doSummary()">Run</button>
      <div class="res" id="ds_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m get">GET</span><code>/analytics/leaderboard/late</code></div>
      <p class="hint">Late arrivals, ranked. Ties share a rank and the next rank is skipped (1,2,2,4).</p>
      <div class="f">
        <label>month <select id="lb_month" class="mo"></select></label>
        <label>limit (rank cut-off) <input id="lb_lim" type="number" value="10" min="1" max="50"></label>
      </div>
      <button onclick="doBoard()">Run</button>
      <div class="res" id="lb_res"></div>
    </section>

    <section>
      <div class="ct"><span class="m get">GET</span><code>/analytics/departments/{department}/trend</code></div>
      <p class="hint">Daily trend with gap filling ($densify) and a 7-day moving average. Max 92 days.</p>
      <div class="f">
        <label>department <input id="tr_dept" value="Engineering"></label>
        <label>&nbsp;</label>
        <label>from <input id="tr_from" type="date" value="2026-07-01"></label>
        <label>to <input id="tr_to" type="date" value="2026-07-08"></label>
      </div>
      <button onclick="doTrend()">Run</button>
      <div class="res" id="tr_res"></div>
    </section>

    <section class="wide">
      <div class="ct"><span class="m get">GET</span><code>/admin/explain/{endpoint}</code></div>
      <p class="hint">Real MongoDB explain with executionStats. Rule R10: the plan must use an index (IXSCAN), never a full collection scan (COLLSCAN). Fill only the parameters your chosen endpoint needs.</p>
      <div class="f">
        <label>endpoint <select id="ex_ep"><option>attendance_list</option><option>employee_monthly</option><option>department_summary</option><option>late_leaderboard</option><option>department_trend</option></select></label>
        <label>emp_code <input id="ex_emp" placeholder="EMP0001"></label>
        <label>month <select id="ex_month" class="mo"><option value="">(not needed)</option></select></label>
        <label>department <input id="ex_dept" placeholder="Engineering"></label>
        <label>limit <input id="ex_lim" type="number" value="10" min="1" max="50"></label>
        <label>date_from <input id="ex_df" type="date"></label>
        <label>date_to <input id="ex_dt" type="date"></label>
        <label>from (trend) <input id="ex_from" type="date"></label>
        <label>to (trend) <input id="ex_to" type="date"></label>
        <label>page_size <input id="ex_ps" type="number" value="20" min="1" max="100"></label>
      </div>
      <button onclick="doExplain()">Run</button>
      <div class="res" id="ex_res"></div>
    </section>

  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).split("&").join("&amp;").split("<").join("&lt;").split(">").join("&gt;");
const v = (id) => ($(id).value || "").trim();

// epoch milliseconds -> "06 Jul, 09:28" in Indian time (the API returns ms)
const ist = (ms) => ms == null ? "-"
  : new Date(ms).toLocaleString("en-IN", { timeZone:"Asia/Kolkata",
      day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit", hour12:false });

// one generic caller: sends the request, renders STATUS + body into the card
async function call(method, path, body, resId) {
  $(resId).innerHTML = '<span class="empty">running&hellip;</span>';
  try {
    const r = await fetch(path, {
      method,
      headers: body ? {"Content-Type": "application/json"} : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await r.json(); } catch (e) { /* non-JSON body */ }
    const cls = r.ok ? "ok" : (r.status >= 500 ? "err" : "warn");
    $(resId).innerHTML =
      '<div class="line"><span class="badge ' + cls + '">' + r.status + '</span>' +
      '<code>' + method + ' ' + esc(path) + '</code></div>' +
      '<pre>' + esc(JSON.stringify(data, null, 2)) + '</pre>';
    return data;
  } catch (e) {
    $(resId).innerHTML = '<span class="err">' + esc(e.message) + '</span>';
    return null;
  }
}

// build "?a=1&b=2" skipping blank values
function qs(pairs) {
  const q = new URLSearchParams();
  for (const [k, val] of pairs) if (val !== "" && val != null) q.set(k, val);
  const s = q.toString();
  return s ? "?" + s : "";
}

// numeric fields: keep user text as-is if blank, cast if present (a float
// stays a float on purpose - sending one proves the API answers 422, rule R1)
function body(fields) {
  const o = {};
  for (const [k, raw] of fields) {
    if (raw === "" || raw == null) continue;
    o[k] = /^[0-9]+$/.test(raw) ? Number(raw) : raw;
  }
  return o;
}

// tiny table builder; cols = [{label, num?, get(row)}]
function render(el, rows, cols) {
  for (const ph of el.querySelectorAll(".empty")) ph.remove();   // drop "loading…" placeholder
  if (!rows || !rows.length) { el.insertAdjacentHTML("beforeend",
    '<span class="empty">no rows</span>'); return; }
  const head = cols.map(c => '<th class="' + (c.num ? "num" : "") + '">' + c.label + "</th>").join("");
  const trs = rows.map(r => "<tr>" + cols.map(c =>
    '<td class="' + (c.num ? "num" : "") + '">' + c.get(r) + "</td>").join("") + "</tr>").join("");
  el.insertAdjacentHTML("beforeend", "<table><thead><tr>" + head + "</tr></thead><tbody>" + trs + "</tbody></table>");
}

// ------------------------------------------------------------------ overview
async function loadMonth() {
  const m = $("month").value;
  $("summary").innerHTML = $("leaderboard").innerHTML = '<span class="empty">loading&hellip;</span>';
  try {
    const [sum, lead] = await Promise.all([
      getJSON("/analytics/departments/summary" + qs([["month", m]])),
      getJSON("/analytics/leaderboard/late" + qs([["month", m], ["limit", "10"]])),
    ]);
    render($("summary"), sum.items, [
      {label:"Department", get:r => r.department},
      {label:"Head", num:true, get:r => r.headcount},
      {label:"Present", num:true, get:r => r.present_days},
      {label:"Avg hrs", num:true, get:r => r.avg_work_hours ?? "-"},
      {label:"Late", num:true, get:r => r.late_count},
    ]);
    render($("leaderboard"), lead.items, [
      {label:"Rank", num:true, get:r => r.rank},
      {label:"Employee", get:r => r.name},
      {label:"Dept", get:r => r.department},
      {label:"Late mins", num:true, get:r => r.total_late_minutes},
    ]);
  } catch (e) {
    $("summary").innerHTML = $("leaderboard").innerHTML = '<span class="err">' + esc(e.message) + "</span>";
  }
}

async function loadMonthly() {
  const code = $("emp").value;
  if (!code) return;
  $("monthly").innerHTML = '<span class="empty">loading&hellip;</span>';
  try {
    const d = await getJSON("/analytics/employees/" + code + "/monthly" + qs([["month", $("month").value]]));
    render($("monthly"), [d], [
      {label:"Working days", num:true, get:r => r.working_days},
      {label:"Present", num:true, get:r => r.present_days},
      {label:"Attendance %", num:true, get:r => r.attendance_pct ?? "-"},
      {label:"Late", num:true, get:r => r.late_count},
    ]);
  } catch (e) {
    $("monthly").innerHTML = '<span class="err">' + esc(e.message) + "</span>";
  }
}

async function loadRecent() {
  try {
    const d = await getJSON("/attendance" + qs([["page_size", "8"]]));
    render($("recent"), d.items, [
      {label:"Emp", get:r => r.emp_code},
      {label:"Date", get:r => r.date},
      {label:"In", get:r => ist(r.punch_in)},
      {label:"Out", get:r => ist(r.punch_out)},
      {label:"Hrs", num:true, get:r => r.work_hours ?? "-"},
      {label:"Status", get:r => r.status},
    ]);
  } catch (e) {
    $("recent").innerHTML = '<span class="err">' + esc(e.message) + "</span>";
  }
}

async function loadHealth() {
  try { await getJSON("/health"); $("health").textContent = "Service online"; $("dot").classList.add("up"); }
  catch { $("health").textContent = "Service offline"; }
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
  return r.json();
}

// ------------------------------------------------------------- operations
async function doHealth() {
  await call("GET", "/health", null, "h_res");
}

async function doCreate() {
  await call("POST", "/employees", body([
    ["emp_code", v("e_code")], ["name", v("e_name")], ["email", v("e_mail")],
    ["department", v("e_dept")], ["shift_start", v("e_ss")],
    ["shift_end", v("e_se")], ["joined_on", v("e_join")],
  ]), "e_res");
}

async function doEmpList() {
  await call("GET", "/employees" + qs([
    ["department", v("el_dept")], ["page_size", v("el_ps")],
  ]), null, "el_res");
}

async function doPunchIn() {
  await call("POST", "/attendance/punch-in", body([
    ["emp_code", v("pi_emp")], ["punched_at", v("pi_at")], ["status", v("pi_st")],
  ]), "pi_res");
}

async function doPunchOut() {
  await call("POST", "/attendance/punch-out", body([
    ["emp_code", v("po_emp")], ["punched_at", v("po_at")],
  ]), "po_res");
}

async function doAttList() {
  await call("GET", "/attendance" + qs([
    ["date_from", v("al_from")], ["date_to", v("al_to")],
    ["status", v("al_st")], ["page_size", v("al_ps")],
  ]), null, "al_res");
}

async function doPatch() {
  await call("PATCH", "/attendance/" + encodeURIComponent(v("rg_emp")) + "/" + v("rg_date"), body([
    ["status", v("rg_st")], ["punch_in", v("rg_in")], ["punch_out", v("rg_out")],
    ["reason", v("rg_why")], ["regularized_by", v("rg_by")],
  ]), "rg_res");
}

async function doMonthly() {
  const data = await call("GET", "/analytics/employees/" + encodeURIComponent(v("am_emp")) +
    "/monthly" + qs([["month", v("am_month")]]), null, "am_res");
  if (data && data.emp_code) render($("am_res"), [data], [
    {label:"Working days", num:true, get:r => r.working_days},
    {label:"Present", num:true, get:r => r.present_days},
    {label:"Leave", num:true, get:r => r.leave_days},
    {label:"Late", num:true, get:r => r.late_count},
    {label:"Att %", num:true, get:r => r.attendance_pct ?? "-"},
  ]);
}

async function doSummary() {
  const data = await call("GET", "/analytics/departments/summary" + qs([
    ["month", v("ds_month")], ["department", v("ds_dept")],
  ]), null, "ds_res");
  if (data && data.items) render($("ds_res"), data.items, [
    {label:"Dept", get:r => r.department},
    {label:"Head", num:true, get:r => r.headcount},
    {label:"Present", num:true, get:r => r.present_days},
    {label:"Avg hrs", num:true, get:r => r.avg_work_hours ?? "-"},
    {label:"Late", num:true, get:r => r.late_count},
    {label:"Leave", num:true, get:r => r.leave_count},
  ]);
}

async function doBoard() {
  const data = await call("GET", "/analytics/leaderboard/late" + qs([
    ["month", v("lb_month")], ["limit", v("lb_lim")],
  ]), null, "lb_res");
  if (data && data.items) render($("lb_res"), data.items, [
    {label:"Rank", num:true, get:r => r.rank},
    {label:"Employee", get:r => r.name},
    {label:"Dept", get:r => r.department},
    {label:"Late mins", num:true, get:r => r.total_late_minutes},
    {label:"Late days", num:true, get:r => r.late_count},
  ]);
}

async function doTrend() {
  const data = await call("GET", "/analytics/departments/" + encodeURIComponent(v("tr_dept")) +
    "/trend" + qs([["from", v("tr_from")], ["to", v("tr_to")]]), null, "tr_res");
  if (data && data.items) render($("tr_res"), data.items, [
    {label:"Date", get:r => r.date},
    {label:"Work day", get:r => r.is_working_day ? "yes" : "no"},
    {label:"Head", num:true, get:r => r.headcount},
    {label:"Present", num:true, get:r => r.present_count},
    {label:"Late", num:true, get:r => r.late_count},
    {label:"Rate", num:true, get:r => r.attendance_rate ?? "-"},
    {label:"7d avg", num:true, get:r => r.moving_avg_7d ?? "-"},
  ]);
}

async function doExplain() {
  const data = await call("GET", "/admin/explain/" + v("ex_ep") + qs([
    ["emp_code", v("ex_emp")], ["month", v("ex_month")], ["department", v("ex_dept")],
    ["limit", v("ex_lim")], ["date_from", v("ex_df")], ["date_to", v("ex_dt")],
    ["from", v("ex_from")], ["to", v("ex_to")], ["page_size", v("ex_ps")],
  ]), null, "ex_res");
  if (data && data.explain) {
    const s = JSON.stringify(data.explain);
    const ix = s.indexOf("IXSCAN") >= 0, co = s.indexOf("COLLSCAN") >= 0;
    const cls = co ? "bad" : "ok";
    $("ex_res").insertAdjacentHTML("beforeend",
      '<p class="verdict">Rule R10 verdict: index used (IXSCAN): <b class="' + (ix ? "ok" : "bad") + '">' + ix +
      '</b> &nbsp; full collection scan (COLLSCAN): <b class="' + cls + '">' + co + '</b></p>');
  }
}

// month list for every picker: last 12 months, newest first
function monthOptions() {
  const out = [], now = new Date();
  for (let i = 0; i < 12; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push(d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0"));
  }
  return out;
}

function refresh() { loadMonth(); loadRecent(); loadMonthly(); }

// Preselect the newest month that actually has attendance records (the sample
// data sits in the past, so defaulting to the current month would open empty).
async function boot() {
  const months = monthOptions();
  for (const sel of document.querySelectorAll(".mo")) {
    sel.innerHTML = months.map(m => "<option>" + m + "</option>").join("");
  }
  try {
    const latest = await getJSON("/attendance" + qs([["page_size", "1"]]));  // sorted date desc
    if (latest.items.length) {
      const newest = latest.items[0].date.slice(0, 7);   // "2026-07-06" -> "2026-07"
      for (const sel of document.querySelectorAll(".mo")) {
        if ([...sel.options].some(o => o.value === newest)) sel.value = newest;
      }
    }
  } catch (e) { /* the health badge already reports an unreachable API */ }
  $("month").onchange = refresh;
  $("emp").onchange = loadMonthly;
  $("reload").onclick = refresh;
  try {
    const d = await getJSON("/employees" + qs([["page_size", "100"]]));
    $("emp").innerHTML = d.items.map(e =>
      '<option value="' + e.emp_code + '">' + e.emp_code + " - " + e.name + "</option>").join("");
    if (d.items.length) loadMonthly();
  } catch (e) { /* ignore; the cards show their own errors */ }
  loadHealth(); loadMonth(); loadRecent();
}

boot();
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    """Console UI (extra, not in the contract; never part of the OpenAPI schema).

    One page with (a) read-only analytics tables and (b) an operations console
    where every public endpoint can be called through a small form and its HTTP
    status + raw JSON output are shown side by side. It only calls the public
    JSON endpoints with fetch(); it never talks to MongoDB and contains no
    business logic of its own, so the API itself is byte-for-byte unchanged.
    """
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Convenience redirect: the console UI lives at /dashboard."""
    return RedirectResponse("/dashboard")
