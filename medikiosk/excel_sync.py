"""
excel_sync.py - keeps an Excel file on YOUR PC up to date.

The website runs on your computer, so it can write a normal .xlsx file.
File: hospital_data.xlsx (next to app.py)
  Sheet "Doctors"  - same columns as your Google Sheet, with LIVE status
  Sheet "Bookings" - one row per appointment (history is never overwritten)

Tip: if Excel has the file open, Windows locks it and the website cannot
save. Close it, or open the copy from Staff desk -> "Download Excel".
"""
import io
import os
import threading

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import services as svc

_LOCK = threading.Lock()   # several bookings at once must not write the file at the same time
HEAD_FILL = PatternFill("solid", fgColor="0F766E")
HEAD_FONT = Font(bold=True, color="FFFFFF")


def _style(ws, widths):
    for cell in ws[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def build_workbook(db):
    wb = Workbook()
    ws = wb.active
    ws.title = "Doctors"
    ws.append(["Doctor Name", "Email", "Phone Number", "Ward Name", "Available Time", "Status",
               "Patient Name", "Summary", "Queue count", "Last token", "Room No"])
    today = svc.today_iso()
    for d in db.execute("SELECT * FROM doctors ORDER BY ward, name"):
        live = svc.doctor_live(db, d)
        cur = db.execute(
            """SELECT COALESCE(a.person_name, p.name) AS name, a.symptoms FROM appointments a JOIN patients p ON p.id = a.patient_id
                WHERE a.doctor_id = ? AND a.appt_date = ? AND a.status = 'Booked'
                  AND a.start_min <= ? AND a.end_min > ? LIMIT 1""",
            (d["id"], today, svc.minutes_now(), svc.minutes_now())).fetchone()
        last = db.execute("SELECT MAX(token) FROM appointments WHERE doctor_id = ? AND appt_date = ?",
                          (d["id"], today)).fetchone()[0]
        ws.append([d["name"], d["email"], d["phone"], d["ward"], d["shift_label"], live["status"],
                   cur["name"] if cur else "", cur["symptoms"] if cur else "",
                   live["waiting_count"], last or "", d["room"]])
    _style(ws, [22, 30, 14, 20, 22, 12, 20, 36, 12, 12, 16])

    bs = wb.create_sheet("Bookings")
    bs.append(["Appointment ID", "Date", "Time", "Token", "Patient Name", "Patient Email", "Phone",
               "Symptoms", "Ward Name", "Doctor Name", "Room", "Priority", "Status",
               "Triage by", "Booked at", "Booked by (account)"])
    rows = db.execute(
        """SELECT a.*, COALESCE(a.person_name, p.name) AS pname, p.name AS account, p.email AS pemail, p.phone AS pphone,
                  d.name AS dname, d.room AS droom
             FROM appointments a JOIN patients p ON p.id = a.patient_id JOIN doctors d ON d.id = a.doctor_id
            ORDER BY a.id DESC""").fetchall()
    for r in rows:
        bs.append([r["id"], r["appt_date"], svc.fmt_time(r["start_min"]), r["token"], r["pname"],
                   r["pemail"], r["pphone"], r["symptoms"], r["ward"], r["dname"], r["droom"],
                   "URGENT" if r["priority"] else "Normal", r["status"], r["triage_source"],
                   svc.utc_to_local_str(r["created_at"]), r["account"]])
    _style(bs, [14, 12, 10, 8, 20, 28, 14, 36, 20, 22, 14, 10, 12, 14, 20, 20])
    return wb


def workbook_bytes(db):
    buf = io.BytesIO()
    build_workbook(db).save(buf)
    buf.seek(0)
    return buf


def write_local(db_path, out_path):
    """Rewrite hospital_data.xlsx. Never raises - the website must not break because of Excel."""
    with _LOCK:
        return _write_local(db_path, out_path)


def _write_local(db_path, out_path):
    try:
        from database import open_target
        db = open_target(db_path)
        try:
            wb = build_workbook(db)
        finally:
            db.close()
        tmp = out_path + ".tmp"
        wb.save(tmp)
        os.replace(tmp, out_path)   # fails (PermissionError) if Excel has the file open
        return True, ""
    except PermissionError:
        return False, "hospital_data.xlsx is open in Excel - close it so the website can update it."
    except Exception as exc:
        return False, str(exc)
