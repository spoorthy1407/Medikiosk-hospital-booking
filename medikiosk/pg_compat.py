"""
pg_compat.py - lets the website run on PostgreSQL (Neon, Supabase, ...) with the SAME code that runs on SQLite / MySQL.

Used when DATABASE_URL starts with postgres:// or postgresql://, e.g. a Neon connection string:
  postgresql://USER:PASSWORD@ep-xxxx.region.aws.neon.tech/neondb?sslmode=require
Driver: pg8000 (pure Python - nothing to compile, works on Windows, Linux, Render).
"""
import re
from urllib.parse import parse_qs, unquote, urlparse

import pg8000.dbapi

from mysql_compat import MyRow, _clean

LOCK_KEY = 7771001                       # one booking decides slots at a time (like SQLite's BEGIN IMMEDIATE)
ID_TABLES = {"patients", "staff", "doctors", "appointments", "records", "medications", "med_logs"}


def is_pg_url(target):
    return isinstance(target, str) and target.lower().startswith(("postgres://", "postgresql://", "postgresql+pg8000://"))


def translate(sql):
    """SQLite-style SQL -> PostgreSQL."""
    sql = sql.replace("%", "%%").replace("?", "%s").replace("`", '"')
    sql = re.sub(r"\bLIKE\b", "ILIKE", sql)              # SQLite/MySQL LIKE ignores case; PostgreSQL's does not
    m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
    if m and m.group(1).lower() in ID_TABLES and "RETURNING" not in sql.upper():
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
    return sql


class PGCursor:
    def __init__(self, cur, returning=False):
        self._cur = cur
        self._cols = [d[0] if isinstance(d[0], str) else d[0].decode() for d in cur.description] if cur is not None and cur.description else []
        self.rowcount = cur.rowcount if cur is not None else 0
        self.lastrowid = None
        self._pending = None
        if returning and self._cols:
            row = cur.fetchone()
            self.lastrowid = row[0] if row else None
            self._cols = []                                  # the id row is consumed

    def _wrap(self, r):
        return MyRow(self._cols, [_clean(v) for v in r])

    def fetchone(self):
        if not self._cols:
            return None
        r = self._cur.fetchone()
        return self._wrap(r) if r is not None else None

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()] if self._cols else []

    def __iter__(self):
        return iter(self.fetchall())


class PGConn:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        s = sql.strip()
        up = s.upper()
        if up == "BEGIN IMMEDIATE":
            self._conn.commit()                              # fresh view of the data
            cur = self._conn.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))   # released automatically at commit/rollback
            return PGCursor(None)
        if up.startswith("PRAGMA"):
            return PGCursor(None)
        sql2 = translate(s)
        cur = self._conn.cursor()
        cur.execute(sql2, tuple(params))
        return PGCursor(cur, returning=sql2.endswith("RETURNING id"))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def connect(url):
    u = urlparse(url.replace("postgresql+pg8000://", "postgresql://", 1).replace("postgres://", "postgresql://", 1))
    q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
    mode = q.get("sslmode", "").lower()
    kwargs = dict(user=unquote(u.username or ""), password=unquote(u.password or ""), host=u.hostname,
                  port=u.port or 5432, database=(u.path or "/").lstrip("/") or None, timeout=30)
    if mode not in ("disable", "allow"):
        kwargs["ssl_context"] = True                         # Neon / Supabase require SSL
    return PGConn(pg8000.dbapi.connect(**kwargs))


SCHEMA_PG = [
    """CREATE TABLE IF NOT EXISTS patients (
        id SERIAL PRIMARY KEY, name VARCHAR(120) NOT NULL, email VARCHAR(190) NOT NULL UNIQUE,
        phone VARCHAR(30), age INT, gender VARCHAR(20), password_hash VARCHAR(255) NOT NULL,
        created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC'))""",
    """CREATE TABLE IF NOT EXISTS staff (
        id SERIAL PRIMARY KEY, username VARCHAR(100) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS doctors (
        id SERIAL PRIMARY KEY, name VARCHAR(120) NOT NULL, email VARCHAR(190), phone VARCHAR(30),
        ward VARCHAR(60) NOT NULL, room VARCHAR(60) NOT NULL, shift_label VARCHAR(60) NOT NULL,
        shift_start INT NOT NULL, shift_end INT NOT NULL, consult_minutes INT NOT NULL DEFAULT 15,
        on_leave INT NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS appointments (
        id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        doctor_id INT NOT NULL REFERENCES doctors(id), ward VARCHAR(60) NOT NULL, symptoms TEXT NOT NULL,
        appt_date VARCHAR(10) NOT NULL, token INT NOT NULL, start_min INT NOT NULL, end_min INT NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'Booked', triage_source VARCHAR(40),
        created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC'), completed_at VARCHAR(30),
        priority INT NOT NULL DEFAULT 0, person_name VARCHAR(120))""",
    "CREATE INDEX IF NOT EXISTS idx_appt_doctor_day ON appointments (doctor_id, appt_date, status)",
    """CREATE TABLE IF NOT EXISTS records (
        id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        title VARCHAR(200) NOT NULL, category VARCHAR(60) NOT NULL, record_date VARCHAR(10), notes TEXT,
        filename VARCHAR(255) NOT NULL, stored_name VARCHAR(255) NOT NULL,
        uploaded_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC'))""",
    """CREATE TABLE IF NOT EXISTS medications (
        id SERIAL PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        name VARCHAR(160) NOT NULL, dosage VARCHAR(100), times VARCHAR(255) NOT NULL, start_date VARCHAR(10) NOT NULL,
        end_date VARCHAR(10), notes TEXT, active INT NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS med_snooze (
        medication_id INT NOT NULL REFERENCES medications(id) ON DELETE CASCADE, log_date VARCHAR(10) NOT NULL,
        time_slot VARCHAR(10) NOT NULL, until_min INT NOT NULL, count INT NOT NULL DEFAULT 1,
        PRIMARY KEY (medication_id, log_date, time_slot))""",
    """CREATE TABLE IF NOT EXISTS meta ("key" VARCHAR(100) PRIMARY KEY, "value" VARCHAR(255))""",
    """CREATE TABLE IF NOT EXISTS reminder_emails (
        medication_id INT NOT NULL REFERENCES medications(id) ON DELETE CASCADE, log_date VARCHAR(10) NOT NULL,
        time_slot VARCHAR(40) NOT NULL, PRIMARY KEY (medication_id, log_date, time_slot))""",
    """CREATE TABLE IF NOT EXISTS med_logs (
        id SERIAL PRIMARY KEY, medication_id INT NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
        log_date VARCHAR(10) NOT NULL, time_slot VARCHAR(10) NOT NULL, status VARCHAR(20) NOT NULL,
        logged_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC'), UNIQUE (medication_id, log_date, time_slot))""",
]
