import os
import sys
from pathlib import Path

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    init_indexes,
    db,
    round_half_up,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    epoch_ms_to_datetime_utc,
    determine_attendance_date,
    count_working_days
)

init_indexes()
client = TestClient(app)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_business_rules_unit():
    # R8 Rounding half-up (numbers 2 decimals, rates 4 decimals)
    assert round_half_up(9.125, 2) == 9.13
    assert round_half_up(9.124, 2) == 9.12
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234
    assert round_half_up(2.505, 2) == 2.51

    # R1 Attendance Date & Overnight shift
    # Overnight shift: 22:00 - 06:00
    # Punch in at 2026-07-07 05:30:00 IST (before shift_end 06:00) -> belongs to 2026-07-06
    # 2026-07-07 05:30:00 IST = 2026-07-07 00:00:00 UTC = 1783382400000 ms
    dt_early_morning = epoch_ms_to_datetime_utc(1783382400000)
    assert determine_attendance_date(dt_early_morning, "22:00", "06:00") == "2026-07-06"

    # Punch in at 2026-07-07 06:05:00 IST (after shift_end 06:00) -> belongs to 2026-07-07
    dt_morning_after = epoch_ms_to_datetime_utc(1783382400000 + 35 * 60 * 1000)
    assert determine_attendance_date(dt_morning_after, "22:00", "06:00") == "2026-07-07"

    # R2 Late minutes
    # Shift 09:30: 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45
    # 2026-07-06 09:40:00 IST is 04:10:00 UTC = 1783311000000 ms
    dt_094000 = epoch_ms_to_datetime_utc(1783311000000)
    assert compute_late_minutes(dt_094000, "09:30", "2026-07-06") == 0

    dt_094001 = epoch_ms_to_datetime_utc(1783311001000)
    assert compute_late_minutes(dt_094001, "09:30", "2026-07-06") == 10

    # 10:15:59 IST = 04:45:59 UTC = 1783311000000 + 35*60 + 59 = 1783313159000 ms
    # difference from 09:30 is 45 min 59 sec -> whole minutes = 45
    dt_101559 = epoch_ms_to_datetime_utc(1783313159000)
    assert compute_late_minutes(dt_101559, "09:30", "2026-07-06") == 45

    # R3 Overtime
    # Shift 18:30. Punch out 18:59:59 -> 0 (< 30 min); 19:00:00 -> 30; 19:10:00 -> 40
    # 2026-07-06 19:00:00 IST is 13:30:00 UTC = 1783344600000 ms
    dt_190000 = epoch_ms_to_datetime_utc(1783344600000)
    assert compute_overtime_minutes(dt_190000, "09:30", "18:30", "2026-07-06") == 30

    dt_185900 = epoch_ms_to_datetime_utc(1783344540000)
    assert compute_overtime_minutes(dt_185900, "09:30", "18:30", "2026-07-06") == 0

    # Overnight overtime: 22:00 - 06:00 on date 2026-07-06 (shift end is 2026-07-07 06:00 IST)
    # Punch out at 2026-07-07 06:40 IST = 01:10 UTC = 1783386600000 ms -> 40 min overtime
    dt_overnight_out = epoch_ms_to_datetime_utc(1783386600000)
    assert compute_overtime_minutes(dt_overnight_out, "22:00", "06:00", "2026-07-06") == 40

    # R4 Work hours
    # 9 hours 5 minutes = 9.08 hours
    assert compute_work_hours(epoch_ms_to_datetime_utc(0), epoch_ms_to_datetime_utc(32700 * 1000)) == 9.08

    # R7 Working days: July 2026 has 23 weekdays (Mon-Fri)
    # Employee joined 2026-07-13 -> weekdays from July 13 to July 31 = 15 weekdays
    assert count_working_days("2026-07", "2026-01-05") == 23
    assert count_working_days("2026-07", "2026-07-13") == 15


def test_crud_employees():
    emp_payload = {
        "emp_code": "EMP9999",
        "name": "Test Tester",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01"
    }
    db.employees.delete_one({"emp_code": "EMP9999"})
    
    # Create
    res = client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert "_id" not in data
    assert "created_at" in data
    assert isinstance(data["created_at"], int)

    # Shift start equal to shift end -> 422
    invalid_shift_payload = dict(emp_payload, emp_code="EMP9998", shift_start="10:00", shift_end="10:00")
    assert client.post("/employees", json=invalid_shift_payload).status_code == 422

    # Duplicate create -> 409
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # List employees with department filter and pagination
    res_list = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["page"] == 1
    assert any(e["emp_code"] == "EMP9999" for e in list_data["items"])


