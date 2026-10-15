"""Expected-vs-actual output checker for the Employee Attendance & Analytics API.

What it does, in order:
  1. Restores the 9 sample documents (same data every run).
  2. Calls every endpoint exactly once, in a safe order.
  3. Prints one row per comparison: what the contract expects vs what the
     server actually returned, with PASS/FAIL in the last column.

Usage:
  1. Start the server first:  uvicorn app.main:app --port 8010
  2. python check_outputs.py http://127.0.0.1:8010
     (omit the URL to use http://127.0.0.1:8010 by default)

The expected values come from openapi.yaml + DATA_MODEL.md (the nine sample
documents in sample_data/). Keep the server running; this script never needs
to be run twice to be trustworthy - every run starts from fresh sample data.
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
results = []


def check(label, expected, actual):
    """Record one EXPECTED vs ACTUAL comparison."""
    ok = expected == actual
    results.append((label, expected, actual, ok))
    return ok


def req(method, path, body=None):
    """Call the API; return (status_code, parsed_json). Never raises on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            raw, code = resp.read().decode(), resp.status
    except urllib.error.HTTPError as exc:              # 4xx / 5xx are answers too
        raw, code = exc.read().decode(), exc.code
    except urllib.error.URLError as exc:
        sys.exit(f"\nCannot reach the server at {BASE} ({exc.reason}).\n"
                 "Start it first:  uvicorn app.main:app --port 8010")
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, raw


def get(path):
    return req("GET", path)


def find(items, **keys):
    """First item of a list matching all given key/values, else None."""
    return next((it for it in items
                 if all(it.get(k) == v for k, v in keys.items())), None)


def show(value, width=16):
    text = json.dumps(value)                          # null / true / numbers
    return text if len(text) <= width else text[:width - 1] + "…"


# --------------------------------------------------------------------------- #
# 0. restore sample data (deterministic starting point)
# --------------------------------------------------------------------------- #
here = pathlib.Path(__file__).parent / "sample_data"
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))[
    os.getenv("MONGO_DB", "attendance_db")]
for name in ("employees", "attendance_logs"):
    docs = json_util.loads((here / f"{name}.json").read_text())
    db[name].delete_many({})
    db[name].insert_many(docs)

code, body = get("/employees")
if not (code == 200 and body.get("total") == 6):
    sys.exit(f"\nAfter restoring the sample data the server still sees "
             f"total={body.get('total')!r} employees (expected 6).\n"
             "Server and this script are probably using DIFFERENT databases - "
             "set $env:MONGO_DB to the same value in both windows "
             f"(script uses {os.getenv('MONGO_DB', 'attendance_db')!r}).")
print(f"Sample data restored. Server: {BASE}   DB: {db.name}\n")

# --------------------------------------------------------------------------- #
# 1. read-only operations (health, lists, analytics, explain)
# --------------------------------------------------------------------------- #
code, body = get("/health")
check("GET /health  status", 200, code)
check("GET /health  body", {"status": "ok"}, body)

code, body = get("/employees?page_size=100")
check("GET /employees  status", 200, code)
check("GET /employees  total (6 samples)", 6, body.get("total"))
check("GET /employees  first item", "EMP0001",
      body["items"][0]["emp_code"] if body.get("items") else None)

code, body = get("/employees?department=Engineering")
check("GET /employees?department=Engineering  total", 2, body.get("total"))

code, body = get("/attendance?date_from=2026-07-01&date_to=2026-07-31&page_size=100")
check("GET /attendance  status", 200, code)
check("GET /attendance  total (9 samples)", 9, body.get("total"))
check("GET /attendance  newest date first", "2026-07-14",
      body["items"][0]["date"] if body.get("items") else None)
rec = find(body.get("items", []), emp_code="EMP0001", date="2026-07-07")
check("EMP0001 2026-07-07  work_hours", 9.03, rec and rec.get("work_hours"))
check("EMP0001 2026-07-07  late_minutes", 0, rec and rec.get("late_minutes"))
check("EMP0001 2026-07-07  audit history entries", 1,
      len(rec.get("history", [])) if rec else None)
check("EMP0001 2026-07-07  history keeps old value (from)",
      1783398900000,
      rec and rec["history"][0]["changes"]["punch_in"]["from"] if rec else None)

code, body = get("/analytics/employees/EMP0001/monthly?month=2026-07")
check("monthly EMP0001  status", 200, code)
check("monthly EMP0001  working_days", 23, body.get("working_days"))
check("monthly EMP0001  present_days", 2.0, body.get("present_days"))
check("monthly EMP0001  late_count", 0, body.get("late_count"))
check("monthly EMP0001  attendance_pct (2/23 half-up)", 8.7, body.get("attendance_pct"))

code, body = get("/analytics/employees/EMP0002/monthly?month=2026-07")
check("monthly EMP0002 (joined 07-13)  working_days", 15, body.get("working_days"))
check("monthly EMP0002  attendance_pct (1/15 half-up)", 6.67, body.get("attendance_pct"))

code, body = get("/analytics/departments/summary?month=2026-07")
check("dept summary  status", 200, code)
check("dept summary  department count", 3, len(body.get("items", [])))
sales = find(body.get("items", []), department="Sales")
check("dept Sales  headcount", 2, sales and sales.get("headcount"))
check("dept Sales  present_days (1 + 0.5 half-day)", 1.5, sales and sales.get("present_days"))
check("dept Sales  avg_work_hours ((9.08+3.5)/2)", 6.29, sales and sales.get("avg_work_hours"))
check("dept Sales  late_count / total_late_minutes", (1, 35),
      (sales and sales.get("late_count"), sales and sales.get("total_late_minutes")))
