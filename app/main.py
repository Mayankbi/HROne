"""
Employee Attendance & Analytics API

Implemented in FastAPI + MongoDB according to OpenAPI 2.0.0 contract and business rules R1-R10.
"""
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# Real environment variables take priority over .env
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Index Initialization
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes at startup idempotently."""
    # 1. Unique index on employees.emp_code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    # 2. Filter/sort index on employees by department, emp_code
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_emp_dept_code")
    # 3. Index on joined_on for headcount and period filtering
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")

    # 4. Unique compound index on attendance_logs (emp_code, date)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_att_emp_date_unique"
    )
    # 5. Compound index for listAttendance: sorted by date DESC, emp_code ASC
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_att_date_desc_emp_asc"
    )
    # 6. Compound index for employee monthly analytics and queries
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="idx_att_emp_date_status"
    )
    # 7. Index for department/leaderboard/trend date matching
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="idx_att_date_late"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Utilities (Math, Rounding, Time & Shifts)
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int) -> float:
    """Half-up rounding per R8 (rates 4 decimals, numbers 2 decimals)."""
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """
    Truncate epoch ms to whole seconds per R1, then convert to UTC-aware datetime.
    """
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC or naive datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def determine_attendance_date(punch_in_dt_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is the IST calendar date of punch_in.
    Exception: overnight shifts (shift_end <= shift_start), punch-in IST time < shift_end
    belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_dt_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if (dt_ist.hour, dt_ist.minute, dt_ist.second) < (end_h, end_m, 0):
            # Previous calendar day in IST
            prev_day = dt_ist.date() - timedelta(days=1)
            return prev_day.isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch_in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    Shift start is on attendance_date in IST.
    """
    sh_h, sh_m = map(int, shift_start.split(":"))
    att_y, att_m, att_d = map(int, attendance_date_str.split("-"))
    shift_start_dt = datetime(att_y, att_m, att_d, sh_h, sh_m, 0, tzinfo=IST)
    
    # Calculate difference in seconds
    diff_sec = (punch_in_dt_utc - shift_start_dt).total_seconds()
    if diff_sec > 600:  # strictly more than 10:00 (600 seconds)
        return int(diff_sec // 60)
    return 0


def compute_overtime_minutes(punch_out_dt_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if that is >= 30; otherwise 0.
    For overnight shift, shift_end falls on next calendar day.
    """
    end_h, end_m = map(int, shift_end.split(":"))
    att_y, att_m, att_d = map(int, attendance_date_str.split("-"))
    shift_end_date = dt_date(att_y, att_m, att_d)
    if shift_end <= shift_start:
        # Overnight shift: ends next day
        shift_end_date = shift_end_date + timedelta(days=1)
    
    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, end_h, end_m, 0, tzinfo=IST)
    diff_sec = (punch_out_dt_utc - shift_end_dt).total_seconds()
    if diff_sec >= 1800:  # at least 30 minutes (1800 seconds)
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt_utc: datetime, punch_out_dt_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    sec = (punch_out_dt_utc - punch_in_dt_utc).total_seconds()
    return round_half_up(sec / 3600.0, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Mon-Fri working days in the month.
    If joined_on is after the month start, count from joined_on.
    If joined_on is after the month end, 0.
    """
    y, m = map(int, month_str.split("-"))
    start_date = dt_date(y, m, 1)
    if m == 12:
        end_date = dt_date(y + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = dt_date(y, m + 1, 1) - timedelta(days=1)
    
    joined_date = dt_date.fromisoformat(joined_on_str)
    effective_start = max(start_date, joined_date)
    if effective_start > end_date:
        return 0
    
    count = 0
    cur = effective_start
    while cur <= end_date:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


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
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


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


# Format document to response dict
def format_employee_doc(doc: dict) -> dict:
    created = doc.get("created_at")
    created_ms = datetime_to_epoch_ms(created) if isinstance(created, datetime) else created
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_ms,
    }


def format_history_entry(entry: dict) -> dict:
    at_val = entry.get("at")
    at_ms = datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
    formatted_changes = {}
    for k, v in entry.get("changes", {}).items():
        frm = v.get("from")
        to = v.get("to")
        if k in ("punch_in", "punch_out"):
            frm = datetime_to_epoch_ms(frm) if isinstance(frm, datetime) else frm
            to = datetime_to_epoch_ms(to) if isinstance(to, datetime) else to
        formatted_changes[k] = {"from": frm, "to": to}
    return {
        "at": at_ms,
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": formatted_changes,
    }


def format_attendance_doc(doc: dict) -> dict:
    pin = doc.get("punch_in")
    pout = doc.get("punch_out")
    history_list = [format_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(pin) if isinstance(pin, datetime) else pin,
        "punch_out": datetime_to_epoch_ms(pout) if isinstance(pout, datetime) else pout,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_list,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# 1. GET /health
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB ping failed")


# 2. POST /employees
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="shift_start must differ from shift_end"
        )
    now_utc = datetime.now(timezone.utc)
    # Truncate to whole seconds for consistency
    now_utc = epoch_ms_to_datetime_utc(int(now_utc.timestamp() * 1000))
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")
    
    return format_employee_doc(doc)


# 3. GET /employees
@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department is not None:
        q["department"] = department
    
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# 4. POST /attendance/punch-in
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    
    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        punch_in_dt = epoch_ms_to_datetime_utc(now_ms)
    
    attendance_date = determine_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)
    
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Attendance record already exists for date {attendance_date}"
        )
    
    return format_attendance_doc(doc)


