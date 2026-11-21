"""
receipt.py - OP receipt for the staff desk (details typed in by staff, then Print or Download PDF).

"OP will be active up to 6 days": last active date = appointment date + OP_VALID_DAYS.
Change OP_VALID_DAYS below if the hospital rule changes.
"""
import io
import re

import services as svc
from datetime import date, datetime, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OP_VALID_DAYS = 6
HOSPITAL_NAME = "Neelima Hospital"


def last_active(day):
    return day + timedelta(days=OP_VALID_DAYS)


def fmt_date(day):
    return day.strftime("%d %b %Y")


def validate(form):
    """Returns (data, errors). data holds cleaned values + the computed last active date."""
    errors = []
    name = " ".join(form.get("patient_name", "").split())
    if not 2 <= len(name) <= 80:
        errors.append("Patient name must be 2-80 characters.")
    age = form.get("age", "").strip()
    if not (age.isdigit() and 0 <= int(age) <= 120):
        errors.append("Age must be a number between 0 and 120.")
    phone = form.get("phone", "").strip().replace(" ", "")
    if not re.fullmatch(r"\+?\d{10,13}", phone):
        errors.append("Phone number must be 10-13 digits.")
    try:
        day = date.fromisoformat(form.get("appt_date", ""))
    except ValueError:
        day = None
        errors.append("Please enter a valid date of appointment.")
    weight = form.get("weight", "").strip()
    try:
        w = float(weight)
        if not 0.5 <= w <= 300:
            raise ValueError
    except ValueError:
        errors.append("Weight must be a number in kg (0.5 to 300).")
    bp = form.get("bp", "").replace(" ", "")
    m = re.fullmatch(r"(\d{2,3})/(\d{2,3})", bp)
    if not (m and 50 <= int(m.group(1)) <= 260 and 30 <= int(m.group(2)) <= 160 and int(m.group(1)) > int(m.group(2))):
        errors.append("Blood pressure must look like 120/80 (mmHg).")
    appt_no = form.get("appt_no", "").strip().lstrip("#")
    if not re.fullmatch(r"[A-Za-z0-9\-]{1,20}", appt_no):
        errors.append("Appointment number: use letters, numbers or '-' (up to 20 characters).")
    if errors:
        return None, errors
    data = {
        "patient_name": name, "age": int(age), "phone": phone, "appt_date": day,
        "weight": weight, "bp": bp, "appt_no": appt_no,
        "last_active": last_active(day), "valid_days": OP_VALID_DAYS,
    }
    return data, []


def make_pdf(d):
    buf = io.BytesIO()
    w, h = A5
    c = canvas.Canvas(buf, pagesize=A5)
    c.setTitle(f"OP Receipt - {d['patient_name']}")
    teal = colors.HexColor("#0f766e")
    c.setStrokeColor(teal)
    c.setLineWidth(1.5)
    c.roundRect(10 * mm, 10 * mm, w - 20 * mm, h - 20 * mm, 4 * mm)
    c.setFillColor(teal)
    c.rect(10 * mm, h - 38 * mm, w - 20 * mm, 28 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(w / 2, h - 24 * mm, HOSPITAL_NAME.upper())
    c.setFont("Helvetica", 11)
    c.drawCentredString(w / 2, h - 32 * mm, "OP RECEIPT")

    rows = [
        ("Patient Name", d["patient_name"]),
        ("Age", f"{d['age']} yrs"),
        ("Phone Number", d["phone"]),
        ("Date of Appointment", fmt_date(d["appt_date"])),
        ("Weight", f"{d['weight']} kg"),
        ("Blood Pressure", f"{d['bp']} mmHg"),
        ("Appointment Number", f"#{d['appt_no']}"),
    ]
    y = h - 52 * mm
    c.setFillColor(colors.black)
    for label, value in rows:
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.HexColor("#475569"))
        c.drawString(18 * mm, y, label)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(colors.black)
        c.drawString(68 * mm, y, str(value)[:34])
        c.setStrokeColor(colors.HexColor("#e2e8f0"))
        c.setLineWidth(0.5)
        c.line(18 * mm, y - 2.5 * mm, w - 18 * mm, y - 2.5 * mm)
        y -= 10 * mm

    box_top = y - 2 * mm
    c.setFillColor(colors.HexColor("#fef3c7"))
    c.setStrokeColor(colors.HexColor("#f59e0b"))
    c.setLineWidth(1)
    c.roundRect(18 * mm, box_top - 24 * mm, w - 36 * mm, 24 * mm, 3 * mm, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#78350f"))
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(w / 2, box_top - 9 * mm, f"OP will be active up to {d['valid_days']} days")
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(w / 2, box_top - 18 * mm, f"Last active date of the OP: {fmt_date(d['last_active'])}")

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.6)
    c.line(w - 70 * mm, 28 * mm, w - 18 * mm, 28 * mm)
    c.setFillColor(colors.HexColor("#475569"))
    c.setFont("Helvetica", 8)
    c.drawCentredString(w - 44 * mm, 24 * mm, "Receptionist signature")
    c.drawString(18 * mm, 24 * mm, "Printed on " + svc.now_local().strftime("%d %b %Y, %I:%M %p"))
    c.showPage()
    c.save()
    buf.seek(0)
    return buf