check("dept Sales  leave_count", 1, sales and sales.get("leave_count"))

code, body = get("/analytics/leaderboard/late?month=2026-07&limit=10")
check("late leaderboard  status", 200, code)
check("late leaderboard  rows (one late employee)", 1, len(body.get("items", [])))
top = body["items"][0] if body.get("items") else {}
check("late leaderboard  #1 is EMP0003 rank 1", ("EMP0003", 1, 35),
      (top.get("emp_code"), top.get("rank"), top.get("total_late_minutes")))

code, body = get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-08")
check("dept trend  status", 200, code)
check("dept trend  one row per day (8 days)", 8, len(body.get("items", [])))
sat = find(body.get("items", []), date="2026-07-04")          # a Saturday
check("trend 2026-07-04  is_working_day=false, rate=null", (False, None),
      (sat.get("is_working_day"), sat.get("attendance_rate")) if sat else None)
mon = find(body.get("items", []), date="2026-07-06")
check("trend 2026-07-06  attendance_rate=1.0", 1.0,
      mon.get("attendance_rate") if mon else None)

code, body = get("/admin/explain/attendance_list")
expl = json.dumps(body.get("explain", {}))
check("explain attendance_list  status", 200, code)
check("explain  uses index (IXSCAN present)", True, "IXSCAN" in expl)
check("explain  no full collection scan (COLLSCAN absent)", False, "COLLSCAN" in expl)

# --------------------------------------------------------------------------- #
# 2. write operations (create, punch-in, punch-out, regularize) + error codes
# --------------------------------------------------------------------------- #
code, body = req("POST", "/employees", {
    "emp_code": "EMP9001", "name": "Test User", "email": "test@example.com",
    "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
    "joined_on": "2026-07-01"})
check("POST /employees (new)  status", 201, code)
check("POST /employees (new)  echoes emp_code", "EMP9001", body.get("emp_code"))

code, body = req("POST", "/employees", {
    "emp_code": "EMP0001", "name": "Dup", "email": "dup@example.com",
    "department": "Sales", "joined_on": "2026-07-01"})
check("POST /employees (duplicate EMP0001)  status", 409, code)

code, body = req("POST", "/attendance/punch-in", {"emp_code": "EMP9001"})
today, punch_in = body.get("date"), body.get("punch_in")
check("POST punch-in (first)  status", 201, code)
check("POST punch-in (first)  open record (punch_out null)", None, body.get("punch_out"))
check("POST punch-in (first)  status derived", "PRESENT", body.get("status"))

code, body = req("POST", "/attendance/punch-in", {"emp_code": "EMP9001"})
check("POST punch-in (same day again)  status", 409, code)

code, body = req("POST", "/attendance/punch-out",
                 {"emp_code": "EMP9001", "punched_at": punch_in + 3_600_000})
check("POST punch-out  status", 200, code)
check("POST punch-out  work_hours (1h/3600s)", 1.0, body.get("work_hours"))

code, body = req("POST", "/attendance/punch-out",
                 {"emp_code": "EMP9001", "punched_at": punch_in + 7_200_000})
check("POST punch-out (again)  status", 409, code)

code, body = req("PATCH", f"/attendance/EMP9001/{today}", {
    "punch_out": punch_in + 7_200_000, "reason": "correct biometric error",
    "regularized_by": "hr.admin"})
check("PATCH regularize  status", 200, code)
check("PATCH regularize  audit entry appended", 1, len(body.get("history", [])))
check("PATCH regularize  audit stores old value (from)",
      punch_in + 3_600_000,
      body.get("history", [{}])[0].get("changes", {}).get("punch_out", {}).get("from"))

code, body = req("PATCH", "/attendance/EMP9001/2026-01-01", {
    "reason": "record does not exist", "regularized_by": "hr.admin"})
check("PATCH on missing date  status", 404, code)

code, body = req("PATCH", f"/attendance/EMP9001/{today}", {
    "punch_in": punch_in + 7_200_000, "punch_out": punch_in,
    "reason": "punch_out before punch_in", "regularized_by": "hr.admin"})
check("PATCH with punch_out before punch_in  status", 422, code)

code, body = req("POST", "/attendance/punch-in",
                 {"emp_code": "EMP9001", "punched_at": 1783396680000.5})
check("POST punch-in with float milliseconds  status", 422, code)

code, body = req("POST", "/attendance/punch-in", {"emp_code": "EMP4044"})
check("POST punch-in for unknown employee  status", 404, code)

# --------------------------------------------------------------------------- #
# print the table
# --------------------------------------------------------------------------- #
print(f"{'#':>3}  {'CHECK':<56} {'EXPECTED':<18} {'ACTUAL':<18} RESULT")
print("-" * 110)
for n, (label, expected, actual, ok) in enumerate(results, 1):
    print(f"{n:>3}  {label:<56} {show(expected):<18} {show(actual):<18} "
          f"{'PASS' if ok else '<<< FAIL'}")

passed = sum(1 for *_, ok in results if ok)
print("-" * 110)
if passed == len(results):
    print(f"ALL {len(results)} CHECKS PASSED - every operation returns the "
          "contract's expected output.")
else:
    print(f"{passed}/{len(results)} passed. Fix the rows marked FAIL, then rerun.")
print("\nThis run used fresh sample data; rerun it any time (it re-seeds first).")
