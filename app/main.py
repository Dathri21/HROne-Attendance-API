"""Employee Attendance & Analytics API (HROne hiring assignment).

Run from the repository root: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables (optional local .env).
"""
from __future__ import annotations

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()  # Existing process environment variables take precedence over .env.
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Startup indexes (idempotent). Unique indexes enforce race safety in MongoDB.
# ---------------------------------------------------------------------------
def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_employee_date_desc")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")


@app.on_event("startup")
def startup() -> None:
    # Index creation is deliberately idempotent; readiness still verifies the DB.
    ensure_indexes()


# ---------------------------------------------------------------------------
# Validation, date/time and serialization helpers
# ---------------------------------------------------------------------------
def parse_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def epoch_to_utc(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    # API instants are stored at whole-second precision (R1).
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quant = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def shift_bounds(shift_start: str, shift_end: str, attendance_date: str) -> tuple[datetime, datetime]:
    day = parse_date(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_local = datetime.combine(day, time(sh, sm), IST)
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    end_local = datetime.combine(end_day, time(eh, em), IST)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_utc(punch_in).astimezone(IST)
    start = time(*map(int, shift_start.split(":")))
    end = time(*map(int, shift_end.split(":")))
    day = local.date()
    if end <= start and local.time().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, attendance_date: str) -> int:
    if punch_in is None:
        return 0
    day = parse_date(attendance_date)
    h, m = map(int, shift_start.split(":"))
    shift_start_utc = datetime.combine(day, time(h, m), IST).astimezone(UTC)
    elapsed = (as_utc(punch_in) - shift_start_utc).total_seconds()
    return int(elapsed // 60) if elapsed > 600 else 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    return round_half_up(Decimal(str(seconds)) / Decimal(3600), 2)


def compute_overtime_minutes(punch_out: Optional[datetime], shift_end: str, attendance_date: str, shift_start: str) -> int:
    if punch_out is None:
        return 0
    _, end_utc = shift_bounds(shift_start, shift_end, attendance_date)
    elapsed = (as_utc(punch_out) - end_utc).total_seconds()
    minutes = int(elapsed // 60)
    return minutes if minutes >= 30 else 0


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, day: str) -> dict:
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp["shift_start"], day)
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime_minutes(punch_out, emp["shift_end"], day, emp["shift_start"]) if punch_out else 0
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": bool(hours is not None and hours < 4.50)}


def serialize_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in out:
        out["created_at"] = to_epoch_ms(out["created_at"])
    return out


def serialize_attendance(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        out[key] = to_epoch_ms(out.get(key))
    out.setdefault("work_hours", None)
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    out.setdefault("history", [])
    history = []
    for item in out["history"]:
        h = dict(item)
        h["at"] = to_epoch_ms(h.get("at"))
        changes = {}
        for key, values in h.get("changes", {}).items():
            changes[key] = {"from": to_epoch_ms(values.get("from")) if key in ("punch_in", "punch_out") else values.get("from"),
                            "to": to_epoch_ms(values.get("to")) if key in ("punch_in", "punch_out") else values.get("to")}
        h["changes"] = changes
        history.append(h)
    out["history"] = history
    return out


def validate_pagination(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def api_doc(doc: dict) -> dict:
    # Extended JSON conversion also handles any Decimal/ObjectId accidentally returned by drivers.
    return json_util.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


# ---------------------------------------------------------------------------
# Request models: strict epoch-millisecond values, contract-aligned validation.
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v: str) -> str:
        try:
            if date.fromisoformat(v).isoformat() != v:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None and not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds in range")
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None and not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds in range")
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None and not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS:
            raise ValueError("timestamp must be epoch milliseconds in range")
        return v


# ---------------------------------------------------------------------------
# Employee and attendance CRUD endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_seconds()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = epoch_to_utc(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = epoch_to_utc(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    # Most recent still-open punch-in at or before the requested punch-out.
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    pin = as_utc(record["punch_in"])
    elapsed = (ts - pin).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    derived = derived_fields(record["status"], pin, ts, emp, record["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")
    old_status = old.get("status", "PRESENT")
    status = body.status if body.status is not None else old_status
    pin = epoch_to_utc(body.punch_in) if body.punch_in is not None else as_utc(old.get("punch_in"))
    pout = epoch_to_utc(body.punch_out) if body.punch_out is not None else as_utc(old.get("punch_out"))
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot supply punch times for ABSENT or LEAVE")
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pin is not None and attendance_date_for(pin, emp["shift_start"], emp["shift_end"]) != date:
        raise HTTPException(422, "punch_in must belong to the record attendance date")
    if pout is not None:
        if pin is None or (pout - pin).total_seconds() <= 0 or (pout - pin).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = derived_fields(status, pin, pout, emp, date)
    candidate = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    old_normalized = {k: old.get(k, {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}.get(k)) for k in candidate}
    changes = {}
    for key, new_value in candidate.items():
        old_value = old_normalized.get(key)
        if key in ("punch_in", "punch_out"):
            old_value, new_value = as_utc(old_value), as_utc(new_value)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    at = now_utc_seconds()
    entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap on the old history snapshot prevents concurrent updates losing audit entries.
    history = old.get("history", [])
    query = {"_id": old["_id"]}
    if "history" in old:
        query["history"] = history
    else:
        query["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(query,
        {"$set": candidate, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry with the latest version")
    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# Aggregation pipeline builders. Analytics are computed inside MongoDB.
# ---------------------------------------------------------------------------
def month_match(month: str) -> tuple[str, str]:
    start, end = parse_month(month)
    return start.isoformat(), (end + timedelta(days=1)).isoformat()


def employee_month_pipeline(emp_code: str, month: str) -> list[dict]:
    start, after = month_match(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": after}}},
        {"$addFields": {"_dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}}}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$in": ["$_dayOfWeek", [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    _, after = month_match(month)
    match = {"joined_on": {"$lt": after}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", month + "-01"]}, {"$lt": ["$date", after]}]}}},
            {"$addFields": {"_dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}}}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": ["$logs._dayOfWeek", [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    start, after = month_match(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lt": after}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, start_date: str, end_date: str) -> list[dict]:
    start = parse_date(start_date, "from")
    end = parse_date(end_date, "to")
    # $documents creates a calendar spine even if the department has no attendance logs.
    start_dt = datetime.combine(start, time.min, UTC)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min, UTC)
    return [
        {"$documents": [{"day": start_dt}]},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": "UTC"}}, "weekday": {"$dayOfWeek": "$day"}}},
        {"$lookup": {"from": "employees", "let": {"daystr": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$daystr"]}}},
            {"$project": {"_id": 0, "emp_code": 1}}], "as": "eligible_employees"}},
        {"$set": {"headcount": {"$size": "$eligible_employees"}, "employee_codes": "$eligible_employees.emp_code"}},
        {"$lookup": {"from": "attendance_logs", "let": {"daystr": "$date", "codes": "$employee_codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$daystr"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}, "is_working_day": {"$in": ["$weekday", [2, 3, 4, 5, 6]]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    result = list(db.attendance_logs.aggregate(employee_month_pipeline(emp_code, month)))
    stats = result[0] if result else {}
    first_working = max(start, parse_date(emp["joined_on"], "joined_on"))
    wd = working_days(first_working, end)
    present = round_half_up(stats.get("present_days", 0), 2)
    pct = round_half_up(present / wd * 100, 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": int(stats.get("leave_days", 0)), "late_count": int(stats.get("late_count", 0)),
            "total_late_minutes": int(stats.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    parse_month(month)
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department), allowDiskUse=True))
    items = []
    for row in rows:
        # A department is omitted if its headcount is zero; the pipeline's employee-driven join ensures
        # employees with no logs remain in the group.
        if row["headcount"] == 0:
            continue
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2) if row.get("avg_work_hours") is not None else None
        row["total_late_minutes"] = int(row.get("total_late_minutes", 0))
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    parse_month(month)
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, department), allowDiskUse=True))
    # $rank assigns standard competition ranks. Apply limit after ranking so cutoff ties remain.
    items = []
    for row in rows:
        if row["rank"] > limit:
            break
        row["rank"] = int(row["rank"])
        row["total_late_minutes"] = int(row["total_late_minutes"])
        row["late_count"] = int(row["late_count"])
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    rows = list(db.aggregate(trend_pipeline(department, from_date, to_date), allowDiskUse=True))
    items = []
    for row in rows:
        for key in ("attendance_rate", "moving_avg_7d"):
            row[key] = round_half_up(row[key], 4) if row.get(key) is not None else None
        row["present_count"] = round_half_up(row.get("present_count", 0), 2)
        row["headcount"] = int(row.get("headcount", 0))
        row["late_count"] = int(row.get("late_count", 0))
        items.append(row)
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Explain endpoint: same filters/pipelines as the real endpoints where applicable.
# ---------------------------------------------------------------------------
def explain_query(collection, query: dict, sort: list, skip: int, limit: int) -> dict:
    return collection.find(query).sort(sort).skip(skip).limit(limit).explain("executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                     emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to")):
    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        exp = explain_query(db.attendance_logs, q, [("date", DESCENDING), ("emp_code", ASCENDING)], (page - 1) * page_size, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": api_doc(exp)}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        parse_month(month)
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": employee_month_pipeline(emp_code, month), "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": api_doc(exp)}
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        parse_month(month)
        exp = db.command("explain", {"aggregate": "employees", "pipeline": department_summary_pipeline(month, department), "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": api_doc(exp)}
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        parse_month(month)
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": leaderboard_pipeline(month, department), "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": api_doc(exp)}
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        exp = db.command("explain", {"aggregate": 1, "pipeline": trend_pipeline(department, from_date, to_date), "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": api_doc(exp)}
    raise HTTPException(422, "unsupported endpoint")
