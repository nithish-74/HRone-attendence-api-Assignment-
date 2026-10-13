"""
Tests verifying API endpoints against the seeded sample dataset.
"""
import pathlib
import pytest
from bson import json_util
from fastapi.testclient import TestClient

from app.main import app, db, init_indexes

client = TestClient(app)


@pytest.fixture(autouse=True)
def seed_sample_docs():
    init_indexes()
    here = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)
    init_indexes()


def test_sample_employees():
    res = client.get("/employees")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 6
    assert len(data["items"]) == 6
    codes = [e["emp_code"] for e in data["items"]]
    assert codes == ["EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005", "EMP0006"]


def test_sample_attendance():
    res = client.get("/attendance")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 9
    assert len(data["items"]) == 9


def test_sample_monthly_summary():
    # EMP0001 (joined 2026-01-05): 23 working days in July 2026
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0  # EMP0001 has 2 PRESENT records (2026-07-06 and 2026-07-07)


def test_sample_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    depts = {d["department"]: d for d in data["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts
    assert depts["Engineering"]["headcount"] == 2
    assert depts["Sales"]["headcount"] == 2
    assert depts["Support"]["headcount"] == 2


def test_sample_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    items = res.json()["items"]
    # Only EMP0003 has late minutes > 0 (35 min)
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP0003"
    assert items[0]["total_late_minutes"] == 35
    assert items[0]["rank"] == 1


def test_sample_department_trend():
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 10
