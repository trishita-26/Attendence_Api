"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import calendar
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query 
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import PyMongoError, DuplicateKeyError
from datetime import date,datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from bson import json_util
import json 

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True  #prevent duplicate employee codes and duplicate attendence record for same employee
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)]
    )

    db.attendance_logs.create_index(  # create_index is safe to run repeatedly with same index specification, making it suitable for application startup
        [("date", 1), ("status", 1)]
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def as_ist(value: datetime) -> datetime:
    """Convert a datetime from MongoDB or Python to IST."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).replace(microsecond=0)


def parse_timestamp(timestamp: Optional[int]) -> datetime:
    """Epoch milliseconds -> IST datetime; None means now."""
    if timestamp is None:
        return datetime.now(timezone.utc).astimezone(IST).replace(microsecond=0)
    return epoch_ms_to_datetime(timestamp)


def get_shift_bounds(punch_in: datetime, shift_start: str, shift_end: str):
    """Return the scheduled start and end datetimes for this shift."""
    punch_in = as_ist(punch_in)

    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    overnight = (end_hour, end_minute) <= (start_hour, start_minute)

    shift_date = punch_in.date()

    # An early-morning punch-in belongs to the previous day's overnight shift.
    if overnight and (punch_in.hour, punch_in.minute) < (end_hour, end_minute):
        shift_date -= timedelta(days=1)

    shift_start_dt = datetime(
        shift_date.year, shift_date.month, shift_date.day,
        start_hour, start_minute, tzinfo=IST
    )

    end_date = shift_date + timedelta(days=1) if overnight else shift_date
    shift_end_dt = datetime(
        end_date.year, end_date.month, end_date.day,
        end_hour, end_minute, tzinfo=IST
    )

    return shift_start_dt, shift_end_dt


def compute_late_minutes(punch_in, shift_start, shift_end):
    punch_in = as_ist(punch_in)
    start_dt, _ = get_shift_bounds(punch_in, shift_start, shift_end)

    seconds_late = (punch_in - start_dt).total_seconds()

    # Strictly more than 10 minutes late.
    if seconds_late > 10 * 60:
        return int(seconds_late // 60)

    return 0


def compute_work_hours(punch_in, punch_out):
    punch_in = as_ist(punch_in)
    punch_out = as_ist(punch_out)

    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(punch_in, punch_out, shift_start, shift_end):
    punch_in = as_ist(punch_in)
    punch_out = as_ist(punch_out)

    _, scheduled_end = get_shift_bounds(
        punch_in, shift_start, shift_end
    )

    seconds_after_shift = (punch_out - scheduled_end).total_seconds()
    whole_minutes = int(seconds_after_shift // 60)

    return whole_minutes if whole_minutes >= 30 else 0


def epoch_ms_to_datetime(value: int) -> datetime:
    """Validate epoch milliseconds and truncate to whole seconds."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 100_000_000_000
        or value > 4_102_444_800_000
    ):
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be a valid epoch-milliseconds integer",
        )

    # Drop milliseconds before any calculations.
    return datetime.fromtimestamp(
        value // 1000, tz=timezone.utc
    ).astimezone(IST)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a stored datetime to epoch milliseconds."""
    if value is None:
        return None

    normalized = as_ist(value)
    return int(normalized.timestamp()) * 1000


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum, rounding=ROUND_HALF_UP
        )
    )


def count_weekdays(start_date, end_date):
    """Count Monday-Friday dates, including both endpoints."""
    if start_date > end_date:
        return 0

    days = (end_date - start_date).days + 1
    weeks, remainder = divmod(days, 7)
    count = weeks * 5

    for offset in range(remainder):
        weekday = (start_date.weekday() + offset) % 7
        if weekday < 5:
            count += 1

    return count


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class AttendanceRegularizationIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )
  # for previous one the health always return ok but with this it points out if the database is connected or not 


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    try:
        date.fromisoformat(body.joined_on)
    except ValueError:
        raise HTTPException(422, "joined_on must be YYYY-MM-DD")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return doc

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422, detail="page_size must be between 1 and 100"
        )

    q = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    for item in items:
        item["created_at"] = datetime_to_epoch_ms(item.get("created_at"))

    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(status_code=422, detail="Invalid punch-in status")


    ts = parse_timestamp(body.punched_at)

    shift_start_dt, _ = get_shift_bounds(
        ts, emp["shift_start"], emp["shift_end"]
    )
    date_str = shift_start_dt.date().isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
    ts,
    emp["shift_start"],
    emp["shift_end"]
),

        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        )

    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)
    return doc



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
  
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )

    # Validate date range (ISO dates: YYYY-MM-DD)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    # Build MongoDB filters
    q = {}

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

    # Count all matching records, not just this page
    total = db.attendance_logs.count_documents(q)

    # Let MongoDB sort and paginate
    skip = (page - 1) * page_size

    page_docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    # Convert MongoDB ObjectId to a JSON-friendly string
    for d in page_docs:
        d["id"] = str(d.pop("_id"))

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = parse_timestamp(body.punched_at)

    # Find the most recent punch-in at or before the requested punch-out.
    record = db.attendance_logs.find_one(
    {
        "emp_code": body.emp_code,
        "punch_in": {"$ne": None, "$lte": punched_at},
    },
    sort=[("punch_in", -1)],
)

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found before this punch-out",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already punched out",
        )

    punch_in_time = as_ist(record["punch_in"])

    if punched_at <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    if punched_at > punch_in_time + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = compute_work_hours(punch_in_time, punched_at)
    overtime_minutes = compute_overtime(
        punch_in_time,
        punched_at,
        emp["shift_start"],
        emp["shift_end"],
    )
    half_day = work_hours < 4.50

    # Conditional update prevents two simultaneous requests from both succeeding.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already punched out",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated["id"] = str(updated.pop("_id"))

    return updated


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: AttendanceRegularizationIn,
):
    # Validate the attendance date.
    try:
        requested_date = datetime.strptime(date, "%Y-%m-%d").date()
        if requested_date.isoformat() != date:
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date; use YYYY-MM-DD")

    # Validate the employee and the record.
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Unknown employee")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Distinguish an omitted field from one explicitly supplied as null.
    supplied = body.model_fields_set
    if "punch_in" in supplied and body.punch_in is None:
        raise HTTPException(status_code=422, detail="punch_in cannot be null")
    if "punch_out" in supplied and body.punch_out is None:
        raise HTTPException(status_code=422, detail="punch_out cannot be null")

    valid_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    if "status" in supplied and body.status not in valid_statuses:
        raise HTTPException(status_code=422, detail="Invalid status")

    old_status = record.get("status", "PRESENT")
    old_punch_in_raw = record.get("punch_in")
    old_punch_out_raw = record.get("punch_out")

    old_punch_in = (
        as_ist(old_punch_in_raw) if old_punch_in_raw is not None else None
    )
    old_punch_out = (
        as_ist(old_punch_out_raw) if old_punch_out_raw is not None else None
    )

    # Apply supplied values; omitted fields remain unchanged.
    new_status = body.status if "status" in supplied else old_status
    new_punch_in = (
        epoch_ms_to_datetime(body.punch_in)
        if "punch_in" in supplied
        else old_punch_in
    )
    new_punch_out = (
        epoch_ms_to_datetime(body.punch_out)
        if "punch_out" in supplied
        else old_punch_out
    )

    # ABSENT and LEAVE clear punches, but callers cannot supply punches
    # in the same request that sets either status.
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="Do not supply punch times with ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None

    # A presence status requires a punch-in.
    if new_status in {"PRESENT", "WFH", "ON_DUTY"} and new_punch_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires punch_in",
        )

    if new_punch_in is not None:
        shift_start_dt, _ = get_shift_bounds(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if shift_start_dt.date() != requested_date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must fall on the attendance date in IST",
            )
    # Validate the final punch times together.
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out requires punch_in",
            )
        if new_punch_out <= new_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )
        if new_punch_out > new_punch_in + timedelta(hours=24):
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot be more than 24 hours after punch_in",
            )

    # Recompute all derived fields from the final values.
    late_minutes = (
        compute_late_minutes(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if new_punch_in is not None
        else 0
    )

    work_hours = (
        compute_work_hours(new_punch_in, new_punch_out)
        if new_punch_in is not None and new_punch_out is not None
        else None
    )

    overtime_minutes = (
        compute_overtime(
            new_punch_in,
            new_punch_out,
            emp["shift_start"],
            emp["shift_end"],
        )
        if new_punch_in is not None and new_punch_out is not None
        else 0
    )

    half_day = work_hours is not None and work_hours < 4.50

    # Convert times to the representation required inside audit history.
    old_values = {
        "status": old_status,
        "punch_in": datetime_to_epoch_ms(old_punch_in),
        "punch_out": datetime_to_epoch_ms(old_punch_out),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": new_status,
        "punch_in": datetime_to_epoch_ms(new_punch_in),
        "punch_out": datetime_to_epoch_ms(new_punch_out),
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {
        field: {"from": old_values[field], "to": new_values[field]}
        for field in old_values
        if old_values[field] != new_values[field]
    }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The request does not change this attendance record",
        )

    # Keep the original stored values in the conditional filter.
    # Only one concurrent correction based on this version can succeed.
    history = record.get("history", [])
    history_entry = {
        "at": int(datetime.now(timezone.utc).timestamp()) * 1000,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": old_punch_in_raw,
            "punch_out": old_punch_out_raw,
            "history": history,
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the correction",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated.pop("_id", None)

    # API responses use epoch milliseconds, not datetime strings.
    updated["punch_in"] = datetime_to_epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = datetime_to_epoch_ms(updated.get("punch_out"))

    return updated

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate YYYY-MM.
    try:
        month_start = date.fromisoformat(month + "-01")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown employee",
        )

    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(
            month_start.year, month_start.month
        )[1],
    )

    joined_on = date.fromisoformat(employee["joined_on"])
    effective_start = max(month_start, joined_on)

    working_days = count_weekdays(effective_start, month_end)

    # Aggregate attendance in MongoDB.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_attendance_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$addFields": {
                "_weekday": {
                    "$dayOfWeek": "$_attendance_date"
                },
                "_present_credit": {
                    "$cond": [
                        {
                            "$in": [
                                "$status",
                                ["PRESENT", "WFH", "ON_DUTY"],
                            ]
                        },
                        {
                            "$cond": [
                                {"$eq": ["$half_day", True]},
                                0.5,
                                1.0,
                            ]
                        },
                        0.0,
                    ]
                },
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
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]},
                                ]
                            },
                            "$_present_credit",
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$ifNull": ["$overtime_minutes", 0]
                    }
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    stats = results[0] if results else {}

    present_days = float(stats.get("present_days", 0))
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get(
            "total_overtime_minutes", 0
        ),
        "attendance_pct": attendance_pct,
    }



@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    # Validate YYYY-MM.
    try:
        month_start = date.fromisoformat(month + "-01")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    )
    employee_match = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    employee_pipeline = [
        {"$match": employee_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    departments = list(db.employees.aggregate(employee_pipeline))

    if department is not None:
        employee_pipeline.insert(
            0, {"$match": {"department": department}}
        )

    departments = list(db.employees.aggregate(employee_pipeline))

    # Aggregate attendance statistics in MongoDB.
    attendance_pipeline = [
        {
        "$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }
    },
    {"$unwind": "$employee"},
    {"$addFields": {"_department": "$employee.department"}},
        {
            "$group": {
                "_id": "$_department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$status",
                                    ["PRESENT", "WFH", "ON_DUTY"]
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {"$ne": ["$work_hours", None]}
                                ]
                            },
                            "$work_hours",
                            None
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        }
    ]

    attendance_stats = {
        item["_id"]: item
        for item in db.attendance_logs.aggregate(attendance_pipeline)
        if item["_id"] is not None
    }

    result = []

    for dept in departments:
        name = dept["_id"]
        stats = attendance_stats.get(name, {})

        result.append({
            "department": name,
            "headcount": dept["headcount"],
            "present_days": round_half_up(
                stats.get("present_days", 0), 2
            ),
            "avg_work_hours": (
                round_half_up(stats["avg_work_hours"], 2)
                if stats.get("avg_work_hours") is not None
                else None
            ),
            "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get("total_late_minutes", 0),
            "leave_count": stats.get("leave_count", 0),
            "on_duty_count": stats.get("on_duty_count", 0)
        })

    return result


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None
):
    # Validate month format.
    try:
        month_start = date.fromisoformat(month + "-01")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format"
        )

    # Validate limit.
    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50"
        )

    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1]
    )

    # Aggregate attendance records and join employee details.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat()
                },
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        # Removes logs whose employee does not exist.
        {"$unwind": "$employee"}
    ]

    # Apply department filter before calculating ranks.
    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department
            }
        })

    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        # Apply limit after ranking so ties are included.
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to")
):
    # Validate the requested date range.
    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'"
        )

    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days"
        )

    # A department must have at least one employee.
    if db.employees.find_one(
        {"department": department},
        {"_id": 1}
    ) is None:
        raise HTTPException(
            status_code=404,
            detail="Department not found"
        )
    dept_codes = db.employees.distinct("emp_code", {"department": department})
    
    start_dt = datetime.combine(
        from_date, datetime.min.time(), tzinfo=timezone.utc
    )
    end_dt = datetime.combine(
        to_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc
    )

    pipeline = [
       
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "day": {
                    "$dateFromString": {
                        "dateString": from_date.isoformat()
                    }
                }
            }
        },
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt]
                }
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day"
                    }
                },
                "weekday": {"$isoDayOfWeek": "$day"}
            }
        },

        # Calculate attendance statistics for each calendar day.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"target_date": "$date"},
                "pipeline": [
                   {
    "$match": {
        "$expr": {"$eq": ["$date", "$$target_date"]},
        "emp_code": {"$in": dept_codes},
    }
},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"]
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1.0
                                            ]
                                        },
                                        0.0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "attendance_stats"
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$attendance_stats.present_count", 0
                            ]
                        },
                        0.0
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$attendance_stats.late_count", 0
                            ]
                        },
                        0
                    ]
                }
            }
        },

        # Count department employees who have joined by this date.
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "target_date": "$date",
                    "target_department": department
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$department",
                                            "$$target_department"
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$joined_on",
                                            "$$target_date"
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"}
                ],
                "as": "employee_stats"
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$employee_stats.headcount", 0
                            ]
                        },
                        0
                    ]
                },
                "is_working_day": {"$lte": ["$weekday", 5]}
            }
        },

        # Attendance rate rounded half-up to four decimal places.
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            {
                                                                "$cond": [
                                                                    {"$gt": ["$headcount", 0]},
                                                                    "$headcount",
                                                                    1
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    10000
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                10000
                            ]
                        },
                        None
                    ]
                }
            }
        },

        # The current date plus the six preceding dates in this range.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },

        # Round the moving average half-up to four decimal places.
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_raw", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_raw",
                                                    10000
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                10000
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str):
    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    }

    if endpoint not in supported:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported endpoint. Choose one of: {sorted(supported)}"
        )

    # Explain the main indexed read associated with each endpoint.
    if endpoint == "attendance_list":
        collection = db.attendance_logs
        command = {
            "find": collection.name,
            "filter": {"date": {"$gte": "2026-10-01"}},
            "sort": {"date": -1, "emp_code": 1},
            "limit": 20
        }

    elif endpoint == "employee_monthly":
        collection = db.attendance_logs
        command = {
            "find": collection.name,
            "filter": {
                "emp_code": "EMP0001",
                "date": {
                    "$gte": "2026-10-01",
                    "$lte": "2026-10-31"
                }
            }
        }

    elif endpoint == "department_summary":
        collection = db.employees
        command = {
            "find": collection.name,
            "filter": {"department": {"$exists": True}},
            "sort": {"department": 1}
        }

    elif endpoint == "late_leaderboard":
        collection = db.attendance_logs
        command = {
            "find": collection.name,
            "filter": {
                "date": {
                    "$gte": "2026-10-01",
                    "$lte": "2026-10-31"
                },
                "late_minutes": {"$gt": 0}
            }
        }

    else:  # department_trend
        collection = db.employees
        command = {
            "find": collection.name,
            "filter": {
                "department": "Engineering",
                "joined_on": {"$lte": "2026-10-31"}
            }
        }

    plan = db.command(
        "explain",
        command,
        verbosity="queryPlanner"
    )

    # Convert BSON-specific values into JSON-compatible values.
    return json.loads(json_util.dumps(plan))

