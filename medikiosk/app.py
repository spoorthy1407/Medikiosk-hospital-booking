"""
app.py - MediKiosk web server (Flask).

Run:  python app.py      then open http://127.0.0.1:5000
"""
import csv
import io
import os
import re
import secrets
import uuid
from datetime import date, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


def _load_env_file():
    """Read settings from a file named .env next to app.py (one KEY=value per line), so you do not have to type
    $env:... lines every time. Real environment variables (e.g. on Render) always win over the file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file()      # must run before the project modules below read their settings

import excel_sync
import gsheets
import lab_analyser
import mailer
import receipt
import services as svc
from database import get_db, init_db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# On a host with a persistent disk, set DATA_DIR to that disk's folder (uploads, Excel file and local database go there).
DATA_DIR = os.environ.get("DATA_DIR", "").strip()
INSTANCE_DIR = DATA_DIR or os.path.join(BASE_DIR, "instance")
UPLOAD_DIR = os.path.join(INSTANCE_DIR, "uploads")
ALLOWED_EXT = {"pdf", "png", "jpg", "jpeg", "webp", "gif", "doc", "docx", "txt"}
RECORD_CATEGORIES = ["Lab Report", "Prescription", "Scan / X-Ray", "Discharge Summary", "Other"]
PAY_MESSAGE = "Please pay the bill at the counter and take the receipt."


def load_secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


# USE_N8N=0 switches n8n off completely: ward choice uses the built-in rules and the website sends the
# appointment e-mails itself (needs the SMTP_* settings). Default: n8n on, exactly as before.
USE_N8N = os.environ.get("USE_N8N", "1").strip().lower() not in ("0", "false", "off", "no")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    # Local SQLite file by default; set DATABASE_URL=mysql://user:password@host:port/dbname?ssl-mode=REQUIRED to use MySQL (Aiven)
    DATABASE=os.environ.get("DATABASE_URL", "").strip() or os.path.join(INSTANCE_DIR, "medikiosk.db"),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # 10 MB uploads
    # Optional n8n integration - leave empty to use built-in keyword triage
    # Defaults point at your n8n cloud workflow; set the variable to "off" to disable.
    N8N_TRIAGE_URL=svc.env_url("N8N_TRIAGE_URL", "https://spoorthy1406.app.n8n.cloud/webhook/medikiosk-triage") if USE_N8N else "",
    N8N_NOTIFY_URL=svc.env_url("N8N_NOTIFY_URL", "https://spoorthy1406.app.n8n.cloud/webhook/medikiosk-booked") if USE_N8N else "",
    EXCEL_PATH=os.path.join(DATA_DIR or BASE_DIR, "hospital_data.xlsx"),
    # Set COOKIE_SECURE=1 on a website served over https
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"),
    SESSION_COOKIE_SAMESITE="Lax",
)
os.makedirs(UPLOAD_DIR, exist_ok=True)
init_db(app)
svc.STATUS["triage"] = "connected - waiting for the first booking" if app.config["N8N_TRIAGE_URL"] else "off (keyword rules)"
if app.config["N8N_NOTIFY_URL"]:
    svc.STATUS["notify"] = "connected - waiting for the first booking"
elif mailer.configured():
    svc.STATUS["notify"] = "connected - e-mails sent by the website (no n8n) - waiting for the first booking"
else:
    svc.STATUS["notify"] = "off - n8n is off and no e-mail settings: no appointment e-mails are sent"
mailer.init_status()
svc.STATUS["sheet"] = "ready - waiting for the next booking" if gsheets.configured() else "off"


def _persist_status(key, text):
    from database import open_target
    db = open_target(app.config["DATABASE"])
    try:
        k = f"status_{key}"
        if db.execute("SELECT 1 FROM meta WHERE `key` = ?", (k,)).fetchone():
            db.execute("UPDATE meta SET `value` = ? WHERE `key` = ?", (text[:250], k))
        else:
            db.execute("INSERT INTO meta (`key`, `value`) VALUES (?, ?)", (k, text[:250]))
        db.commit()
    finally:
        db.close()


def _restore_status():
    """After a restart, show the last real n8n result instead of 'not used yet'."""
    from database import open_target
    try:
        db = open_target(app.config["DATABASE"])
        try:
            for key in ("triage", "notify"):
                row = db.execute("SELECT `value` FROM meta WHERE `key` = ?", (f"status_{key}",)).fetchone()
                if row and row[0]:
                    svc.STATUS[key] = row[0]
        finally:
            db.close()
    except Exception as exc:
        print(f"[status] could not read: {exc}")


_restore_status()
svc.STATUS.persist = _persist_status


def sync_excel():
    """Refresh hospital_data.xlsx on this PC (safe: never raises)."""
    ok, msg = excel_sync.write_local(app.config["DATABASE"], app.config["EXCEL_PATH"])
    svc.STATUS["excel"] = "ok" if ok else msg
    if not ok:
        print(f"[excel] {msg}")

app.jinja_env.filters["fmt_time"] = svc.fmt_time
app.jinja_env.filters["local_dt"] = svc.utc_to_local_str
app.jinja_env.filters["fmt_hhmm"] = lambda t: svc.fmt_time(svc.parse_hhmm(t.strip()))
app.jinja_env.filters["date_label"] = svc.date_label


# --------------------------------------------------------------------------- #
# Security helpers: CSRF token + login decorators
# --------------------------------------------------------------------------- #
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "today": svc.today_iso()}


@app.before_request
def before_request():
    if request.endpoint == "static":
        return None
    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not sent or sent != session.get("csrf"):
            abort(400, "Form expired. Please go back, refresh the page and try again.")
    g.patient = None
    if session.get("patient_id"):
        g.patient = get_db().execute(
            "SELECT * FROM patients WHERE id = ?", (session["patient_id"],)
        ).fetchone()
        if g.patient is None:
            session.pop("patient_id", None)
    g.staff = session.get("staff_id")
    # AUTO-TIMER: finished slots are completed on every request
    svc.auto_complete(get_db())


def patient_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.patient is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def staff_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.staff:
            flash("Staff login required.", "warning")
            return redirect(url_for("staff_login"))
        return view(*args, **kwargs)
    return wrapper


def start_session(key, value):
    """Fresh session on login/logout (prevents session fixation) but keep the CSRF token."""
    token = session.get("csrf")
    session.clear()
    if token:
        session["csrf"] = token
    if key:
        session[key] = value


def safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else None


# --------------------------------------------------------------------------- #
# Public pages
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    db = get_db()
    doctors = [svc.doctor_live(db, d) for d in db.execute("SELECT * FROM doctors ORDER BY ward, name")]
    stats = {
        "free": sum(1 for d in doctors if d["status"] == "Free"),
        "consulting": sum(1 for d in doctors if d["status"] == "Consulting"),
        "waiting": sum(d["waiting_count"] for d in doctors),
    }
    return render_template("index.html", stats=stats)


@app.route("/doctors")
def doctors():
    db = get_db()
    rows = [svc.doctor_live(db, d) for d in db.execute("SELECT * FROM doctors ORDER BY ward, name")]
    return render_template("doctors.html", doctors=rows)


@app.route("/queue")
def queue():
    return render_template("queue.html")


@app.route("/api/queue")
def api_queue():
    db = get_db()
    rows = [svc.doctor_live(db, d) for d in db.execute("SELECT * FROM doctors ORDER BY ward, name")]
    return jsonify({"time": svc.fmt_time(svc.minutes_now()), "doctors": rows})


# --------------------------------------------------------------------------- #
# Patient signup / login / logout
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.patient:
        return redirect(url_for("dashboard"))
    form = request.form
    if request.method == "POST":
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        phone = form.get("phone", "").strip()
        age = form.get("age", "").strip()
        gender = form.get("gender", "")
        pw, pw2 = form.get("password", ""), form.get("confirm", "")
        errors = []
        if len(name) < 2:
            errors.append("Please enter your full name.")
        if not EMAIL_RE.match(email):
            errors.append("Please enter a valid email address.")
        if not re.fullmatch(r"\+?\d{10,13}", phone):
            errors.append("Phone number must be 10-13 digits.")
        if not age.isdigit() or not 0 < int(age) < 120:
            errors.append("Please enter a valid age.")
        if gender not in ("Female", "Male", "Other"):
            errors.append("Please select a gender.")
        if len(pw) < 6:
            errors.append("Password must be at least 6 characters.")
        if pw != pw2:
            errors.append("Passwords do not match.")
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM patients WHERE email = ?", (email,)).fetchone():
            errors.append("An account with this email already exists. Please log in.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("signup.html", form=form)
        cur = db.execute(
            "INSERT INTO patients (name, email, phone, age, gender, password_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (name, email, phone, int(age), gender, generate_password_hash(pw)),
        )
        db.commit()
        start_session("patient_id", cur.lastrowid)
        flash(f"Welcome to MediKiosk, {name}! Your account is ready.", "success")
        return redirect(url_for("dashboard"))
    return render_template("signup.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.patient:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM patients WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], pw):
            flash("Incorrect email or password.", "danger")
            return render_template("login.html", email=email)
        start_session("patient_id", user["id"])
        flash(f"Welcome back, {user['name']}!", "success")
        return redirect(safe_next(request.args.get("next")) or url_for("dashboard"))
    return render_template("login.html", email="")


@app.route("/logout", methods=["POST"])
def logout():
    start_session(None, None)
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Patient dashboard
# --------------------------------------------------------------------------- #
def today_reminders(db, patient_id):
    """All of today's tablet times with their status."""
    t, m = svc.today_iso(), svc.minutes_now()
    meds = db.execute(
        """SELECT * FROM medications WHERE patient_id = ? AND active = 1
              AND start_date <= ? AND (end_date IS NULL OR end_date = '' OR end_date >= ?)""",
        (patient_id, t, t),
    ).fetchall()
    logs = {
        (r["medication_id"], r["time_slot"]): r["status"]
        for r in db.execute(
            """SELECT l.* FROM med_logs l JOIN medications md ON md.id = l.medication_id
                WHERE md.patient_id = ? AND l.log_date = ?""",
            (patient_id, t),
        )
    }
    snoozes = {
        (r["medication_id"], r["time_slot"]): r
        for r in db.execute(
            """SELECT s.* FROM med_snooze s JOIN medications md ON md.id = s.medication_id
                WHERE md.patient_id = ? AND s.log_date = ?""",
            (patient_id, t),
        )
    }
    items = []
    for med in meds:
        for slot in med["times"].split(","):
            mins = svc.parse_hhmm(slot)
            if mins is None:
                continue
            status = logs.get((med["id"], slot))
            snooze = snoozes.get((med["id"], slot))
            if not status:
                if snooze and snooze["until_min"] > m:
                    status = "Snoozed"
                elif mins > m:
                    status = "Upcoming"
                elif m - mins <= 120:
                    status = "Due"
                else:
                    status = "Missed"
            items.append({
                "medication_id": med["id"], "name": med["name"], "dosage": med["dosage"] or "",
                "notes": med["notes"] or "", "slot": slot, "time": svc.fmt_time(mins),
                "minutes": mins, "status": status,
                "snooze_until": svc.fmt_time(snooze["until_min"]) if snooze else "",
                "alert_key": f"{med['id']}-{slot}-{snooze['count'] if snooze else 0}",
            })
    items.sort(key=lambda i: i["minutes"])
    return items


