"""
Comprehensive automated tests for Employee Attendance & Analytics API.
"""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
import os

from app.main import app, db, init_indexes

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    init_indexes()
    # Reset collections for clean test state
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    init_indexes()


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_crud_and_validation():
    # Invalid emp_code format
    res = client.post("/employees", json={
        "emp_code": "BADCODE",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Equal shift_start and shift_end -> 422
    res = client.post("/employees", json={
        "emp_code": "EMP1001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Successful creation
    res = client.post("/employees", json={
        "emp_code": "EMP1001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP1001"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

    # Duplicate emp_code -> 409
    res = client.post("/employees", json={
        "emp_code": "EMP1001",
        "name": "Duplicate User",
        "email": "dup@example.com",
        "department": "Sales",
        "joined_on": "2026-02-01"
    })
    assert res.status_code == 409

    # Add second employee
    client.post("/employees", json={
        "emp_code": "EMP1002",
        "name": "Vikram Shah",
        "email": "vikram@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-13"
    })

    # List employees
    res = client.get("/employees?page=1&page_size=20")
    assert res.status_code == 200
    list_data = res.json()
    assert list_data["total"] == 2
    assert len(list_data["items"]) == 2
    assert list_data["items"][0]["emp_code"] == "EMP1001"
    assert list_data["items"][1]["emp_code"] == "EMP1002"

    # Filter by department
    res = client.get("/employees?department=Engineering")
    assert res.status_code == 200
    assert res.json()["total"] == 2

    res = client.get("/employees?department=HR")
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_punch_in_rules():
    # Seed employee
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # 404 for unknown employee
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404

    # R2 Late: 09:40:00 -> 0 minutes late (inside grace)
    # 2026-07-06 09:40:00 IST is 2026-07-06 04:10:00 UTC
    dt_094000 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    ms_094000 = int(dt_094000.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_094000
    })
    assert res.status_code == 201
    doc = res.json()
    assert doc["date"] == "2026-07-06"
    assert doc["late_minutes"] == 0
    assert doc["punch_out"] is None
    assert doc["work_hours"] is None
    assert doc["history"] == []
    assert "_id" not in doc
    assert "id" not in doc

    # Duplicate punch-in for same date -> 409
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_094000
    })
    assert res.status_code == 409


def test_late_minutes_strictly_more_than_10():
    # Seed employee
    client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Vikram Shah",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # R2: 09:40:01 IST -> 10 minutes late!
    # 2026-07-07 09:40:01 IST is 2026-07-07 04:10:01 UTC
    dt_094001 = datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc)
    ms_094001 = int(dt_094001.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": ms_094001
    })
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 10

    # Next date: 10:15:59 IST -> 45 minutes late!
    # 2026-07-08 10:15:59 IST is 2026-07-08 04:45:59 UTC
    dt_101559 = datetime(2026, 7, 8, 4, 45, 59, tzinfo=timezone.utc)
    ms_101559 = int(dt_101559.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": ms_101559
    })
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 45


def test_overnight_shift_punch_in():
    # Overnight employee: 22:00 to 06:00
    client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Meera Nair",
        "email": "meera@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-03-10"
    })

    # Punch in at 05:30 IST on 2026-07-07 (earlier than shift_end 06:00)
    # Per R1: belongs to shift starting on 2026-07-06!
    # 05:30 IST is 00:00 UTC on 2026-07-07
    dt_0530 = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    ms_0530 = int(dt_0530.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": ms_0530
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


def test_punch_out_and_work_hours():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Punch in at 09:28 IST (03:58 UTC) on 2026-07-06
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp() * 1000)
    })
    assert res.status_code == 201

    # Punch out 5 min after shift end: 18:35 IST (13:05 UTC)
    # Duration: 9h 7m = 32820s / 3600 = 9.11666 -> 9.12 work hours
    # Overtime: 5 minutes < 30 minutes -> 0 overtime
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res.status_code == 200
    doc = res.json()
    assert doc["work_hours"] == 9.12
    assert doc["overtime_minutes"] == 0
    assert doc["half_day"] is False

    # Second punch out -> 409
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res.status_code == 409


def test_half_day_and_overtime():
    client.post("/employees", json={
        "emp_code": "EMP0004",
        "name": "Rohit Das",
        "email": "rohit@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01"
    })

    # 3.5 hours work: 09:30 IST to 13:00 IST (04:00 UTC to 07:30 UTC)
    in_dt = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc)

    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0004",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "WFH"
    })
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0004",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res.status_code == 200
    doc = res.json()
    assert doc["work_hours"] == 3.5
    assert doc["half_day"] is True
    assert doc["overtime_minutes"] == 0


