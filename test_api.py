"""Focused tests for the HROne Employee Attendance & Analytics API.

Requires a running MongoDB (MONGO_URI / MONGO_DB; defaults:
mongodb://localhost:27017 + hrone_test) and the app served on BASE.
Seeds the nine sample documents, then exercises the contract, rules R1-R5,
pagination, the audit trail, the analytics endpoints and the explain plans.

Usage:  python test_api.py [base_url]     (default http://127.0.0.1:8000)
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))
RESULTS = []

QA_JOINED = "2026-12-01"  # keeps the QA test employees out of the July analytics


def ms_of(day: str, hhmmss: str = "00:00:00") -> int:
    """IST wall clock -> epoch milliseconds."""
    y, m, d = map(int, day.split("-"))
    hh, mm, ss = map(int, hhmmss.split(":"))
    dt = datetime(y, m, d, hh, mm, ss, tzinfo=IST)
    return int(dt.timestamp() * 1000)


def call(method, path, body=None, expect=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, payload = resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {"_raw": raw[:200]}
        status = e.code
    ok = expect == -1 or status == expect
    RESULTS.append((ok, method, path[:90], f"expected {expect} got {status}",
                    "" if ok else json.dumps(payload)[:300]))
    return status, payload


def check(cond, label, detail=""):
    RESULTS.append((bool(cond), "CHECK", label, "" if cond else str(detail)[:300], ""))


def emp(code, dept="QA", joined=QA_JOINED, shift=("09:30", "18:30")):
    return call("POST", "/employees", {
        "emp_code": code, "name": f"Test {code}", "email": f"{code.lower()}@example.com",
        "department": dept, "joined_on": joined,
        "shift_start": shift[0], "shift_end": shift[1]}, expect=201)


def plan_stages(node, found=None):
    """Collect every {"stage": ...} value in an explain document, skipping rejected plans."""
    found = [] if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "rejectedPlans":  # rejected options are not the winning path
                continue
            if key == "stage" and isinstance(value, str):
                found.append(value)
            plan_stages(value, found)
    elif isinstance(node, list):
        for value in node:
            plan_stages(value, found)
    return found


def main():
    # ---------------------------------------------------------------- health
    _, body = call("GET", "/health", expect=200)
    check(body.get("status") == "ok", "health returns status ok")

    # ------------------------------------------------------------- employees
    _, e = emp("EMP9001")
    check(isinstance(e.get("created_at"), int), "created_at is epoch ms", str(e.get("created_at")))
    check("_id" not in e and "id" not in e, "employee response has no _id/id", str(list(e)))
    call("POST", "/employees", {"emp_code": "EMP9001", "name": "Dup", "email": "d@example.com",
                                "department": "QA", "joined_on": QA_JOINED}, expect=409)
    call("POST", "/employees", {"emp_code": "BAD1", "name": "X", "email": "x@example.com",
                                "department": "QA", "joined_on": QA_JOINED}, expect=422)
    call("POST", "/employees", {"emp_code": "EMP9002", "name": "", "email": "x@example.com",
                                "department": "QA", "joined_on": QA_JOINED}, expect=422)
    call("POST", "/employees", {"emp_code": "EMP9002", "name": "X", "email": "nope",
                                "department": "QA", "joined_on": QA_JOINED}, expect=422)
    call("POST", "/employees", {"emp_code": "EMP9002", "name": "X", "email": "x@example.com",
                                "department": "QA", "joined_on": QA_JOINED,
                                "shift_start": "09:30", "shift_end": "09:30"}, expect=422)
    call("POST", "/employees", {"emp_code": "EMP9002", "name": "X", "email": "x@example.com",
                                "department": "QA", "joined_on": "2026-02-30"}, expect=422)

    _, page = call("GET", "/employees?department=Sales&page=1&page_size=1")
    check(page["total"] == 2 and len(page["items"]) == 1, "employees filtered total + page size",
          json.dumps(page)[:200])
    check(page["items"][0]["emp_code"] == "EMP0003", "employees sorted emp_code asc",
          page["items"][0]["emp_code"])
    call("GET", "/employees?page_size=101", expect=422)
    call("GET", "/employees?page=0", expect=422)
    _, page = call("GET", "/employees?department=Nope")
    check(page["total"] == 0, "unknown department -> total 0", str(page["total"]))

    # -------------------------------------------------- punch-in rules (R1/R2)
    call("POST", "/attendance/punch-in", {"emp_code": "EMPX"}, expect=404)
    call("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": 1783312500}, expect=422)
    call("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": 1783312500000.5}, expect=422)
    call("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": "1783312500000"}, expect=422)
    call("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": None}, expect=422)
    call("POST", "/attendance/punch-in", {"emp_code": "EMP0003", "status": "ABSENT"}, expect=422)
    # derived fields in a request body are ignored (R: read-only), not validation errors
    st, rec = call("POST", "/attendance/punch-in",
                   {"emp_code": "EMP0001", "status": "PRESENT", "work_hours": 8.0}, expect=201)
    check(rec.get("work_hours") is None and rec.get("half_day") is False,
          "derived fields in punch-in body are ignored", str(rec.get("work_hours")))

    # R2 grace: 09:40:00 -> 0 ; 09:40:01 -> 10 ; 10:15:59 -> 45 (shift 09:30 IST)
    for i, (hhmmss, expect_late) in enumerate([("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45)]):
        code = f"EMP92{i:02d}"
        emp(code)
        st, rec = call("POST", "/attendance/punch-in",
                       {"emp_code": code, "punched_at": ms_of("2026-08-10", hhmmss)}, expect=201)
        check(rec.get("late_minutes") == expect_late, f"R2 {hhmmss} -> late {expect_late}",
              str(rec.get("late_minutes")))
        check(rec.get("date") == "2026-08-10", "R1 normal-shift date", str(rec.get("date")))
        check(rec.get("punch_in", 0) % 1000 == 0, "truncated to whole seconds", str(rec.get("punch_in")))

    # R1 truncation: 09:30:00.900 counts as 09:30:00
    emp("EMP9109")
    st, rec = call("POST", "/attendance/punch-in",
                   {"emp_code": "EMP9109", "punched_at": ms_of("2026-08-10", "09:30:00") + 900},
                   expect=201)
    check(rec["punch_in"] == ms_of("2026-08-10", "09:30:00") and rec["late_minutes"] == 0,
          "sub-second punch truncated before calculation", str(rec["punch_in"]))

    # R1 overnight: a 05:00 punch belongs to the shift that started the previous day
    emp("EMP9108", shift=("22:00", "06:00"))
    st, rec = call("POST", "/attendance/punch-in",
                   {"emp_code": "EMP9108", "punched_at": ms_of("2026-08-20", "05:00:00")}, expect=201)
    check(rec.get("date") == "2026-08-19", "R1 overnight: 05:00 punch belongs to previous day",
          str(rec.get("date")))

    # duplicate punch-in -> 409
    emp("EMP9100")
    call("POST", "/attendance/punch-in",
         {"emp_code": "EMP9100", "punched_at": ms_of("2026-08-11", "09:00:00")}, expect=201)
    call("POST", "/attendance/punch-in",
         {"emp_code": "EMP9100", "punched_at": ms_of("2026-08-11", "09:30:00")}, expect=409)

    # ------------------------------------------------- punch-out rules R3-R5
    call("POST", "/attendance/punch-out", {"emp_code": "EMPX"}, expect=404)
    # newest record for that day is already closed -> 409
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP0001", "punched_at": ms_of("2026-07-07", "20:00:00")}, expect=409)
    # EMP0006's only punch-in is later that morning -> nothing eligible at 03:00
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP0006", "punched_at": ms_of("2026-07-07", "03:00:00")}, expect=404)

    # R4 half-up: 32418 s / 3600 = 9.005 -> 9.01 (float/banker's rounding says 9.0)
    emp("EMP9101")
    call("POST", "/attendance/punch-in",
         {"emp_code": "EMP9101", "punched_at": ms_of("2026-08-12", "09:30:00")}, expect=201)
    st, rec = call("POST", "/attendance/punch-out",
                   {"emp_code": "EMP9101", "punched_at": ms_of("2026-08-12", "18:30:18")}, expect=200)
    check(rec.get("work_hours") == 9.01, "R4 half-up 9.005 -> 9.01", str(rec.get("work_hours")))
    check(rec.get("overtime_minutes") == 0, "R3 overtime < 30 min -> 0", str(rec.get("overtime_minutes")))
    check(rec.get("half_day") is False, "R5 full day", str(rec.get("half_day")))
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP9101", "punched_at": ms_of("2026-08-12", "19:00:00")}, expect=409)

    # R3 overtime >= 30 min
    emp("EMP9102")
    call("POST", "/attendance/punch-in",
         {"emp_code": "EMP9102", "punched_at": ms_of("2026-08-13", "09:30:00")}, expect=201)
    st, rec = call("POST", "/attendance/punch-out",
                   {"emp_code": "EMP9102", "punched_at": ms_of("2026-08-13", "19:05:00")}, expect=200)
    check(rec.get("overtime_minutes") == 35, "R3 overtime 35 min", str(rec.get("overtime_minutes")))

    # R5 boundary: exactly 4.50 h -> full day; 4.48 h -> half day
    emp("EMP9103")
    call("POST", "/attendance/punch-in",
         {"emp_code": "EMP9103", "punched_at": ms_of("2026-08-14", "09:30:00")}, expect=201)
    st, rec = call("POST", "/attendance/punch-out",
                   {"emp_code": "EMP9103", "punched_at": ms_of("2026-08-14", "14:00:00")}, expect=200)
    check(rec.get("work_hours") == 4.5 and rec.get("half_day") is False,
          "R5 exactly 4.50 -> not half", f"{rec.get('work_hours')} {rec.get('half_day')}")
    emp("EMP9104")
    call("POST", "/attendance/punch-in",
         {"emp_code": "EMP9104", "punched_at": ms_of("2026-08-14", "09:30:00")}, expect=201)
    st, rec = call("POST", "/attendance/punch-out",
                   {"emp_code": "EMP9104", "punched_at": ms_of("2026-08-14", "13:59:00")}, expect=200)
    check(rec.get("half_day") is True, "R5 4.48 h -> half day", str(rec.get("half_day")))

    # punch-out validation: <= punch_in and > 24 h
    emp("EMP9105")
    call("POST", "/attendance/punch-in",
         {"emp_code": "EMP9105", "punched_at": ms_of("2026-08-15", "09:30:00")}, expect=201)
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP9105", "punched_at": ms_of("2026-08-15", "09:30:00")}, expect=422)
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP9105", "punched_at": ms_of("2026-08-16", "09:30:01")}, expect=422)

    # cross-midnight punch-out (overnight shift): out on the next calendar day
    emp("EMP9106", shift=("22:00", "06:00"))
    st, rec = call("POST", "/attendance/punch-in",
                   {"emp_code": "EMP9106", "punched_at": ms_of("2026-08-17", "21:55:00")}, expect=201)
    check(rec.get("date") == "2026-08-17", "overnight punch-in dated on shift start", str(rec.get("date")))
    st, rec = call("POST", "/attendance/punch-out",
                   {"emp_code": "EMP9106", "punched_at": ms_of("2026-08-18", "06:40:00")}, expect=200)
    check(rec.get("overtime_minutes") == 40 and rec.get("work_hours") == 8.75,
          "cross-midnight: overtime 40 / work 8.75",
          f"{rec.get('overtime_minutes')} {rec.get('work_hours')}")

    # ---------------------------------------------------------- concurrency
    emp("EMP9107")
    statuses, lock, barrier = [], threading.Lock(), threading.Barrier(5)

    def racer():
        barrier.wait()
        s, _ = call("POST", "/attendance/punch-in",
                    {"emp_code": "EMP9107", "punched_at": ms_of("2026-08-19", "09:00:00")}, expect=-1)
        with lock:
            statuses.append(s)

    threads = [threading.Thread(target=racer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(statuses.count(201) == 1 and statuses.count(409) == 4,
          "5 parallel punch-ins -> one 201, four 409", str(sorted(statuses)))

    statuses.clear()
    barrier2 = threading.Barrier(2)

    def racer_out():
        barrier2.wait()
        s, _ = call("POST", "/attendance/punch-out",
                    {"emp_code": "EMP9107", "punched_at": ms_of("2026-08-19", "18:00:00")}, expect=-1)
        with lock:
            statuses.append(s)

    threads = [threading.Thread(target=racer_out) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(statuses.count(200) == 1 and statuses.count(409) == 1,
          "2 parallel punch-outs -> one 200, one 409", str(sorted(statuses)))

    # ------------------------------------------------------------ list & page
    st, page = call("GET", "/attendance?date_from=2026-07-06&date_to=2026-07-08&page_size=100")
    dates = [r["date"] for r in page["items"]]
    check(dates == sorted(dates, reverse=True), "attendance sorted date desc", str(dates))
    check(page["total"] == len(page["items"]), "total reflects filters", str(page["total"]))
    call("GET", "/attendance?date_from=2026-07-10&date_to=2026-07-01", expect=422)
    _, page = call("GET", "/attendance?emp_code=EMP0004&status=ABSENT")
    check(page["total"] == 1, "status filter", str(page["total"]))
    _, page = call("GET", "/attendance?page=2&page_size=2")
    check(page["page"] == 2 and page["page_size"] == 2 and len(page["items"]) == 2,
          "pagination echoes and slices", json.dumps(page)[:200])
    _, recs = call("GET", "/attendance?emp_code=EMP0002")
    legacy = recs["items"][0]
    check(legacy["history"] == [] and legacy["half_day"] is False,
          "legacy record: missing history/half_day served as []/false",
          f"{legacy['history']} {legacy['half_day']}")
    check(all("_id" not in r and "id" not in r for r in recs["items"]), "records expose no _id/id")

    # ------------------------------------------------------- regularize/PATCH
    call("PATCH", "/attendance/EMP9999/2026-07-01",
         {"reason": "because this is long enough", "regularized_by": "hr"}, expect=404)
    call("PATCH", "/attendance/EMP0001/2026-01-01",
         {"reason": "because this is long enough", "regularized_by": "hr"}, expect=404)
    call("PATCH", "/attendance/EMP0001/2026-07-06",
         {"reason": "abc", "regularized_by": "hr"}, expect=422)  # reason too short
    call("PATCH", "/attendance/EMP0001/2026-07-06",
         {"reason": "same values nothing changes", "regularized_by": "hr"}, expect=422)
    call("PATCH", "/attendance/EMP0003/2026-07-06",
         {"status": "LEAVE", "punch_in": ms_of("2026-07-06", "09:30:00"),
          "reason": "because this is long enough", "regularized_by": "hr"}, expect=422)
    call("PATCH", "/attendance/EMP0004/2026-07-07",
         {"punch_in": ms_of("2026-07-07", "09:30:00"), "punch_out": ms_of("2026-07-07", "09:00:00"),
          "reason": "because this is long enough", "regularized_by": "hr"}, expect=422)
    call("PATCH", "/attendance/EMP0004/2026-07-07",
         {"punch_in": ms_of("2026-07-08", "09:30:00"),
          "reason": "because this is long enough", "regularized_by": "hr"}, expect=422)
    call("PATCH", "/attendance/EMP0004/2026-07-07",
         {"reason": "because this is long enough", "regularized_by": ""}, expect=422)
    call("PATCH", "/attendance/EMP0001/2026-07-06",
         {"status": "ABSENT", "punch_in": ms_of("2026-07-06", "09:30:00"),
          "reason": "because this is long enough", "regularized_by": "hr"}, expect=422)
    call("PATCH", "/attendance/EMP0001/2026-07-06",
         {"punch_in": 1783312500, "reason": "because this is long enough",
          "regularized_by": "hr"}, expect=422)  # seconds-looking value

    # happy path: move punch-in -> derived recompute + one history entry
    st, rec = call("PATCH", "/attendance/EMP0001/2026-07-06",
                   {"punch_in": ms_of("2026-07-06", "10:35:00"),
                    "reason": "biometric glitch", "regularized_by": "hr.admin"}, expect=200)
    check(rec["late_minutes"] == 65 and rec["punch_in"] == ms_of("2026-07-06", "10:35:00"),
          "PATCH recomputes late_minutes", str(rec["late_minutes"]))
    entry = rec["history"][-1] if rec["history"] else {}
    check(entry.get("by") == "hr.admin" and entry.get("reason") == "biometric glitch"
          and isinstance(entry.get("at"), int), "history has at/by/reason", json.dumps(entry)[:200])
    ch = entry.get("changes", {})
    check(ch.get("punch_in", {}).get("to") == ms_of("2026-07-06", "10:35:00")
          and isinstance(ch.get("punch_in", {}).get("from"), int),
          "changes use epoch-ms from/to", json.dumps(ch)[:200])
    check(ch.get("late_minutes") == {"from": 0, "to": 65},
          "derived change recorded in history", json.dumps(ch)[:200])
    check(rec["work_hours"] is not None, "work_hours recomputed on punch-in change",
          str(rec["work_hours"]))
    first_len = len(rec["history"])

    st, rec = call("PATCH", "/attendance/EMP0001/2026-07-06",
                   {"punch_in": ms_of("2026-07-06", "09:28:00"),
                    "reason": "second correction now", "regularized_by": "hr.b"}, expect=200)
    check(len(rec["history"]) == first_len + 1 and rec["history"][0]["reason"] == "biometric glitch",
          "history appends, never overwrites", str(len(rec["history"])))
    check(rec["late_minutes"] == 0 and rec["history"][-1]["changes"].get("late_minutes") == {"from": 65, "to": 0},
          "recompute after second correction", json.dumps(rec["history"][-1]["changes"]))

    # ABSENT -> PRESENT with a punch_in
    st, rec = call("PATCH", "/attendance/EMP0004/2026-07-08",
                   {"status": "PRESENT", "punch_in": ms_of("2026-07-08", "09:30:00"),
                    "reason": "was actually here", "regularized_by": "hr.admin"}, expect=200)
    check(rec["status"] == "PRESENT" and rec["punch_in"] == ms_of("2026-07-08", "09:30:00"),
          "ABSENT -> PRESENT correction", f"{rec['status']} {rec['punch_in']}")
    check(rec["history"][-1]["changes"].get("status") == {"from": "ABSENT", "to": "PRESENT"},
          "status change recorded", json.dumps(rec["history"][-1]["changes"]))

    # presence -> LEAVE clears punches and derived fields
    st, rec = call("PATCH", "/attendance/EMP0004/2026-07-07",
                   {"status": "LEAVE", "reason": "approved leave day",
                    "regularized_by": "hr.admin"}, expect=200)
    check(rec["punch_in"] is None and rec["punch_out"] is None and rec["work_hours"] is None
          and rec["late_minutes"] == 0 and rec["overtime_minutes"] == 0 and rec["half_day"] is False,
          "LEAVE clears punches and derived fields",
          json.dumps({k: rec[k] for k in ("punch_in", "punch_out", "work_hours", "half_day")}))
    check(rec["history"][-1]["changes"].get("punch_out", {}).get("to") is None,
          "history records cleared punch_out", json.dumps(rec["history"][-1]["changes"])[:200])

    # concurrent PATCH must not silently lose history (loser 409 or serialised 200)
    results, lock2 = [], threading.Lock()

    def patcher(i):
        s, _ = call("PATCH", "/attendance/EMP9102/2026-08-13",
                    {"punch_out": ms_of("2026-08-13", "19:20:00" if i else "19:25:00"),
                     "reason": f"concurrent correction {i}", "regularized_by": f"hr{i}"}, expect=-1)
        with lock2:
            results.append(s)

    ts = [threading.Thread(target=patcher, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check(sorted(results) == [200, 409] or sorted(results) == [200, 200],
          "concurrent PATCH: loser 409 (or serialised 200s)", str(sorted(results)))
    _, recs = call("GET", "/attendance?emp_code=EMP9102")
    hist = recs["items"][0]["history"]
    check(len(hist) >= 1 and all(e.get("reason") for e in hist),
          "history survived concurrency", json.dumps(hist)[:200])

    # ------------------------------------------------------------- analytics
    _, m = call("GET", "/analytics/employees/EMP0001/monthly?month=2026-07")
    check(m == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23,
                "present_days": 2.0, "leave_days": 0, "late_count": 0,
                "total_late_minutes": 0, "total_overtime_minutes": 0,
                "attendance_pct": 8.7},
          "employee_monthly EMP0001/2026-07", json.dumps(m))

    _, m = call("GET", "/analytics/employees/EMP0003/monthly?month=2026-07")
    check(m.get("leave_days") == 1 and m.get("late_count") == 1
          and m.get("total_late_minutes") == 35 and m.get("total_overtime_minutes") == 40
          and m.get("present_days") == 1.0 and m.get("attendance_pct") == 4.35,
          "employee_monthly EMP0003/2026-07 (late/leave/overtime)", json.dumps(m))

    # mid-month joiner: EMP0002 joined 2026-07-13 -> 15 working days
    _, m = call("GET", "/analytics/employees/EMP0002/monthly?month=2026-07")
    check(m.get("working_days") == 15 and m.get("present_days") == 1.0
          and m.get("attendance_pct") == 6.67, "mid-month joiner working_days/pct", json.dumps(m))

    # weekend records count for late/overtime but not for present_days (EMP0005,
    # 2026-07-06 is a Monday; use the trend for weekends - here just totals)
    call("GET", "/analytics/employees/EMP9999/monthly?month=2026-07", expect=404)
    call("GET", "/analytics/employees/EMP0001/monthly?month=2026-13", expect=422)
    call("GET", "/analytics/employees/EMP0001/monthly", expect=422)

    _, s = call("GET", "/analytics/departments/summary?month=2026-07")
    by_name = {i["department"]: i for i in s["items"]}
    check(list(by_name) == sorted(by_name), "departments sorted asc", str(list(by_name)))
    check("QA" not in by_name, "headcount-0 department omitted for the month", str(list(by_name)))
    eng = by_name.get("Engineering", {})
    check(eng.get("headcount") == 2, "headcount includes zero-log employee (R9)", json.dumps(eng))
    check(eng.get("present_days") == 3.0 and eng.get("avg_work_hours") == 9.04,
          "Engineering present/avg (record-level mean, half-up)", json.dumps(eng))
    sales = by_name.get("Sales", {})
    # after the corrections above: 07-06 full (1) + 07-07 LEAVE (0) + 07-08 PRESENT open (1);
    # leave_count = EMP0003 07-08 plus the record we just corrected to LEAVE
    check(sales.get("present_days") == 2.0 and sales.get("avg_work_hours") == 9.08
          and sales.get("leave_count") == 2 and sales.get("late_count") == 1
          and sales.get("total_late_minutes") == 35, "Sales summary after corrections", json.dumps(sales))
    sup = by_name.get("Support", {})
    check(sup.get("present_days") == 2.0 and sup.get("avg_work_hours") == 8.75,
          "Support summary (open record excluded from avg)", json.dumps(sup))
    _, s = call("GET", "/analytics/departments/summary?month=2026-07&department=Sales")
    check(len(s["items"]) == 1, "department filter", json.dumps(s["items"]))

    _, lb = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=10")
    check(lb.get("items") == [{"rank": 1, "emp_code": "EMP0003", "name": "Neha Iyer",
                               "department": "Sales", "total_late_minutes": 35, "late_count": 1}],
          "leaderboard rows", json.dumps(lb))
    call("GET", "/analytics/leaderboard/late", expect=422)
    call("GET", "/analytics/leaderboard/late?month=2026-07&limit=51", expect=422)

    # trend: gap filling, weekends, headcount by joined_on, rates + moving avg
    _, t = call("GET", "/analytics/departments/Engineering/trend?from=2026-07-04&to=2026-07-08")
    check([i["date"] for i in t["items"]] ==
          ["2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08"],
          "one row per calendar day incl. weekend", str([i["date"] for i in t["items"]]))
    sat, sun, mon, tue, wed = t["items"]
    check(sat["is_working_day"] is False and sun["is_working_day"] is False, "weekend flagged",
          str((sat["is_working_day"], sun["is_working_day"])))
    check(sat["attendance_rate"] is None and sat["moving_avg_7d"] is None,
          "weekend rate null", json.dumps(sat))
    check(mon["headcount"] == 1 and mon["attendance_rate"] == 1.0 and mon["moving_avg_7d"] == 1.0,
          "monday rate (EMP0002 not joined yet)", json.dumps(mon))
    check(wed["present_count"] == 0 and wed["attendance_rate"] == 0.0
          and wed["moving_avg_7d"] == 0.6667,
          "empty working day -> rate 0 + 3-row moving average", json.dumps(wed))
    call("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02", expect=404)
    call("GET", "/analytics/departments/Engineering/trend?from=2026-07-08&to=2026-07-01", expect=422)
    call("GET", "/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01", expect=422)

    # ---------------------------------------------------------------- explain
    explain_cases = [
        ("attendance_list", "?date_from=2026-07-01&date_to=2026-07-31&page=2&page_size=10",
         "attendance_logs"),
        ("employee_monthly", "?emp_code=EMP0001&month=2026-07", "attendance_logs"),
        ("department_summary", "?month=2026-07", "employees"),
        ("late_leaderboard", "?month=2026-07&limit=10", "attendance_logs"),
        ("department_trend", "?department=Engineering&from=2026-07-04&to=2026-07-08",
         "attendance_logs"),
    ]
    for name, qs, coll in explain_cases:
        st, ex = call("GET", f"/admin/explain/{name}{qs}")
        explain = ex.get("explain", {})
        check(ex.get("collection") == coll, f"explain {name}: collection",
              str(ex.get("collection")))
        stages = plan_stages(explain)
        check("IXSCAN" in stages, f"explain {name}: plan uses IXSCAN", str(stages)[:200])
        check("COLLSCAN" not in stages, f"explain {name}: plan has no COLLSCAN", str(stages)[:200])
    call("GET", "/admin/explain/employee_monthly?month=2026-07", expect=422)
    call("GET", "/admin/explain/department_trend?department=Engineering&from=2026-07-01",
         expect=422)
    call("GET", "/admin/explain/nope", expect=422)

    # ------------------------------------------------------------- summarise
    passed = sum(1 for r in RESULTS if r[0])
    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    for r in failed:
        print("FAIL:", r[1], r[2], "|", r[3], "|", r[4][:250])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