@app.route("/dashboard")
@patient_required
def dashboard():
    db = get_db()
    upcoming = db.execute(
        """SELECT a.*, d.name AS doctor_name, d.room FROM appointments a
             JOIN doctors d ON d.id = a.doctor_id
            WHERE a.patient_id = ? AND a.status = 'Booked'
            ORDER BY a.appt_date, a.start_min LIMIT 1""",
        (g.patient["id"],),
    ).fetchone()
    live = svc.appointment_live(db, upcoming) if upcoming else None
    counts = {
        "appointments": db.execute("SELECT COUNT(*) FROM appointments WHERE patient_id = ?", (g.patient["id"],)).fetchone()[0],
        "records": db.execute("SELECT COUNT(*) FROM records WHERE patient_id = ?", (g.patient["id"],)).fetchone()[0],
        "medications": db.execute("SELECT COUNT(*) FROM medications WHERE patient_id = ? AND active = 1", (g.patient["id"],)).fetchone()[0],
    }
    return render_template("dashboard.html", upcoming=upcoming, live=live, counts=counts,
                           reminders=today_reminders(db, g.patient["id"]),
                           adh=svc.adherence(db, g.patient["id"]))


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #
def notify_without_n8n(payload):
    """What n8n normally does after a booking: e-mails + Google Sheet row."""
    mailer.notify_booking_async(payload)
    gsheets.log_async(payload)


