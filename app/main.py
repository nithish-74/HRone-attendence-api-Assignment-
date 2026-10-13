"""
Employee Attendance & Analytics API
FastAPI + MongoDB backend implementation.
"""
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import calendar
import json
import os
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load .env without overriding existing environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Round a float half-up to the given number of decimal places."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert integer epoch milliseconds to UTC datetime truncated to seconds."""
    if ms is None:
        return None
    # R1: Instants truncated to whole seconds
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def truncate_dt_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime to whole seconds."""
    return dt.replace(microsecond=0)


def determine_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date:
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    ist_dt = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        ist_time_str = ist_dt.strftime("%H:%M")
        if ist_time_str < shift_end:
            prev_day = ist_dt.date() - timedelta(days=1)
            return prev_day.isoformat()
    return ist_dt.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2 Late:
    Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start.
    late_minutes = whole minutes (floor) since shift_start, not since grace ended.
    """
    start_h, start_m = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime.combine(att_date, datetime.min.time(), tzinfo=IST).replace(
        hour=start_h, minute=start_m, second=0, microsecond=0
    )
    punch_in_ist = punch_in_dt.astimezone(IST)
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4 Work hours:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    hours = Decimal(diff_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3 Overtime:
    Whole minutes (floor) from shift_end to punch_out, counted only if at least 30; otherwise 0.
    Overnight shifts end on the next calendar day.
    """
    end_h, end_m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    if shift_end <= shift_start:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date
    shift_end_dt = datetime.combine(end_date, datetime.min.time(), tzinfo=IST).replace(
        hour=end_h, minute=end_m, second=0, microsecond=0
    )
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    ot_minutes = int(diff_sec // 60)
    return ot_minutes if ot_minutes >= 30 else 0


def format_attendance_doc(doc: dict) -> dict:
    """Format MongoDB attendance document to OpenAPI AttendanceRecord response."""
    history_out = []
    for h in doc.get("history", []):
        changes_out = {}
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if isinstance(from_val, datetime):
                from_val = dt_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_epoch_ms(to_val)
            changes_out[k] = {"from": from_val, "to": to_val}
        history_out.append({
            "at": dt_to_epoch_ms(h["at"]),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }


def format_employee_doc(doc: dict) -> dict:
    """Format MongoDB employee document to OpenAPI Employee response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """R7: Count Mon-Fri working days in the month, from joined_on for mid-month joiners."""
    y, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(y, m)
    month_start_date = date(y, m, 1)
    month_end_date = date(y, m, last_day)

    joined_date = date.fromisoformat(joined_on_str)
    effective_start = max(month_start_date, joined_date)
    if effective_start > month_end_date:
        return 0

    cur = effective_start
    working_days = 0
    while cur <= month_end_date:
        if cur.weekday() < 5:  # Monday to Friday
            working_days += 1
        cur += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_shift_and_joined(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        now = datetime.now(timezone.utc)
        punch_in_dt = truncate_dt_to_seconds(now)

    att_date = determine_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        now = datetime.now(timezone.utc)
        punch_out_dt = truncate_dt_to_seconds(now)

    # Find the most recent record whose punch_in is <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    punch_in_dt = record["punch_in"]
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(422, "punched_at must be strictly after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = work_hours < 4.50
    overtime_mins = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])

    # Race-safe atomic update
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Record already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(422, "Request must specify changes")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch times when setting status to ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(422, "Presence status requires punch_in")

        # punch_in must stay on the record's attendance date per R1
        punch_in_att_date = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if punch_in_att_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_half_day = new_work_hours < 4.50
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)

    # Calculate diff
    changes_db = {}
    if target_status != record["status"]:
        changes_db["status"] = {"from": record["status"], "to": target_status}
    if new_punch_in != record.get("punch_in"):
        changes_db["punch_in"] = {"from": record.get("punch_in"), "to": new_punch_in}
    if new_punch_out != record.get("punch_out"):
        changes_db["punch_out"] = {"from": record.get("punch_out"), "to": new_punch_out}
    if new_work_hours != record.get("work_hours"):
        changes_db["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != record.get("late_minutes", 0):
        changes_db["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != record.get("overtime_minutes", 0):
        changes_db["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != record.get("half_day", False):
        changes_db["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes_db:
        raise HTTPException(422, "Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic concurrency control using history array matching
    existing_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": existing_history},
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Concurrent regularize update conflict")

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    y, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(y, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start_date, "$lte": end_date},
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {
                                    "$and": [
                                        {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                                        {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                                    ]
                                },
                            ]
                        },
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late = int(agg["total_late_minutes"])
        total_ot = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    y, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(y, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", start_date]},
                            {"$lte": ["$date", end_date]},
                        ]
                    }
                }}
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "emp_code": 1,
            "logs": 1,
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True,
        }},
        {"$group": {
            "_id": {
                "department": "$department",
                "emp_code": "$emp_code",
            },
            "department": {"$first": "$department"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {
                                    "$and": [
                                        {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 1]},
                                        {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 7]},
                                    ]
                                },
                            ]
                        },
                        {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "work_hours_sum": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$logs.work_hours", None]},
                            ]
                        },
                        "$logs.work_hours",
                        0.0,
                    ]
                }
            },
            "work_hours_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$logs.work_hours", None]},
                            ]
                        },
                        1,
                        0,
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]},
                        1,
                        0,
                    ]
                }
            },
            "total_late_minutes": {
                "$sum": {
                    "$cond": [
                        {"$ne": ["$logs", None]},
                        {"$ifNull": ["$logs.late_minutes", 0]},
                        0,
                    ]
                }
            },
            "leave_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "LEAVE"]}]},
                        1,
                        0,
                    ]
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "ON_DUTY"]}]},
                        1,
                        0,
                    ]
                }
            },
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$work_hours_count", 0]},
                    {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                    None,
                ]
            },
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    for it in items:
        if it.get("avg_work_hours") is not None:
            it["avg_work_hours"] = round_half_up(it["avg_work_hours"], 2)
    return {"month": month, "items": items}