def test_regularize_patch():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Punch in late at 10:05 IST (04:35 UTC) on 2026-07-07 (late 35 min)
    in_dt = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp() * 1000)
    })
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })

    # Correct punch-in to 09:28 IST (03:58 UTC)
    corr_in = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
    patch_body = {
        "punch_in": int(corr_in.timestamp() * 1000),
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    }
    res = client.patch("/attendance/EMP0001/2026-07-07", json=patch_body)
    assert res.status_code == 200
    doc = res.json()
    assert doc["late_minutes"] == 0
    assert doc["work_hours"] == 9.03
    assert len(doc["history"]) == 1
    h = doc["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 35
    assert h["changes"]["late_minutes"]["to"] == 0

    # Changing nothing -> 422
    res = client.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": int(corr_in.timestamp() * 1000),
        "reason": "again same",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 422


def test_monthly_summary():
    # Employee joined 2026-07-13 (mid-month)
    # Mon-Fri from July 13 to July 31 2026 = 15 working days
    client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Vikram Shah",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-13"
    })

    # Punch in on Tuesday 2026-07-14 (Mon-Fri)
    in_dt = datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": int(in_dt.timestamp() * 1000)
    })
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": int(out_dt.timestamp() * 1000)
    })

    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 15
    assert data["present_days"] == 1.0
    assert data["attendance_pct"] == 6.67  # 1/15 * 100 = 6.666... -> 6.67


def test_leaderboard_competition_ranking():
    # Seed 3 employees in Sales
    for i in range(1, 4):
        client.post("/employees", json={
            "emp_code": f"EMP00{i}0",
            "name": f"User {i}",
            "email": f"u{i}@example.com",
            "department": "Sales",
            "joined_on": "2026-01-01"
        })

    # Insert logs directly with ties
    # EMP0010: late 40 min
    # EMP0020: late 40 min (tied with EMP0010)
    # EMP0030: late 20 min
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0010",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 40,
            "overtime_minutes": 0,
            "history": []
        },
        {
            "emp_code": "EMP0020",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 40,
            "overtime_minutes": 0,
            "history": []
        },
        {
            "emp_code": "EMP0030",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 20,
            "overtime_minutes": 0,
            "history": []
        }
    ])

    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    # Competition ranking: rank 1, rank 1, rank 3!
    assert items[0]["rank"] == 1
    assert items[0]["emp_code"] == "EMP0010"
    assert items[1]["rank"] == 1
    assert items[1]["emp_code"] == "EMP0020"
    assert items[2]["rank"] == 3
    assert items[2]["emp_code"] == "EMP0030"


def test_department_trend_gap_filling():
    client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Neha Iyer",
        "email": "neha@example.com",
        "department": "Sales",
        "joined_on": "2026-01-01"
    })

    # 1 log on July 6 (Monday)
    db.attendance_logs.insert_one({
        "emp_code": "EMP0003",
        "date": "2026-07-06",
        "status": "PRESENT",
        "half_day": False,
        "late_minutes": 15,
        "overtime_minutes": 0,
        "history": []
    })

    # Query trend from July 1 to July 10 (10 days)
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 10
    dates = [x["date"] for x in items]
    assert dates[0] == "2026-07-01"
    assert dates[-1] == "2026-07-10"

    # July 4 (Saturday) should be is_working_day = False and attendance_rate = None
    jul4 = next(x for x in items if x["date"] == "2026-07-04")
    assert jul4["is_working_day"] is False
    assert jul4["attendance_rate"] is None

    # July 6 (Monday) has present_count = 1.0, headcount = 1, rate = 1.0
    jul6 = next(x for x in items if x["date"] == "2026-07-06")
    assert jul6["is_working_day"] is True
    assert jul6["present_count"] == 1.0
    assert jul6["attendance_rate"] == 1.0


def test_explain_endpoints():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    # Test all 5 explain endpoints
    endpoints_to_test = [
        ("attendance_list", "/admin/explain/attendance_list?page=1&page_size=20"),
        ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "/admin/explain/department_summary?month=2026-07"),
        ("late_leaderboard", "/admin/explain/late_leaderboard?month=2026-07"),
        ("department_trend", "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10"),
    ]

    for ep_name, url in endpoints_to_test:
        res = client.get(url)
        assert res.status_code == 200, f"Explain failed for {ep_name}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep_name
        assert "collection" in data
        assert "explain" in data