# 5. POST /attendance/punch-out
@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    
    if body.punched_at is not None:
        pout_dt = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        pout_dt = epoch_ms_to_datetime_utc(now_ms)
    
    # Find most recent record whose punch_in <= punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": pout_dt}},
        sort=[("punch_in", DESCENDING)]
    )
    if not candidate:
        # Check if there is a record with punch_in > pout_dt for this employee
        future_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": pout_dt}},
            sort=[("punch_in", ASCENDING)]
        )
        if future_record:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punched_at is before punch_in"
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No punch-in found for this employee"
        )
    
    pin_dt = candidate["punch_in"]
    if pin_dt.tzinfo is None:
        pin_dt = pin_dt.replace(tzinfo=timezone.utc)
    
    # Validation per contract: punched_at <= punch_in or more than 24 hours after it -> 422
    diff_sec = (pout_dt - pin_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be strictly after punch_in"
        )
    if diff_sec > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out cannot be more than 24 hours after punch_in"
        )
    
    # Compute derived fields
    work_hours = compute_work_hours(pin_dt, pout_dt)
    overtime_min = compute_overtime_minutes(pout_dt, emp["shift_start"], emp["shift_end"], candidate["date"])
    half_day = compute_half_day(work_hours)
    
    # Atomic update: only if punch_out is currently null (prevents race condition)
    update_res = db.attendance_logs.update_one(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {
            "punch_out": pout_dt,
            "work_hours": work_hours,
            "overtime_minutes": overtime_min,
            "half_day": half_day,
        }}
    )
    if update_res.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attendance record is already punched out"
        )
    
    updated_doc = db.attendance_logs.find_one({"_id": candidate["_id"]})
    return format_attendance_doc(updated_doc)


