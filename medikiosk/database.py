"""
database.py - SQLite database (one file: instance/medikiosk.db), or MySQL (Aiven) when DATABASE_URL is set.

Tables
  patients      - patient accounts (password stored as a secure hash)
  staff         - reception / admin login
  doctors       - seeded from 'Neelima Hospital dummy data.xlsx'
  appointments  - one row per booking (this replaces the single
                  'Patient Name' cell in the Excel sheet, so a doctor
                  can have a real queue)
  records       - uploaded reports / prescriptions / scans
  medications   - tablet reminders
  med_logs      - 'taken' / 'skipped' history for each reminder
"""
import os
import sqlite3

import mysql_compat
import pg_compat

from flask import current_app, g
from werkzeug.security import generate_password_hash

from services import parse_shift

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    phone         TEXT,
    age           INTEGER,
    gender        TEXT,
    password_hash TEXT NOT NULL,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS staff (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doctors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    email           TEXT,
    phone           TEXT,
    ward            TEXT NOT NULL,
    room            TEXT NOT NULL,
    shift_label     TEXT NOT NULL,
    shift_start     INTEGER NOT NULL,
    shift_end       INTEGER NOT NULL,
    consult_minutes INTEGER NOT NULL DEFAULT 15,
    on_leave        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS appointments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id    INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    doctor_id     INTEGER NOT NULL REFERENCES doctors(id),
    ward          TEXT NOT NULL,
    symptoms      TEXT NOT NULL,
    appt_date     TEXT NOT NULL,
    token         INTEGER NOT NULL,
    start_min     INTEGER NOT NULL,
    end_min       INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'Booked',
    triage_source TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_appt_doctor_day ON appointments(doctor_id, appt_date, status);
CREATE TABLE IF NOT EXISTS records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id   INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    category     TEXT NOT NULL,
    record_date  TEXT,
    notes        TEXT,
    filename     TEXT NOT NULL,
    stored_name  TEXT NOT NULL,
    uploaded_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS medications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    dosage      TEXT,
    times       TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT,
    notes       TEXT,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS med_snooze (
    medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    log_date      TEXT NOT NULL,
    time_slot     TEXT NOT NULL,
    until_min     INTEGER NOT NULL,
    count         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (medication_id, log_date, time_slot)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS reminder_emails (
    medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    log_date      TEXT NOT NULL,
    time_slot     TEXT NOT NULL,
    UNIQUE (medication_id, log_date, time_slot)
);
CREATE TABLE IF NOT EXISTS med_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    log_date      TEXT NOT NULL,
    time_slot     TEXT NOT NULL,
    status        TEXT NOT NULL,
    logged_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (medication_id, log_date, time_slot)
);
"""

# Data from 'Neelima Hospital dummy data.xlsx' (+ a room number for each doctor)
DOCTORS = [
    ("Dr. Aarav Sharma", "aarav.sharma@hospital.com", "9876543210", "General Ward", "1st floor 101", "9:00 AM – 1:00 PM"),
    ("Dr. Meera Patel", "meera.patel@hospital.com", "9876543210", "General Ward", "1st floor 102", "1:00 PM – 10:00 PM"),
    ("Dr. Rohan Verma", "rohan.verma@hospital.com", "9876543210", "General Ward", "1st floor 103", "10:00 AM – 2:00 PM"),
    ("Dr. Ananya Singh", "ananya.singh@hospital.com", "9876543210", "General Ward", "2nd floor 201", "8:00 AM – 12:00 PM"),
    ("Dr. Kunal Mehta", "kunal.mehta@hospital.com", "9876543210", "Emergency Ward", "2nd floor 202", "12:00 PM – 4:00 PM"),
    ("Dr. Priya Nair", "priya.nair@hospital.com", "9876543210", "Emergency Ward", "2nd floor 203", "4:00 PM – 8:00 PM"),
    ("Dr. Suresh Iyer", "suresh.iyer@hospital.com", "9876543210", "Mental Health Ward", "3rd floor 301", "9:00 AM – 12:00 PM"),
    ("Dr. Neha Kapoor", "neha.kapoor@hospital.com", "9876543210", "Emergency Ward", "3rd floor 302", "12:00 PM – 3:00 PM"),
    ("Dr. Vikram Joshi", "vikram.joshi@hospital.com", "9876543210", "Mental Health Ward", "3rd floor 303", "12:00 PM – 1:00 AM"),
    ("Dr. Pooja Malhotra", "pooja.malhotra@hospital.com", "9876543210", "Mental Health Ward", "1st floor 104", "6:00 PM – 9:00 PM"),
]


def open_target(target):
    """Open a connection to either a MySQL URL or a SQLite file path (also used by background threads)."""
    if mysql_compat.is_mysql_url(target):
        return mysql_compat.connect(target)
    if pg_compat.is_pg_url(target):
        return pg_compat.connect(target)
    db = sqlite3.connect(target, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def get_db():
    if "db" not in g:
        g.db = open_target(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    use_mysql = mysql_compat.is_mysql_url(app.config["DATABASE"])
    use_pg = pg_compat.is_pg_url(app.config["DATABASE"])
    if not (use_mysql or use_pg):
        os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        db = get_db()
        if use_mysql or use_pg:
            for statement in (mysql_compat.SCHEMA_MYSQL if use_mysql else pg_compat.SCHEMA_PG):
                db.execute(statement)
            db.commit()
        else:
            db.executescript(SCHEMA)
            # upgrade older databases: priority flag for Emergency / Mental Health bookings
            cols = [r[1] for r in db.execute("PRAGMA table_info(appointments)")]
            if "priority" not in cols:
                db.execute("ALTER TABLE appointments ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "person_name" not in cols:   # who the appointment is FOR (family members book under one account)
                db.execute("ALTER TABLE appointments ADD COLUMN person_name TEXT")
        if db.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 0:
            for name, email, phone, ward, room, shift in DOCTORS:
                start, end = parse_shift(shift)
                db.execute(
                    """INSERT INTO doctors (name, email, phone, ward, room, shift_label, shift_start, shift_end)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, email, phone, ward, room, shift, start, end),
                )
        # one-time alignment with the hospital sheet (ward / room / shift). After this, edits made by
        # the staff on the Doctors page are kept - they are NOT overwritten on restart.
        if not db.execute("SELECT 1 FROM meta WHERE `key` = 'doctors_synced_v1'").fetchone():
            for name, email, phone, ward, room, shift in DOCTORS:
                start, end = parse_shift(shift)
                db.execute(
                    """UPDATE doctors SET email = ?, phone = ?, ward = ?, room = ?, shift_label = ?,
                              shift_start = ?, shift_end = ? WHERE name = ?""",
                    (email, phone, ward, room, shift, start, end, name),
                )
            db.execute("INSERT INTO meta (`key`, `value`) VALUES ('doctors_synced_v1', '1')")
        if db.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 0:
            db.execute(
                "INSERT INTO staff (username, password_hash) VALUES (?, ?)",
                (
                    os.environ.get("STAFF_USERNAME", "admin"),
                    generate_password_hash(os.environ.get("STAFF_PASSWORD", "admin123")),
                ),
            )
        db.commit()