MAX_ACTIVE_BOOKINGS = 3   # active appointments one account can hold at a time (e.g. parent + 2 children)


def active_booking_count(db, patient_id):
    return db.execute("SELECT COUNT(*) FROM appointments WHERE patient_id = ? AND status = 'Booked'",
                      (patient_id,)).fetchone()[0]


def create_booking(db, person, symptoms, day):
    """Triage + doctor/slot for ONE person. Returns (appointment dict or None, error message)."""
    is_self = person.strip().lower() == g.patient["name"].strip().lower()
    ward, source = svc.triage(symptoms, {"name": person, "age": g.patient["age"] if is_self else "",
                                         "gender": g.patient["gender"] if is_self else ""},
                              app.config["N8N_TRIAGE_URL"])
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        choice = svc.choose_doctor(db, ward, day)
        if choice is None:
            db.rollback()
            mailer.alert_receptionist_async(
                f"No doctor available: {person} ({ward})",
                f"A patient could not be booked because no {ward} doctor has a free slot in the next 7 days.\n\n"
                f"Patient: {person}\nBooked by: {g.patient['name']} ({g.patient['phone']})\nProblem: {symptoms}\n\n"
                "Please call the patient.\n\n- MediKiosk")
            return None, f"{person}: no {ward} slots are free in the next 7 days. Please contact reception."
        doctor, appt_day, start, priority = choice
        if priority:
            svc.push_queue_back(db, doctor, appt_day, start)
        token = db.execute(
            "SELECT COALESCE(MAX(token), 0) + 1 FROM appointments WHERE doctor_id = ? AND appt_date = ?",
            (doctor["id"], appt_day),
        ).fetchone()[0]
        cur = db.execute(
            """INSERT INTO appointments (patient_id, doctor_id, ward, symptoms, appt_date, token,
                                         start_min, end_min, triage_source, priority, person_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (g.patient["id"], doctor["id"], ward, symptoms, appt_day, token,
             start, start + doctor["consult_minutes"], source, 1 if priority else 0, person),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    appt_id = cur.lastrowid
    # tell n8n (emails to doctor + patient, Google Sheet row) - optional
    payload = {
        "appointment_id": appt_id, "token": token,
        "patient_name": person, "booked_by": g.patient["name"], "patient_email": g.patient["email"],
        "patient_phone": g.patient["phone"], "symptoms": symptoms, "ward": ward,
        "doctor_name": doctor["name"], "doctor_email": doctor["email"], "room": doctor["room"],
        "date": appt_day, "time": svc.fmt_time(start), "message": PAY_MESSAGE,
        "priority": "URGENT - immediate treatment" if priority else "Normal",
    }
    if app.config["N8N_NOTIFY_URL"] and svc.n8n_available("notify"):
        # n8n sends the e-mails and logs the sheet; if it fails, the website does both itself (nobody notices)
        svc.notify_n8n_async(app.config["N8N_NOTIFY_URL"], payload, fallback=notify_without_n8n)
    else:
        notify_without_n8n(payload)               # n8n off or temporarily down
    return {"id": appt_id, "person": person, "doctor": doctor, "day": appt_day, "priority": priority}, ""


@app.route("/book", methods=["GET", "POST"])
@patient_required
def book():
    db = get_db()
    max_day = (svc.now_local().date() + timedelta(days=14)).isoformat()
    active = active_booking_count(db, g.patient["id"])
    left = max(0, MAX_ACTIVE_BOOKINGS - active)

    def show(form, day):
        return render_template("book.html", form=form, day=day, max_day=max_day, active=active, left=left,
                               max_rows=MAX_ACTIVE_BOOKINGS)

    if request.method == "POST":
        day = request.form.get("appt_date", svc.today_iso())
        try:
            if not svc.today_iso() <= date.fromisoformat(day).isoformat() <= max_day:
                raise ValueError
        except ValueError:
            flash("Please choose a date between today and the next 14 days.", "danger")
            return show(request.form, svc.today_iso())

        people, errors, seen = [], [], set()
        for i in range(1, MAX_ACTIVE_BOOKINGS + 1):
            name = " ".join(request.form.get(f"person_name_{i}", "").split())
            symptoms = request.form.get(f"symptoms_{i}", "").strip()
            if not name and not symptoms:
                continue
            if len(name) < 2:
                errors.append(f"Person {i}: please enter the patient's name.")
            elif name.lower() in seen:
                errors.append(f"Person {i}: {name} is already in this booking - use a different name for each appointment.")
            if len(symptoms) < 5:
                errors.append(f"Person {i}: please describe the problem in a few words (at least 5 characters).")
            seen.add(name.lower())
            people.append((name, symptoms))
        if not people:
            errors.append("Please fill in at least one patient.")
        if len(people) > left:
            errors.append(f"You can have up to {MAX_ACTIVE_BOOKINGS} active appointments at a time. "
                          f"You have {active} now, so you can book {left} more.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return show(request.form, day)

        booked = []
        for name, symptoms in people:
            existing = db.execute(
                """SELECT id FROM appointments WHERE patient_id = ? AND appt_date = ? AND status = 'Booked'
                      AND lower(COALESCE(person_name, ?)) = lower(?)""",
                (g.patient["id"], day, g.patient["name"], name),
            ).fetchone()
            if existing:
                flash(f"{name} already has an active booking for that day. Cancel it first to book again.", "warning")
                continue
            appt, err = create_booking(db, name, symptoms, day)
            if appt is None:
                flash(err, "danger")
                continue
            booked.append(appt)
            if appt["priority"]:
                flash(f"PRIORITY CASE ({name}): please go to {appt['doctor']['name']} in room {appt['doctor']['room']} "
                      "right now. They will be seen immediately.", "danger")
            if appt["day"] != day:
                flash(f"{name}: no free slot on the chosen day, so we booked the earliest one: {svc.date_label(appt['day'])}.", "info")
        if not booked:
            return show(request.form, day)
        sync_excel()
        flash("Appointment booked successfully!" if len(booked) == 1
              else f"{len(booked)} appointments booked successfully!", "success")
        if len(booked) == 1:
            return redirect(url_for("appointment_detail", appt_id=booked[0]["id"], new=1))
        return redirect(url_for("appointments", new=1))
    return show({}, svc.today_iso())


def get_own_appointment(appt_id):
    appt = get_db().execute(
        """SELECT a.*, d.name AS doctor_name, d.room, d.ward AS doctor_ward, d.shift_label
             FROM appointments a JOIN doctors d ON d.id = a.doctor_id
            WHERE a.id = ? AND a.patient_id = ?""",
        (appt_id, g.patient["id"]),
    ).fetchone()
    if appt is None:
        abort(404)
    return appt


@app.route("/appointments")
@patient_required
def appointments():
    rows = get_db().execute(
        """SELECT a.*, d.name AS doctor_name, d.room FROM appointments a
             JOIN doctors d ON d.id = a.doctor_id
            WHERE a.patient_id = ?
            ORDER BY CASE a.status WHEN 'Booked' THEN 0 ELSE 1 END, a.appt_date DESC, a.start_min DESC""",
        (g.patient["id"],),
    ).fetchall()
    return render_template("appointments.html", appointments=rows, is_new=request.args.get("new") == "1",
                           pay_message=PAY_MESSAGE)


@app.route("/appointments/<int:appt_id>")
@patient_required
def appointment_detail(appt_id):
    appt = get_own_appointment(appt_id)
    live = svc.appointment_live(get_db(), appt)
    return render_template("appointment_detail.html", a=appt, live=live,
                           pay_message=PAY_MESSAGE, is_new=request.args.get("new") == "1")


@app.route("/api/appointments/<int:appt_id>")
@patient_required
def api_appointment(appt_id):
    return jsonify(svc.appointment_live(get_db(), get_own_appointment(appt_id)))


@app.route("/appointments/<int:appt_id>/done", methods=["POST"])
@patient_required
def mark_appointment_done(appt_id):
    """Patient ticks 'consultation done'. If they forget, the staff desk can still tick it."""
    appt = get_own_appointment(appt_id)
    if appt["status"] != "Booked":
        flash("This appointment is already closed.", "warning")
    else:
        db = get_db()
        db.execute("UPDATE appointments SET status = 'Completed', completed_at = ? WHERE id = ? AND status = 'Booked'",
                   (svc.now_local().isoformat(timespec="seconds"), appt_id))
        db.commit()
        sync_excel()
        flash("Marked as done. Thank you!", "success")
    return redirect(request.referrer or url_for("appointments"))


@app.route("/appointments/<int:appt_id>/cancel", methods=["POST"])
@patient_required
def cancel_appointment(appt_id):
    appt = get_own_appointment(appt_id)
    if appt["status"] != "Booked":
        flash("Only active bookings can be cancelled.", "warning")
    else:
        db = get_db()
        db.execute("UPDATE appointments SET status = 'Cancelled' WHERE id = ?", (appt_id,))
        db.commit()
        flash("Your appointment was cancelled.", "info")
        sync_excel()
    return redirect(url_for("appointments"))


# --------------------------------------------------------------------------- #
# Medical records (reports / prescriptions / scans)
# --------------------------------------------------------------------------- #
def allowed_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXT


@app.route("/records", methods=["GET", "POST"])
@patient_required
def records():
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "")
        record_date = request.form.get("record_date", "") or None
        notes = request.form.get("notes", "").strip()
        file = request.files.get("file")
        if not title or category not in RECORD_CATEGORIES:
            flash("Please enter a title and choose a category.", "danger")
        elif not file or not file.filename:
            flash("Please choose a file to upload.", "danger")
        elif not allowed_file(file.filename):
            flash("Allowed files: PDF, images (JPG/PNG/WEBP/GIF), DOC/DOCX, TXT.", "danger")
        else:
            original = secure_filename(file.filename) or "file"
            stored = f"{uuid.uuid4().hex}.{original.rsplit('.', 1)[-1].lower()}"
            folder = os.path.join(UPLOAD_DIR, str(g.patient["id"]))
            os.makedirs(folder, exist_ok=True)
            file.save(os.path.join(folder, stored))
            db.execute(
                """INSERT INTO records (patient_id, title, category, record_date, notes, filename, stored_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (g.patient["id"], title, category, record_date, notes, original, stored),
            )
            db.commit()
            flash("Record uploaded and saved to your account.", "success")
            return redirect(url_for("records"))
    category_filter = request.args.get("category", "")
    sql = "SELECT * FROM records WHERE patient_id = ?"
    params = [g.patient["id"]]
    if category_filter in RECORD_CATEGORIES:
        sql += " AND category = ?"
        params.append(category_filter)
    rows = db.execute(sql + " ORDER BY uploaded_at DESC, id DESC", params).fetchall()
    return render_template("records.html", records=rows, categories=RECORD_CATEGORIES,
                           current=category_filter)


def get_own_record(record_id):
    rec = get_db().execute(
        "SELECT * FROM records WHERE id = ? AND patient_id = ?", (record_id, g.patient["id"])
    ).fetchone()
    if rec is None:
        abort(404)
    return rec


@app.route("/records/<int:record_id>/file")
@patient_required
def record_file(record_id):
    rec = get_own_record(record_id)
    return send_from_directory(os.path.join(UPLOAD_DIR, str(g.patient["id"])), rec["stored_name"],
                               download_name=rec["filename"],
                               as_attachment=request.args.get("download") == "1")


@app.route("/records/<int:record_id>/analyse")
@patient_required
def analyse_record(record_id):
    rec = get_own_record(record_id)
    path = os.path.join(UPLOAD_DIR, str(g.patient["id"]), rec["stored_name"])
    text, reason = lab_analyser.extract_text(path)
    results = lab_analyser.analyse(text, g.patient["gender"]) if text else []
    return render_template("analyse.html", rec=rec, results=results, reason=reason)


@app.route("/records/<int:record_id>/delete", methods=["POST"])
@patient_required
def delete_record(record_id):
    rec = get_own_record(record_id)
    path = os.path.join(UPLOAD_DIR, str(g.patient["id"]), rec["stored_name"])
    if os.path.exists(path):
        os.remove(path)
    db = get_db()
    db.execute("DELETE FROM records WHERE id = ?", (record_id,))
    db.commit()
    flash("Record deleted.", "info")
    return redirect(url_for("records"))


# --------------------------------------------------------------------------- #
# Medication (tablet) reminders
# --------------------------------------------------------------------------- #
@app.route("/reminders", methods=["GET", "POST"])
@patient_required
def reminders():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        dosage = request.form.get("dosage", "").strip()
        notes = request.form.get("notes", "").strip()
        start_date = request.form.get("start_date") or svc.today_iso()
        end_date = request.form.get("end_date") or None
        times = sorted({t.strip() for t in request.form.getlist("times") if t.strip()})
        bad = [t for t in times if svc.parse_hhmm(t) is None]
        if not name:
            flash("Please enter the medicine name.", "danger")
        elif not times or bad:
            flash("Please add at least one valid reminder time.", "danger")
        elif end_date and end_date < start_date:
            flash("End date cannot be before start date.", "danger")
        else:
            times = [f"{svc.parse_hhmm(t) // 60:02d}:{svc.parse_hhmm(t) % 60:02d}" for t in times]
            db.execute(
                """INSERT INTO medications (patient_id, name, dosage, times, start_date, end_date, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (g.patient["id"], name, dosage, ",".join(times), start_date, end_date, notes),
            )
            db.commit()
            mailer.mark_dirty()
            flash(f"Reminder added for {name}.", "success")
            return redirect(url_for("reminders"))
    meds = db.execute(
        "SELECT * FROM medications WHERE patient_id = ? ORDER BY active DESC, name", (g.patient["id"],)
    ).fetchall()
    return render_template("reminders.html", meds=meds, today_items=today_reminders(db, g.patient["id"]),
                           adh=svc.adherence(db, g.patient["id"]), week=svc.adherence(db, g.patient["id"], 7))


def get_own_med(med_id):
    med = get_db().execute(
        "SELECT * FROM medications WHERE id = ? AND patient_id = ?", (med_id, g.patient["id"])
    ).fetchone()
    if med is None:
        abort(404)
    return med


@app.route("/reminders/<int:med_id>/toggle", methods=["POST"])
@patient_required
def toggle_med(med_id):
    med = get_own_med(med_id)
    db = get_db()
    mailer.mark_dirty()
    db.execute("UPDATE medications SET active = ? WHERE id = ?", (0 if med["active"] else 1, med_id))
    db.commit()
    flash(f"Reminder for {med['name']} {'paused' if med['active'] else 'resumed'}.", "info")
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:med_id>/delete", methods=["POST"])
@patient_required
def delete_med(med_id):
    med = get_own_med(med_id)
    db = get_db()
    mailer.mark_dirty()
    db.execute("DELETE FROM medications WHERE id = ?", (med_id,))
    db.commit()
    flash(f"Reminder for {med['name']} deleted.", "info")
    return redirect(url_for("reminders"))


@app.route("/reminders/log", methods=["POST"])
@patient_required
def log_med():
    med = get_own_med(int(request.form.get("medication_id", 0)))
    slot = request.form.get("slot", "")
    status = request.form.get("status", "")
    if slot not in med["times"].split(",") or status not in ("Taken", "Skipped"):
        abort(400)
    db = get_db()
    db.execute(
        """INSERT INTO med_logs (medication_id, log_date, time_slot, status) VALUES (?, ?, ?, ?)
           ON CONFLICT(medication_id, log_date, time_slot) DO UPDATE SET status = excluded.status,
                                                                      logged_at = CURRENT_TIMESTAMP""",
        (med["id"], svc.today_iso(), slot, status),
    )
    db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    flash(f"{med['name']} marked as {status.lower()}.", "success")
    return redirect(request.referrer or url_for("reminders"))


SNOOZE_MINUTES = 5


@app.route("/reminders/snooze", methods=["POST"])
@patient_required
def snooze_med():
    """'Remind me again in 5 minutes'. The dose stays unmarked, so it is NOT counted as taken."""
    med = get_own_med(int(request.form.get("medication_id", 0)))
    slot = request.form.get("slot", "")
    mins = svc.parse_hhmm(slot)
    if slot not in med["times"].split(",") or mins is None:
        abort(400)
    m = svc.minutes_now()
    wants_json = request.headers.get("X-Requested-With") == "fetch"
    db = get_db()
    already = db.execute("SELECT 1 FROM med_logs WHERE medication_id = ? AND log_date = ? AND time_slot = ?",
                         (med["id"], svc.today_iso(), slot)).fetchone()
    if already or m + SNOOZE_MINUTES - mins > 120:
        msg = "This dose is already recorded." if already else "Too late to snooze - please mark it Taken or Skipped."
        if wants_json:
            return jsonify({"ok": False, "message": msg}), 400
        flash(msg, "warning")
        return redirect(request.referrer or url_for("reminders"))
    db.execute(
        """INSERT INTO med_snooze (medication_id, log_date, time_slot, until_min, count) VALUES (?, ?, ?, ?, 1)
           ON CONFLICT(medication_id, log_date, time_slot)
           DO UPDATE SET until_min = excluded.until_min, count = med_snooze.count + 1""",
        (med["id"], svc.today_iso(), slot, m + SNOOZE_MINUTES),
    )
    db.commit()
    mailer.note_snooze(m + SNOOZE_MINUTES)
    until = svc.fmt_time(m + SNOOZE_MINUTES)
    if wants_json:
        return jsonify({"ok": True, "until": until})
    flash(f"{med['name']}: I'll remind you again at {until}.", "info")
    return redirect(request.referrer or url_for("reminders"))


@app.route("/api/reminders/due")
@patient_required
def api_reminders_due():
    db = get_db()
    items = [i for i in today_reminders(db, g.patient["id"]) if i["status"] == "Due"]
    has_meds = db.execute("SELECT 1 FROM medications WHERE patient_id = ? AND active = 1 LIMIT 1",
                          (g.patient["id"],)).fetchone() is not None
    return jsonify({"due": items, "has_meds": has_meds})


# --------------------------------------------------------------------------- #
# Staff / reception
# --------------------------------------------------------------------------- #
@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "POST":
        user = get_db().execute(
            "SELECT * FROM staff WHERE username = ?", (request.form.get("username", "").strip(),)
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash("Incorrect staff username or password.", "danger")
        else:
            start_session("staff_id", user["id"])
            return redirect(url_for("staff_dashboard"))
    return render_template("staff_login.html")


@app.route("/staff")
@staff_required
def staff_dashboard():
    db = get_db()
    doctors = [svc.doctor_live(db, d) for d in db.execute("SELECT * FROM doctors ORDER BY ward, name")]
    leave = {d["id"]: d["on_leave"] for d in db.execute("SELECT id, on_leave FROM doctors")}
    day = request.args.get("day", svc.today_iso())
    appts = db.execute(
        """SELECT a.*, COALESCE(a.person_name, p.name) AS patient_name, p.phone, d.name AS doctor_name, d.room
             FROM appointments a JOIN patients p ON p.id = a.patient_id JOIN doctors d ON d.id = a.doctor_id
            WHERE a.appt_date = ? ORDER BY d.name, a.start_min""",
        (day,),
    ).fetchall()
    return render_template("staff_dashboard.html", doctors=doctors, leave=leave, appts=appts, day=day,
                           n8n_visible=svc.n8n_visible(bool(app.config["N8N_TRIAGE_URL"] or app.config["N8N_NOTIFY_URL"])),
                           status=svc.STATUS, excel_path=app.config["EXCEL_PATH"],
                           sheet_on=gsheets.configured())


@app.route("/staff/patients")
@staff_required
def staff_patients():
    """Doctor / reception view: every patient with their tablet adherence."""
    db = get_db()
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM patients"
    args = ()
    if q:
        sql += " WHERE name LIKE ? OR email LIKE ? OR phone LIKE ?"
        args = (f"%{q}%",) * 3
    rows = []
    for p in db.execute(sql + " ORDER BY name", args).fetchall():
        last = db.execute(
            "SELECT appt_date, ward FROM appointments WHERE patient_id = ? ORDER BY appt_date DESC, start_min DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        rows.append({"p": p, "adh": svc.adherence(db, p["id"]), "last": last,
                     "records": db.execute("SELECT COUNT(*) FROM records WHERE patient_id = ?", (p["id"],)).fetchone()[0]})
    return render_template("staff_patients.html", rows=rows, q=q)


@app.route("/staff/patients/<int:patient_id>")
@staff_required
def staff_patient_detail(patient_id):
    db = get_db()
    p = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if p is None:
        abort(404)
    days = request.args.get("days", 30, type=int)
    days = days if days in (7, 30, 90) else 30
    appts = db.execute(
        """SELECT a.*, d.name AS doctor_name, d.room FROM appointments a JOIN doctors d ON d.id = a.doctor_id
            WHERE a.patient_id = ? ORDER BY a.appt_date DESC, a.start_min DESC""",
        (patient_id,),
    ).fetchall()
    recs = db.execute("SELECT * FROM records WHERE patient_id = ? ORDER BY uploaded_at DESC", (patient_id,)).fetchall()
    return render_template("staff_patient.html", p=p, adh=svc.adherence(db, patient_id, days), days=days,
                           appts=appts, recs=recs, fmt_time=svc.fmt_time)


@app.route("/staff/records/<int:record_id>/file")
@staff_required
def staff_record_file(record_id):
    rec = get_db().execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    if rec is None:
        abort(404)
    return send_from_directory(os.path.join(UPLOAD_DIR, str(rec["patient_id"])), rec["stored_name"],
                               download_name=rec["filename"])


@app.route("/staff/appointments/<int:appt_id>/<action>", methods=["POST"])
@staff_required
def staff_appt_action(appt_id, action):
    status = {"complete": "Completed", "cancel": "Cancelled", "noshow": "No Show"}.get(action)
    if not status:
        abort(400)
    db = get_db()
    db.execute(
        "UPDATE appointments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Booked'",
        (status, appt_id),
    )
    db.commit()
    sync_excel()
    flash(f"Appointment marked {status}. Doctor availability updated.", "success")
    return redirect(request.referrer or url_for("staff_dashboard"))


@app.route("/staff/doctors/<int:doctor_id>/leave", methods=["POST"])
@staff_required
def staff_toggle_leave(doctor_id):
    db = get_db()
    db.execute("UPDATE doctors SET on_leave = 1 - on_leave WHERE id = ?", (doctor_id,))
    db.commit()
    sync_excel()
    flash("Doctor leave status updated.", "success")
    return redirect(url_for("staff_dashboard"))


# --------------------------------------------------------------------------- #
# Staff: edit doctor availability / hours (also refreshes hospital_data.xlsx)
# --------------------------------------------------------------------------- #
@app.route("/staff/doctors")
@staff_required
def staff_doctors():
    db = get_db()
    docs = db.execute("SELECT * FROM doctors ORDER BY ward, name").fetchall()
    rows = []
    for d in docs:
        rows.append({"d": d, "start": f"{d['shift_start'] // 60:02d}:{d['shift_start'] % 60:02d}",
                     "end": f"{(d['shift_end'] % 1440) // 60:02d}:{d['shift_end'] % 60:02d}",
                     "live": svc.doctor_live(db, d)})
    return render_template("staff_doctors.html", rows=rows, excel_path=app.config["EXCEL_PATH"])


@app.route("/staff/doctors/<int:doctor_id>/update", methods=["POST"])
@staff_required
def staff_update_doctor(doctor_id):
    db = get_db()
    doc = db.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone()
    if doc is None:
        abort(404)
    start = svc.parse_hhmm(request.form.get("shift_start", ""))
    end_raw = request.form.get("shift_end", "")
    end = 24 * 60 if end_raw.strip() == "00:00" else svc.parse_hhmm(end_raw)
    available = request.form.get("available") == "1"
    if start is None or end is None:
        flash("Please enter valid start and end times.", "danger")
        return redirect(url_for("staff_doctors"))
    if end - start < doc["consult_minutes"]:
        flash(f"The shift must be at least {doc['consult_minutes']} minutes long (end time after start time, same day).", "danger")
        return redirect(url_for("staff_doctors"))
    label = f"{svc.fmt_time(start)} \u2013 {svc.fmt_time(end)}"
    db.execute("UPDATE doctors SET shift_start = ?, shift_end = ?, shift_label = ?, on_leave = ? WHERE id = ?",
               (start, end, label, 0 if available else 1, doctor_id))
    db.commit()
    outside = db.execute(
        """SELECT COUNT(*) FROM appointments WHERE doctor_id = ? AND status = 'Booked' AND appt_date >= ?
              AND (start_min < ? OR end_min > ? OR ? = 1)""",
        (doctor_id, svc.today_iso(), start, end, 0 if available else 1),
    ).fetchone()[0]
    sync_excel()
    flash(f"{doc['name']}: {'available' if available else 'unavailable'}, {label}. The Excel file was updated.", "success")
    if outside:
        flash(f"{outside} existing booking(s) for {doc['name']} are now outside these hours or on an unavailable day. "
              "Please check the Staff desk and cancel or move them.", "warning")
    return redirect(url_for("staff_doctors"))


# --------------------------------------------------------------------------- #
# Staff: OP receipt (typed in by staff, then Print or Download PDF)
# --------------------------------------------------------------------------- #
@app.route("/staff/receipt")
@staff_required
def staff_receipt():
    return render_template("receipt_form.html", form={}, valid_days=receipt.OP_VALID_DAYS, today_iso=svc.today_iso())


def _receipt_or_form():
    data, errors = receipt.validate(request.form)
    if errors:
        for e in errors:
            flash(e, "danger")
        return None, render_template("receipt_form.html", form=request.form, valid_days=receipt.OP_VALID_DAYS,
                                     today_iso=svc.today_iso())
    return data, None


@app.route("/staff/receipt/pdf", methods=["POST"])
@staff_required
def staff_receipt_pdf():
    data, page = _receipt_or_form()
    if page:
        return page
    safe = re.sub(r"[^A-Za-z0-9]+", "_", data["patient_name"]).strip("_") or "patient"
    return Response(receipt.make_pdf(data).getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=OP_Receipt_{safe}_{data['appt_date']}.pdf"})


@app.route("/staff/receipt/print", methods=["POST"])
@staff_required
def staff_receipt_print():
    data, page = _receipt_or_form()
    if page:
        return page
    return render_template("receipt_print.html", r=data, fmt=receipt.fmt_date, hospital=receipt.HOSPITAL_NAME,
                           form=request.form)


@app.route("/staff/test-email", methods=["POST"])
@staff_required
def staff_test_email():
    """Send one test e-mail so you can see immediately whether the Gmail settings work."""
    to = request.form.get("to", "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
        flash("Please enter a valid e-mail address.", "danger")
    elif not mailer.configured():
        flash("E-mail settings (BREVO_API_KEY + EMAIL_FROM, or SMTP_HOST / SMTP_USER / SMTP_PASS) are not set where the website runs. "
              "Set them, then start the website again.", "danger")
    else:
        try:
            mailer.send_mail(to, "MediKiosk test e-mail", "This is a test. Tablet reminder e-mails will reach you like this.")
            svc.STATUS["email"] = "on - test e-mail sent OK"
            flash(f"Test e-mail sent to {to}. Check the inbox (and Spam).", "success")
        except Exception as exc:
            svc.STATUS["email"] = f"FAILED: {exc}"
            flash(f"Could not send: {exc}", "danger")
    return redirect(url_for("staff_dashboard"))


@app.route("/staff/export.xlsx")
@staff_required
def staff_export_xlsx():
    """Always-fresh Excel copy (works even if hospital_data.xlsx is open in Excel)."""
    buf = excel_sync.workbook_bytes(get_db())
    return Response(buf.getvalue(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=hospital_data.xlsx"})


@app.route("/staff/export.csv")
@staff_required
def staff_export():
    """Download the day's bookings as a CSV that opens in Excel (like your old sheet)."""
    day = request.args.get("day", svc.today_iso())
    rows = get_db().execute(
        """SELECT d.name AS doctor, d.ward, d.room, a.token, a.start_min, a.status,
                  COALESCE(a.person_name, p.name) AS patient, p.phone, a.symptoms
             FROM appointments a JOIN doctors d ON d.id = a.doctor_id JOIN patients p ON p.id = a.patient_id
            WHERE a.appt_date = ? ORDER BY d.name, a.start_min""",
        (day,),
    ).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Doctor Name", "Ward Name", "Room", "Token", "Time", "Status", "Patient Name", "Phone", "Summary"])
    for r in rows:
        writer.writerow([r["doctor"], r["ward"], r["room"], r["token"], svc.fmt_time(r["start_min"]),
                         r["status"], r["patient"], r["phone"], r["symptoms"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=bookings-{day}.csv"})


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="We couldn't find that page."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request.")), 400


@app.errorhandler(413)
def too_large(_e):
    flash("File is too large. Maximum size is 10 MB.", "danger")
    return redirect(request.referrer or url_for("records"))


def _excel_refresher():
    import time
    # Normal: every minute. With PostgreSQL (Neon free plan) every 30 minutes, so the database can sleep.
    gap = int(os.environ.get("EXCEL_REFRESH_SECONDS", "0") or 0) or (1800 if mailer.lazy_mode(app.config["DATABASE"]) else 60)
    while True:
        time.sleep(gap)
        try:
            sync_excel()
        except Exception as exc:
            print(f"[excel] {exc}")


_JOBS_STARTED = False


def start_background_jobs():
    """Excel refresh + tablet e-mail reminders. Started once per process
    (by `python app.py` locally, or by gunicorn.conf.py on a server)."""
    global _JOBS_STARTED
    if _JOBS_STARTED:
        return
    _JOBS_STARTED = True
    import threading
    threading.Thread(target=_excel_refresher, daemon=True).start()
    threading.Thread(target=mailer.loop, args=(app.config["DATABASE"],), daemon=True).start()
    sync_excel()
    print("[jobs] background jobs started (Excel refresh, tablet e-mail reminders)")


if __name__ == "__main__":
    # In debug mode Flask starts twice; only the real server process should run the background jobs.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_background_jobs()
    sync_excel()
    print("\n  MediKiosk is running ->  http://127.0.0.1:5000")
    print("  Staff login           ->  http://127.0.0.1:5000/staff/login  (admin / admin123)\n")
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