# 6. GET /attendance
@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to"
        )
    
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# 7. PATCH /attendance/{emp_code}/{date}
@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found for this date")
    
    cur_status = record["status"]
    cur_pin = record.get("punch_in")
    cur_pout = record.get("punch_out")
    if cur_pin and cur_pin.tzinfo is None:
        cur_pin = cur_pin.replace(tzinfo=timezone.utc)
    if cur_pout and cur_pout.tzinfo is None:
        cur_pout = cur_pout.replace(tzinfo=timezone.utc)
    
    target_status = body.status if body.status is not None else cur_status
    
    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Cannot supply punch times when status is {target_status}"
            )
        target_pin = None
        target_pout = None
    else:
        # Presence status
        if body.punch_in is not None:
            target_pin = epoch_ms_to_datetime_utc(body.punch_in)
        else:
            target_pin = cur_pin
        
        if target_pin is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in"
            )
        
        # punch_in must stay on the record's attendance date per R1
        att_date_of_pin = determine_attendance_date(target_pin, emp["shift_start"], emp["shift_end"])
        if att_date_of_pin != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to attendance date {att_date_of_pin}, does not match record date {date}"
            )
        
        if body.punch_out is not None:
            target_pout = epoch_ms_to_datetime_utc(body.punch_out)
        else:
            target_pout = cur_pout
        
        if target_pout is not None:
            diff_sec = (target_pout - target_pin).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in"
                )
            if diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in"
                )
    
    # Compute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        target_work_hours = None
        target_late_min = 0
        target_overtime_min = 0
        target_half_day = False
    else:
        target_late_min = compute_late_minutes(target_pin, emp["shift_start"], date)
        if target_pout is not None:
            target_work_hours = compute_work_hours(target_pin, target_pout)
            target_overtime_min = compute_overtime_minutes(target_pout, emp["shift_start"], emp["shift_end"], date)
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_min = 0
            target_half_day = False
    
    # Calculate changes
    changes = {}
    if target_status != cur_status:
        changes["status"] = {"from": cur_status, "to": target_status}
    if target_pin != cur_pin:
        changes["punch_in"] = {"from": cur_pin, "to": target_pin}
    if target_pout != cur_pout:
        changes["punch_out"] = {"from": cur_pout, "to": target_pout}
    if target_work_hours != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": target_work_hours}
    if target_late_min != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": target_late_min}
    if target_overtime_min != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": target_overtime_min}
    if target_half_day != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": target_half_day}
    
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Regularization request changes nothing"
        )
    
    history_entry = {
        "at": epoch_ms_to_datetime_utc(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    
    # Optimistic concurrency check: match current history length
    cur_history_len = len(record.get("history", []))
    update_res = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": {"$size": cur_history_len}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_pin,
                "punch_out": target_pout,
                "work_hours": target_work_hours,
                "late_minutes": target_late_min,
                "overtime_minutes": target_overtime_min,
                "half_day": target_half_day,
            },
            "$push": {
                "history": history_entry
            }
        }
    )
    if update_res.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on attendance history"
        )
    
    updated_doc = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Aggregation Analytics Endpoints & Explain Pipeline Builders
# --------------------------------------------------------------------------- #

def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$regex": f"^{month}"}
            }
        },
        {
            "$addFields": {
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]


# 8. GET /analytics/employees/{emp_code}/monthly
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))
    stats = agg_res[0] if agg_res else {}
    
    working_days = count_working_days(month, emp["joined_on"])
    present_days = float(stats.get("present_days", 0.0))
    leave_days = int(stats.get("leave_days", 0))
    late_count = int(stats.get("late_count", 0))
    total_late_minutes = int(stats.get("total_late_minutes", 0))
    total_overtime_minutes = int(stats.get("total_overtime_minutes", 0))
    
    if working_days > 0:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(raw_pct, 2)
    else:
        attendance_pct = None
    
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    # Last day of the month for joined_on check
    y, m = map(int, month.split("-"))
    if m == 12:
        last_day = dt_date(y + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = dt_date(y, m + 1, 1) - timedelta(days=1)
    month_end_str = last_day.isoformat()
    
    match_emp = {"joined_on": {"$lte": month_end_str}}
    if department is not None:
        match_emp["department"] = department
    
    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}"}}
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$logs.dow", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0.0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                }
            }
        },
        {"$sort": {"_id": 1}}
    ]