def test_punch_in_out_and_concurrency():
    emp_code = "EMP9999"
    punch_ms = 1785556800000  # 2026-08-01 09:30 IST (04:00 UTC)
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": "2026-08-01"})

    req = {
        "emp_code": emp_code,
        "punched_at": punch_ms,
        "status": "PRESENT"
    }
    res = client.post("/attendance/punch-in", json=req)
    assert res.status_code == 201
    rec = res.json()
    assert rec["date"] == "2026-08-01"
    assert rec["status"] == "PRESENT"
    assert rec["punch_in"] == punch_ms
    assert rec["punch_out"] is None
    assert rec["work_hours"] is None
    assert "_id" not in rec

    # Duplicate punch-in for same date -> 409
    res_dup = client.post("/attendance/punch-in", json=req)
    assert res_dup.status_code == 409

    # Unknown employee punch in -> 404
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0000"}).status_code == 404

    # Invalid punch out time (before punch-in) -> 422
    assert client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": punch_ms - 1000}).status_code == 422

    # Invalid punch out time (> 24 hours after punch-in) -> 422
    assert client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": punch_ms + 25 * 3600 * 1000}).status_code == 422

    # Punch out at 18:30 IST (9.00 hours work)
    out_ms = punch_ms + 9 * 3600 * 1000
    res_out = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms})
    assert res_out.status_code == 200
    out_rec = res_out.json()
    assert out_rec["punch_out"] == out_ms
    assert out_rec["work_hours"] == 9.0
    assert out_rec["half_day"] is False

    # Second punch out -> 409 already punched out
    res_out2 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms + 1000})
    assert res_out2.status_code == 409


def test_regularize_and_audit_history():
    emp_code = "EMP9999"
    date_str = "2026-08-01"

    # Regularize punch_in by 1 minute
    patch_req = {
        "punch_in": 1785556800000 - 60000,
        "reason": "gate scanner delay",
        "regularized_by": "hr_manager"
    }
    res = client.patch(f"/attendance/{emp_code}/{date_str}", json=patch_req)
    assert res.status_code == 200
    data = res.json()
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr_manager"
    assert h["reason"] == "gate scanner delay"
    assert "punch_in" in h["changes"]
    assert h["changes"]["punch_in"]["to"] == 1785556800000 - 60000

    # Changing nothing -> 422
    res_no_change = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "reason": "no changes really",
        "regularized_by": "hr_manager"
    })
    assert res_no_change.status_code == 422

    # Supplying punch times with status ABSENT or LEAVE -> 422
    res_invalid_absent = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "status": "ABSENT",
        "punch_in": 1785556800000,
        "reason": "marked absent but sent punch in",
        "regularized_by": "hr_manager"
    })
    assert res_invalid_absent.status_code == 422


def test_analytics_endpoints():
    # Employee monthly for EMP0001
    res_m = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_m.status_code == 200
    m_data = res_m.json()
    assert m_data["emp_code"] == "EMP0001"
    assert m_data["working_days"] == 23
    assert m_data["present_days"] >= 1.0

    # Department summary
    res_d = client.get("/analytics/departments/summary?month=2026-07")
    assert res_d.status_code == 200
    d_data = res_d.json()
    assert d_data["month"] == "2026-07"
    depts = {item["department"]: item for item in d_data["items"]}
    assert "Engineering" in depts
    # Check headcount counts employees with zero logs
    assert depts["Engineering"]["headcount"] >= 2

    # Leaderboard with competition ranking and tie handling
    res_l = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res_l.status_code == 200
    l_data = res_l.json()
    assert isinstance(l_data["items"], list)
    for entry in l_data["items"]:
        assert entry["rank"] >= 1
        assert entry["total_late_minutes"] > 0

    # Department trend (5 days)
    res_t = client.get("/analytics/departments/Sales/trend?from=2026-07-06&to=2026-07-10")
    assert res_t.status_code == 200
    t_data = res_t.json()
    assert len(t_data["items"]) == 5
    for item in t_data["items"]:
        assert "date" in item
        assert "attendance_rate" in item
        assert "moving_avg_7d" in item


def test_explain_endpoints():
    for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        params = {}
        if ep == "employee_monthly":
            params = {"emp_code": "EMP0001", "month": "2026-07"}
        elif ep == "department_summary":
            params = {"month": "2026-07"}
        elif ep == "late_leaderboard":
            params = {"month": "2026-07"}
        elif ep == "department_trend":
            params = {"department": "Sales", "from": "2026-07-06", "to": "2026-07-10"}
        
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200
        data = res.json()
        assert data["endpoint"] == ep
        assert "explain" in data