def build_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    y, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(y, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipe = [
        {"$match": {
            "date": {"$gte": start_date, "$lte": end_date},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipe.append({"$match": {"emp.department": department}})

    pipe.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipe


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> list:
    from_dt = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_str).replace(tzinfo=timezone.utc)
    to_dt_plus_1 = to_dt + timedelta(days=1)

    return [
        {"$match": {
            "date": {"$gte": from_str, "$lte": to_str},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {
            "emp.department": department,
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "date_dt": {"$dateFromString": {"dateString": "$_id"}},
            "present_count": 1,
            "late_count": 1,
        }},
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt_plus_1],
            },
        }},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"date": 1}},
        {"$lookup": {
            "from": "employees",
            "pipeline": [
                {"$match": {"department": department}},
                {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
            ],
            "as": "dept_emps",
        }},
        {"$project": {
            "date": 1,
            "present_count": 1,
            "late_count": 1,
            "headcount": {
                "$size": {
                    "$filter": {
                        "input": "$dept_emps",
                        "as": "e",
                        "cond": {"$lte": ["$$e.joined_on", "$date"]},
                    }
                }
            },
            "is_working_day": {
                "$and": [
                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                ]
            },
        }},
        {"$project": {
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]
            },
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, "current"]},
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")

    if d_to < d_from:
        raise HTTPException(422, "to date cannot be earlier than from date")

    day_span = (d_to - d_from).days + 1
    if day_span > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    # Check if department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))

    for it in items:
        if it.get("attendance_rate") is not None:
            it["attendance_rate"] = round_half_up(it["attendance_rate"], 4)
        if it.get("moving_avg_7d") is not None:
            it["moving_avg_7d"] = round_half_up(it["moving_avg_7d"], 4)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        raw_explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        y, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(y, m)
        start_date = f"{month}-01"
        end_date = f"{month}-{last_day:02d}"
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$and": [
                                            {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                                            {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }},
        ]
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        raw_explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        pipeline = build_leaderboard_pipeline(month, limit, department)
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")
        if d_to < d_from:
            raise HTTPException(422, "to date cannot be earlier than from date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"
    else:
        raise HTTPException(422, f"Unknown endpoint: {endpoint}")

    explain_clean = json.loads(json_util.dumps(raw_explain))
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_clean,
    }