# 9. GET /analytics/departments/summary
@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    
    items = []
    for r in results:
        headcount = len(r.get("headcount_set", []))
        if headcount == 0:
            continue
        
        wh_count = r.get("work_hours_count", 0)
        wh_sum = r.get("work_hours_sum", 0.0)
        avg_wh = round_half_up(wh_sum / wh_count, 2) if wh_count > 0 else None
        
        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": float(r.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    
    return {"month": month, "items": items}


def build_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    emp_match = {}
    if department:
        emp_match["emp.department"] = department

    pipeline = [
        {
            "$match": {
                "date": {"$regex": f"^{month}"},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]
    if emp_match:
        pipeline.append({"$match": emp_match})
    
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}}
    ])
    return pipeline


# 10. GET /analytics/leaderboard/late
@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = build_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))
    
    items = []
    for r in results:
        items.append({
            "rank": r["rank"],
            "emp_code": r["_id"],
            "name": r["emp"]["name"],
            "department": r["emp"]["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        })
    
    return {"month": month, "items": items}


def build_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    from_y, from_m, from_d = map(int, from_date.split("-"))
    to_y, to_m, to_d = map(int, to_date.split("-"))
    from_dt = datetime(from_y, from_m, from_d, 0, 0, 0, tzinfo=timezone.utc)
    to_dt = datetime(to_y, to_m, to_d, 0, 0, 0, tzinfo=timezone.utc)
    to_dt_plus_one = to_dt + timedelta(days=1)
    
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": None,
                "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
                "all_logs": {
                    "$push": "$logs"
                }
            }
        },
        {
            "$project": {
                "employees": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "employees": 1,
                "log_date": {"$ifNull": ["$logs.date", from_date]},
                "status": "$logs.status",
                "half_day": "$logs.half_day",
                "late_minutes": "$logs.late_minutes",
                "has_log": {"$cond": [{"$ne": ["$logs", None]}, 1, 0]}
            }
        },
        {
            "$group": {
                "_id": "$log_date",
                "employees": {"$first": "$employees"},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$has_log", 1]}, {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$has_log", 1]}, {"$gt": ["$late_minutes", 0]}]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "employees": 1,
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        from_dt,
                        to_dt_plus_one
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "emp_list": {"$first": "$employees", "window": {"documents": ["unbounded", "unbounded"]}}
                }
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dow": {"$dayOfWeek": "$date_dt"}
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": {"$ifNull": ["$emp_list", []]},
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]}
                    }
                }
            }
        },
        {"$sort": {"date": 1}}
    ]


# 11. GET /analytics/departments/{department}/trend
@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date cannot be earlier than 'from' date"
        )
    
    d_from = dt_date.fromisoformat(from_date)
    d_to = dt_date.fromisoformat(to_date)
    if (d_to - d_from).days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days"
        )
    
    # 404 if department has no employees
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Department '{department}' not found"
        )
    
    pipeline = build_trend_pipeline(department, from_date, to_date)
    results = list(db.employees.aggregate(pipeline))
    
    items = []
    for r in results:
        rate = r.get("attendance_rate")
        rounded_rate = round_half_up(rate, 4) if rate is not None else None
        
        m_avg = r.get("moving_avg_7d")
        rounded_m_avg = round_half_up(m_avg, 4) if m_avg is not None else None
        
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": rounded_rate,
            "moving_avg_7d": rounded_m_avg,
        })
    
    return {"department": department, "items": items}


# 12. GET /admin/explain/{endpoint}
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter
        
        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
        explain_output = cursor.explain()
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_output
        }
    
    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(explain_cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": res
        }
    
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(explain_cmd)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": res
        }
    
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        pipeline = build_leaderboard_pipeline(month, limit, department)
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(explain_cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": res
        }
    
    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' are required for department_trend")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' cannot be earlier than 'from'")
        pipeline = build_trend_pipeline(department, from_date, to_date)
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(explain_cmd)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": res
        }
