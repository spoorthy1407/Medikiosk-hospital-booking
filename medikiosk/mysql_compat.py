"""
mysql_compat.py - lets the website run on MySQL (e.g. Aiven) with the SAME code that runs on SQLite.

Used only when the DATABASE_URL environment variable is set, e.g.
  mysql://avnadmin:PASSWORD@HOST:PORT/defaultdb?ssl-mode=REQUIRED
Without DATABASE_URL the website keeps using the local SQLite file, exactly as before.

It wraps a PyMySQL connection so that the rest of the code can keep using
  db.execute("... ? ...", params)  /  row["column"]  /  row[0]  /  cursor.lastrowid  /  db.commit()
"""
import os
import re
import ssl
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlparse

import pymysql

LOCK_NAME = "medikiosk_booking"


class MyRow:
    """Behaves like sqlite3.Row: row['name'], row[0], row.keys(), iteration over values."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols, self._vals = cols, vals

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        try:
            return self._vals[self._cols.index(key)]
        except ValueError:
            raise IndexError(f"No item with that key: {key}") from None

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return f"MyRow({dict(zip(self._cols, self._vals))})"


def _clean(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


class MyCursor:
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = cur.lastrowid if cur is not None else None
        self.rowcount = cur.rowcount if cur is not None else 0
        self._cols = [d[0] for d in cur.description] if cur is not None and cur.description else []

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


def translate(sql):
    """SQLite-style SQL -> MySQL."""
    sql = sql.replace("%", "%%").replace("?", "%s")
    sql = re.sub(r"ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET", "ON DUPLICATE KEY UPDATE", sql, flags=re.I)
    sql = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", sql, flags=re.I)
    return sql


class MySQLConn:
    def __init__(self, conn):
        self._conn = conn
        self._locked = False

    def execute(self, sql, params=()):
        s = sql.strip()
        up = s.upper()
        if up == "BEGIN IMMEDIATE":
            return self._begin_exclusive()
        if up.startswith("PRAGMA"):
            return MyCursor(None)
        cur = self._conn.cursor()
        cur.execute(translate(s), tuple(params))
        return MyCursor(cur)

    def _begin_exclusive(self):
        """Same idea as SQLite's BEGIN IMMEDIATE: only one booking decides slots at a time."""
        cur = self._conn.cursor()
        cur.execute("SELECT GET_LOCK(%s, 15)", (LOCK_NAME,))
        if cur.fetchone()[0] != 1:
            raise RuntimeError("The booking system is busy, please try again in a moment.")
        self._locked = True
        self._conn.commit()          # fresh view of the data
        cur.execute("START TRANSACTION")
        return MyCursor(None)

    def _unlock(self):
        if self._locked:
            try:
                self._conn.cursor().execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
            finally:
                self._locked = False

    def commit(self):
        try:
            self._conn.commit()
        finally:
            self._unlock()

    def rollback(self):
        try:
            self._conn.rollback()
        finally:
            self._unlock()

    def close(self):
        try:
            self._unlock()
        except Exception:
            pass
        self._conn.close()


def is_mysql_url(target):
    return isinstance(target, str) and target.lower().startswith(("mysql://", "mysql+pymysql://"))


def connect(url):
    u = urlparse(url.replace("mysql+pymysql://", "mysql://", 1))
    q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
    mode = q.get("ssl-mode", q.get("ssl_mode", "")).upper()
    kwargs = dict(
        host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""), password=unquote(u.password or ""),
        database=(u.path or "/").lstrip("/") or None, charset="utf8mb4",
        connect_timeout=10, read_timeout=30, write_timeout=30, autocommit=False,
    )
    if mode in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"):
        ca = os.environ.get("DATABASE_CA", "").strip()
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        ctx.check_hostname = False
        if not ca:
            ctx.verify_mode = ssl.CERT_NONE   # encrypted; add DATABASE_CA=<ca.pem path> to also verify the server
        kwargs["ssl"] = ctx
    return MySQLConn(pymysql.connect(**kwargs))


# ---------------------------------------------------------------------------------------------
# MySQL version of the tables (SQLite version lives in database.py)
# ---------------------------------------------------------------------------------------------
SCHEMA_MYSQL = [
    """CREATE TABLE IF NOT EXISTS patients (
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(120) NOT NULL, email VARCHAR(190) NOT NULL UNIQUE,
        phone VARCHAR(30), age INT, gender VARCHAR(20), password_hash VARCHAR(255) NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS staff (
        id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(100) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS doctors (
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(120) NOT NULL, email VARCHAR(190), phone VARCHAR(30),
        ward VARCHAR(60) NOT NULL, room VARCHAR(60) NOT NULL, shift_label VARCHAR(60) NOT NULL,
        shift_start INT NOT NULL, shift_end INT NOT NULL, consult_minutes INT NOT NULL DEFAULT 15,
        on_leave INT NOT NULL DEFAULT 0) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS appointments (
        id INT AUTO_INCREMENT PRIMARY KEY, patient_id INT NOT NULL, doctor_id INT NOT NULL,
        ward VARCHAR(60) NOT NULL, symptoms TEXT NOT NULL, appt_date VARCHAR(10) NOT NULL, token INT NOT NULL,
        start_min INT NOT NULL, end_min INT NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'Booked',
        triage_source VARCHAR(40), created_at DATETIME DEFAULT CURRENT_TIMESTAMP, completed_at VARCHAR(30),
        priority INT NOT NULL DEFAULT 0, person_name VARCHAR(120),
        INDEX idx_appt_doctor_day (doctor_id, appt_date, status),
        FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE CASCADE,
        FOREIGN KEY (doctor_id) REFERENCES doctors(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS records (
        id INT AUTO_INCREMENT PRIMARY KEY, patient_id INT NOT NULL, title VARCHAR(200) NOT NULL,
        category VARCHAR(60) NOT NULL, record_date VARCHAR(10), notes TEXT, filename VARCHAR(255) NOT NULL,
        stored_name VARCHAR(255) NOT NULL, uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE CASCADE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS medications (
        id INT AUTO_INCREMENT PRIMARY KEY, patient_id INT NOT NULL, name VARCHAR(160) NOT NULL, dosage VARCHAR(100),
        times VARCHAR(255) NOT NULL, start_date VARCHAR(10) NOT NULL, end_date VARCHAR(10), notes TEXT,
        active INT NOT NULL DEFAULT 1,
        FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE CASCADE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS med_snooze (
        medication_id INT NOT NULL, log_date VARCHAR(10) NOT NULL, time_slot VARCHAR(10) NOT NULL,
        until_min INT NOT NULL, count INT NOT NULL DEFAULT 1,
        PRIMARY KEY (medication_id, log_date, time_slot),
        FOREIGN KEY (medication_id) REFERENCES medications(id) ON DELETE CASCADE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS meta (
        `key` VARCHAR(100) PRIMARY KEY, `value` VARCHAR(255)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS reminder_emails (
        medication_id INT NOT NULL, log_date VARCHAR(10) NOT NULL, time_slot VARCHAR(40) NOT NULL,
        PRIMARY KEY (medication_id, log_date, time_slot),
        FOREIGN KEY (medication_id) REFERENCES medications(id) ON DELETE CASCADE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS med_logs (
        id INT AUTO_INCREMENT PRIMARY KEY, medication_id INT NOT NULL, log_date VARCHAR(10) NOT NULL,
        time_slot VARCHAR(10) NOT NULL, status VARCHAR(20) NOT NULL, logged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_med_log (medication_id, log_date, time_slot),
        FOREIGN KEY (medication_id) REFERENCES medications(id) ON DELETE CASCADE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]
